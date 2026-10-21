"""Qualify the Debian package: its receipt, the installed files, a real export and removal.

The CI job installs the package with dpkg and apt, then runs this in phases:

    check_linux_deb.py receipt   PACKAGE.deb           the bytes match BUILD-RECEIPT.json
    check_linux_deb.py installed PACKAGE.deb FIXTURE   files, CLI, desktop start, export
    check_linux_deb.py removed                         after `apt-get remove`
    check_linux_deb.py purged                          after `apt-get purge`: nothing left

`installed` runs as an ordinary user, because Chromium refuses to start its
sandbox as root. The export uses the bundled Chromium with its sandbox on.
Where the host's AppArmor policy refuses that sandbox (Ubuntu 23.10 and later
in a container, which cannot load the package's profile), the only accepted
failure is the app's coded refusal. With --fallback-browser, a trusted
installed Chromium is offered on PATH and the automatic fallback must export.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

try:
    from tools import build_deb, smoke_linux_desktop
except ModuleNotFoundError:  # Direct execution puts tools/, not the root, on sys.path.
    import build_deb
    import smoke_linux_desktop

RUNTIME = Path("/") / build_deb.RESOURCE_DIR / "runtime"
BROWSER = Path("/") / build_deb.BROWSER
PROFILE_SOURCE = Path("/usr/share/platefold/apparmor") / build_deb.PROFILE
PROFILE = Path("/etc/apparmor.d") / build_deb.PROFILE
INSTALLED = (
    Path("/usr/bin/platefold"),
    Path("/usr/bin/platefold-desktop"),
    BROWSER,
    RUNTIME / "platefold",
    PROFILE_SOURCE,
    Path("/usr/share/applications/PlateFold.desktop"),
    Path("/usr/share/icons/hicolor/256x256/apps/platefold-desktop.png"),
    # Minimal container images keep only the copyright of /usr/share/doc and
    # drop manual pages (dpkg path-exclude), so only this one is required.
    Path("/usr/share/doc/platefold/copyright"),
)
# Where anything the package or its scripts wrote could remain after purge.
# The user's own settings, caches and exported PDFs are never removed.
SCAN_ROOTS = ("/etc", "/opt", "/usr", "/var/cache", "/var/lib")
SCAN_SKIP = ("/var/lib/apt", "/var/cache/apt", "/var/cache/debconf", "/var/lib/dpkg/available")


def digest(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def package_status():
    result = subprocess.run(
        ["dpkg-query", "-W", "-f=${db:Status-Abbrev}|${Version}", build_deb.PACKAGE],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        return None, None
    status, version = result.stdout.split("|", 1)
    return status.strip(), version


def check_receipt(package, expected_commit=None):
    """The package is the file the build recorded, from a clean tree at this commit."""
    folder = package.parent
    receipt = json.loads((folder / "BUILD-RECEIPT.json").read_text(encoding="utf-8"))
    if expected_commit and receipt["source_commit"] != expected_commit:
        raise SystemExit("Build receipt source commit does not match the requested commit")
    if expected_commit and receipt.get("source_dirty") is not False:
        raise SystemExit("Build receipt does not describe a clean source tree")
    records = [item for item in receipt["artifacts"] if item["file"] == package.name]
    if len(records) != 1 or receipt.get("debian_package", {}).get("file") != package.name:
        raise SystemExit("BUILD-RECEIPT.json does not record this package")
    if records[0]["sha256"] != digest(package) or records[0]["size"] != package.stat().st_size:
        raise SystemExit("Package bytes do not match BUILD-RECEIPT.json")
    sums = (folder / "SHA256SUMS").read_text(encoding="utf-8").splitlines()
    if f"{records[0]['sha256']}  {package.name}" not in sums:
        raise SystemExit("The package checksum is missing from SHA256SUMS")
    return receipt


def check_files(receipt):
    status, version = package_status()
    expected = receipt["debian_package"]["package_version"]
    if status != "ii" or version != expected:
        raise SystemExit(f"dpkg reports platefold {status} {version}, expected ii {expected}")
    missing = [str(path) for path in INSTALLED if not path.is_file()]
    if missing:
        raise SystemExit(f"Installed package is missing: {', '.join(missing)}")
    program = Path("/usr/bin/platefold-desktop").read_bytes()
    if program.count(build_deb.DEB_MARKER) != 1 or build_deb.MARKER.search(program):
        raise SystemExit("The installed desktop program is not marked as a .deb installation")
    # One Chromium payload: the installed browser is the one the build recorded.
    if digest(BROWSER) != receipt["bundled_browser"]["source_sha256"]:
        raise SystemExit("The installed Chromium differs from the build's bundled browser")
    attached = build_deb.profile_attachment(PROFILE_SOURCE.read_text(encoding="utf-8"))
    if attached != str(BROWSER):
        raise SystemExit(f"The AppArmor profile attaches to {attached}, not {BROWSER}")


def check_profile(expect):
    """The profile is in /etc/apparmor.d exactly when this AppArmor can read it."""
    if expect == "installed":
        if not PROFILE.is_file() or PROFILE.read_bytes() != PROFILE_SOURCE.read_bytes():
            raise SystemExit(f"{PROFILE} is not the packaged profile")
        mode = PROFILE.stat().st_mode & 0o7777
        if mode != 0o644 or PROFILE.stat().st_uid != 0:
            raise SystemExit(f"{PROFILE} must be root-owned mode 0644, found {mode:o}")
    elif expect == "absent" and PROFILE.exists():
        raise SystemExit(f"{PROFILE} was installed where AppArmor cannot read it")


def run(command, *, env=None, timeout=180):
    return smoke_linux_desktop.run_checked(command, env=env, timeout=timeout)


def check_cli(receipt):
    version = run(["platefold", "--version"]).stdout.strip()
    if receipt["version"].replace("-dev.", ".dev") not in version:
        raise SystemExit(f"platefold --version printed {version!r}")
    catalog = json.loads(run(["platefold", "--list-themes", "--json"]).stdout)
    if catalog["schema_version"] != 1 or catalog["status"] != "completed":
        raise SystemExit("The installed CLI returned an invalid theme catalog")
    environment = {**os.environ, "LD_LIBRARY_PATH": str(RUNTIME / "_internal")}
    dependencies = run(["ldd", str(BROWSER)], env=environment).stdout
    if "not found" in dependencies:
        raise SystemExit(f"The bundled Chromium has unresolved libraries:\n{dependencies}")
    browser_version = run([str(BROWSER), "--version"], env=environment).stdout.strip()
    return version, browser_version


def export(fixture, output, *, browser, accept_refusal, path=None, expected_figures=None):
    command = ["platefold", str(fixture), "-o", str(output), "--json", "--browser", browser]
    environment = {**os.environ, **({"PATH": path} if path else {})}
    attempted = subprocess.run(
        command, capture_output=True, text=True, timeout=300, env=environment
    )
    try:
        result = json.loads(attempted.stdout)
    except json.JSONDecodeError:
        result = {}
    refused = smoke_linux_desktop.sandbox_refusal(result)
    if refused and accept_refusal:
        return {"browser": browser, "status": "refused", "error_code": refused["code"]}
    if attempted.returncode:
        details = (attempted.stderr or attempted.stdout).strip()
        raise SystemExit(
            f"Export with --browser {browser} failed ({attempted.returncode}): {details}"
        )
    pdf = output / f"{fixture.stem}.pdf"
    documents = smoke_linux_desktop.verify_export(result, pdf, expected_figures=expected_figures)
    return {
        "browser": browser,
        "status": result["status"],
        "engine": result["report"].get("engine"),
        "pdf_sha256": digest(pdf),
        "figures": documents[0]["figures"],
    }


def installed(args):
    receipt = check_receipt(args.package, args.expected_commit)
    check_files(receipt)
    check_profile(args.expect_profile)
    version, browser_version = check_cli(receipt)
    startup = {"native_startup_exercised": False}
    if args.native_startup_seconds:
        startup = smoke_linux_desktop.exercise_native_startup(
            ["platefold-desktop"], hold_seconds=args.native_startup_seconds
        )
    exports = []
    with tempfile.TemporaryDirectory(prefix="platefold-deb-") as temporary:
        temporary = Path(temporary)
        exports.append(
            export(
                args.fixture,
                temporary / "bundled",
                browser="bundled",
                accept_refusal=args.accept_sandbox_refusal,
                expected_figures=args.expect_figures,
            )
        )
        if args.fallback_browser:
            bin_dir = temporary / "bin"
            bin_dir.mkdir()
            (bin_dir / "chromium").symlink_to(args.fallback_browser)
            result = export(
                args.fixture,
                temporary / "fallback",
                browser="auto",
                accept_refusal=False,
                path=f"{bin_dir}:{os.environ.get('PATH', os.defpath)}",
                expected_figures=args.expect_figures,
            )
            if exports[0]["status"] == "refused" and "sandbox" not in (result["engine"] or ""):
                raise SystemExit(f"Fallback export did not report the refused sandbox: {result}")
            exports.append(result)
    print(
        json.dumps(
            {
                "package": args.package.name,
                "package_sha256": digest(args.package),
                "source_commit": receipt["source_commit"],
                "installed_version": package_status()[1],
                "version_output": version,
                "browser_version": browser_version,
                "apparmor_profile": "installed" if PROFILE.is_file() else "absent",
                "exports": exports,
                **startup,
            },
            sort_keys=True,
        )
    )


def leftovers():
    found = []
    pattern = re.compile("platefold", re.IGNORECASE)
    for root in SCAN_ROOTS:
        for directory, subdirectories, files in os.walk(root):
            if directory.startswith(SCAN_SKIP):
                subdirectories[:] = []
                continue
            for name in subdirectories + files:
                if pattern.search(name):
                    found.append(os.path.join(directory, name))
    return sorted(found)


def removed(args):
    status, _ = package_status()
    if args.phase == "removed" and status != "rc":
        raise SystemExit(f"After removal dpkg reports {status}, expected rc")
    if args.phase == "purged" and status not in (None, "un", "pn"):
        raise SystemExit(f"After purge dpkg still reports platefold as {status}")
    remaining = [str(path) for path in (*INSTALLED, PROFILE) if path.exists()]
    remaining += [str(path) for path in Path("/var/cache/apparmor").glob(f"*/{build_deb.PROFILE}")]
    if args.phase == "purged":
        remaining += leftovers()
    if remaining:
        raise SystemExit(f"Left behind after {args.phase}: {', '.join(sorted(set(remaining)))}")
    print(json.dumps({"phase": args.phase, "dpkg_status": status, "left_behind": []}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    phases = parser.add_subparsers(dest="phase", required=True)
    receipt = phases.add_parser("receipt")
    receipt.add_argument("package", type=Path)
    receipt.add_argument("--expected-commit", default=os.environ.get("CI_COMMIT_SHA"))
    check = phases.add_parser("installed")
    check.add_argument("package", type=Path)
    check.add_argument("fixture", type=Path)
    check.add_argument("--expected-commit", default=os.environ.get("CI_COMMIT_SHA"))
    check.add_argument("--expect-profile", choices=("installed", "absent"), required=True)
    check.add_argument("--native-startup-seconds", type=float)
    check.add_argument("--expect-figures", type=int)
    check.add_argument("--accept-sandbox-refusal", action="store_true")
    check.add_argument("--fallback-browser", type=Path)
    phases.add_parser("removed")
    phases.add_parser("purged")
    args = parser.parse_args()
    if args.phase == "receipt":
        check_receipt(args.package.resolve(), args.expected_commit)
        print(json.dumps({"package": args.package.name, "sha256": digest(args.package)}))
    elif args.phase == "installed":
        args.package = args.package.resolve()
        args.fixture = args.fixture.resolve()
        installed(args)
    else:
        removed(args)


if __name__ == "__main__":
    main()
