"""Sign, notarize and package the exact Apple Silicon PlateFold application."""

from __future__ import annotations

import contextlib
import errno
import hashlib
import json
import os
import platform
import pty
import re
import select
import signal
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "desktop/src-tauri/target/release/bundle/macos/PlateFold.app"
UNSIGNED = ROOT / "dist/macos-unsigned/BUILD-RECEIPT.json"
OUTPUT = ROOT / "dist/macos-release"
STATE = ROOT / "build/macos-release"
PROFILE = "platefold-release"
EXPECTED_CERTIFICATE_SHA256 = "fe6bf19b7ca85ea85470c461458fdd5b3de7216792e350ed6916500550707464"
MACHO_MAGICS = {
    b"\xca\xfe\xba\xbe",
    b"\xbe\xba\xfe\xca",
    b"\xca\xfe\xba\xbf",
    b"\xbf\xba\xfe\xca",
    b"\xce\xfa\xed\xfe",
    b"\xcf\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
}
PRIVATE_VARIABLES = {
    "CM_CERTIFICATE",
    "CM_CERTIFICATE_PASSWORD",
    "APPLE_ID",
    "APPLE_APP_SPECIFIC_PASSWORD",
    "APPLE_PASSWORD",
    "PF_KEYCHAIN_PASSWORD",
}


class Stop(Exception):
    """A release prerequisite or native verification failed."""


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def safe_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in PRIVATE_VARIABLES}


def public_diagnostic(output: bytes) -> str:
    values = {os.environ[key].encode() for key in PRIVATE_VARIABLES if os.environ.get(key)}
    for value in sorted(values, key=len, reverse=True):
        output = output.replace(value, b"[REDACTED]")
    text = output.decode("utf-8", errors="replace")
    text = "".join(
        character for character in text if character.isprintable() or character in "\n\t"
    )
    return text[-4096:].strip()


def run(
    arguments: list[str], stage: str, *, input_bytes: bytes | None = None, timeout=180
) -> bytes:
    try:
        result = subprocess.run(
            arguments,
            input=input_bytes,
            capture_output=True,
            env=safe_environment(),
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise Stop(f"{stage}: timed out") from None
    if result.returncode:
        message = f"{stage}: exit {result.returncode}"
        diagnostic = public_diagnostic(result.stderr + b"\n" + result.stdout)
        if diagnostic:
            message += "\n" + diagnostic
        raise Stop(message)
    return result.stdout or result.stderr


def save_status(receipt: dict) -> None:
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / "release-status.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def require_environment() -> tuple[str, str, str]:
    source = os.environ.get("PF_SOURCE_COMMIT", "")
    signer = os.environ.get("PF_SIGNER_SHA1", "")
    team = os.environ.get("APPLE_TEAM_ID", "")
    keychain = os.environ.get("PF_KEYCHAIN_PATH", "")
    keychain_password = os.environ.get("PF_KEYCHAIN_PASSWORD", "")
    if sys.platform != "darwin" or platform.machine() != "arm64":
        raise Stop("Release signing requires the reviewed native Apple Silicon host")
    if not re.fullmatch(r"[0-9a-f]{40}", source):
        raise Stop("Expected source commit is missing or malformed")
    if not re.fullmatch(r"[0-9a-f]{40}", signer):
        raise Stop("Expected Developer ID signer is missing or malformed")
    if not re.fullmatch(r"[A-Z0-9]{10}", team):
        raise Stop("Apple team ID is missing or malformed")
    if not keychain or not re.fullmatch(r"[A-Fa-f0-9-]{72}", keychain_password):
        raise Stop("Temporary signing keychain is missing or malformed")
    return source, signer, team


def validate_source(source: str) -> dict:
    recorded = json.loads(UNSIGNED.read_text(encoding="utf-8"))
    actual = run(["/usr/bin/git", "rev-parse", "HEAD"], "Read source revision").decode().strip()
    dirty = run(
        ["/usr/bin/git", "status", "--porcelain", "--untracked-files=no"],
        "Check source state",
    ).decode()
    if actual != source or dirty or recorded.get("source_commit") != source:
        raise Stop("Release application does not match the exact clean requested source")
    if recorded.get("source_dirty") is not False or not APP.is_dir():
        raise Stop("Unsigned build receipt or application bundle is invalid")
    return recorded


def partition_command(keychain: str, password: str) -> bytes:
    if not re.fullmatch(
        r"/[A-Za-z0-9_./-]+/pf-identity-[A-Fa-f0-9-]+/import.keychain-db", keychain
    ):
        raise Stop("Unexpected temporary-keychain path")
    return (
        f"set-key-partition-list -S apple-tool:,apple: -s -t private -k {password} {keychain}\n"
    ).encode()


def _prompt_child(arguments: list[str]) -> None:
    try:
        settings = termios.tcgetattr(0)
        settings[3] &= ~(termios.ECHO | termios.ECHONL)
        termios.tcsetattr(0, termios.TCSANOW, settings)
        os.execve(arguments[0], arguments, safe_environment())
    except BaseException:
        os._exit(125)


def _terminal_read(descriptor: int) -> bytes:
    try:
        return os.read(descriptor, 4096)
    except OSError as error:
        if error.errno != errno.EIO:
            raise
        return b""


def respond_to_hidden_prompt(arguments: list[str], password: str, timeout=120) -> None:
    pid, descriptor = pty.fork()
    if pid == 0:
        _prompt_child(arguments)
    sent = False
    reaped = False
    buffer = b""
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            readable, _, _ = select.select([descriptor], [], [], 0.2)
            if readable:
                buffer = (buffer + _terminal_read(descriptor))[-8192:]
                if not sent and re.search(rb"app-specific password[^\r\n]*:", buffer, re.I):
                    settings = termios.tcgetattr(descriptor)
                    if settings[3] & (termios.ECHO | termios.ECHONL):
                        raise Stop("Notarization password prompt unexpectedly enables echo")
                    os.write(descriptor, password.encode() + b"\n")
                    sent = True
                    buffer = b""
            finished, status = os.waitpid(pid, os.WNOHANG)
            if finished:
                reaped = True
                if not sent or os.waitstatus_to_exitcode(status) != 0:
                    raise Stop("Notarization credential validation failed")
                return
        raise Stop("Notarization credential validation timed out")
    finally:
        if not reaped:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pid, signal.SIGKILL)
            os.waitpid(pid, 0)
        os.close(descriptor)


def prepare_credentials(keychain: str, team: str) -> None:
    apple_id = os.environ.get("APPLE_ID", "")
    password = os.environ.get("APPLE_APP_SPECIFIC_PASSWORD", "")
    if not apple_id or any(character.isspace() for character in apple_id):
        raise Stop("Apple notarization account is missing or malformed")
    if not re.fullmatch(r"[A-Za-z-]{16,128}", password):
        raise Stop("Apple app-specific password is missing or malformed")
    run(
        ["/usr/bin/security", "-i"],
        "Configure temporary signing-key access",
        input_bytes=partition_command(keychain, os.environ["PF_KEYCHAIN_PASSWORD"]),
    )
    respond_to_hidden_prompt(
        [
            "/usr/bin/xcrun",
            "notarytool",
            "store-credentials",
            PROFILE,
            "--apple-id",
            apple_id,
            "--team-id",
            team,
            "--keychain",
            keychain,
        ],
        password,
    )


def is_macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        with path.open("rb") as handle:
            return handle.read(4) in MACHO_MAGICS
    except OSError:
        return False


def macho_files(app: Path) -> list[Path]:
    files = [path for path in app.rglob("*") if is_macho(path)]
    return sorted(files, key=lambda path: (-len(path.parts), path.as_posix()))


def chromium_executable(path: Path) -> bool:
    lowered = path.as_posix().lower()
    return path.name in {"chrome-headless-shell", "Chromium"} or (
        "chromium" in lowered and os.access(path, os.X_OK) and ".framework/" not in lowered
    )


def require_arm64(path: Path) -> None:
    arches = run(["/usr/bin/lipo", "-archs", str(path)], "Inspect bundled architecture")
    if arches.decode().split() != ["arm64"]:
        raise Stop(f"Bundled executable is not Apple Silicon only: {path.relative_to(APP)}")


def sign_file(path: Path, signer: str, keychain: str) -> None:
    command = [
        "/usr/bin/codesign",
        "--force",
        "--sign",
        signer,
        "--keychain",
        keychain,
        "--options",
        "runtime",
        "--timestamp",
    ]
    if chromium_executable(path):
        command.extend(
            ["--entitlements", str(ROOT / "packaging/macos/chromium-entitlements.plist")]
        )
    command.append(str(path))
    run(command, f"Sign {path.relative_to(APP)}")


def check_signature(path: Path, team: str) -> None:
    run(["/usr/bin/codesign", "--verify", "--strict", "--verbose=2", str(path)], "Verify code")
    details = run(
        ["/usr/bin/codesign", "--display", "--verbose=4", str(path)],
        "Read signature metadata",
    ).decode(errors="replace")
    if f"TeamIdentifier={team}" not in details.splitlines():
        raise Stop(f"Signature team does not match for {path.relative_to(APP)}")
    if not re.search(r"^Timestamp=.+$", details, re.MULTILINE):
        raise Stop(f"Secure timestamp is missing for {path.relative_to(APP)}")
    if not re.search(r"^CodeDirectory .*flags=.*\bruntime\b", details, re.MULTILINE):
        raise Stop(f"Hardened runtime is missing for {path.relative_to(APP)}")


def sign_application(signer: str, team: str) -> list[Path]:
    keychain = os.environ["PF_KEYCHAIN_PATH"]
    main = APP / "Contents/MacOS/platefold-desktop"
    nested = [path for path in macho_files(APP) if path != main]
    if not nested or not main.is_file():
        raise Stop("Application is missing its nested runtime or desktop executable")
    for index, path in enumerate(nested, start=1):
        require_arm64(path)
        sign_file(path, signer, keychain)
        if index % 25 == 0:
            print(f"Signed {index} of {len(nested)} nested binaries", flush=True)
    require_arm64(main)
    sign_file(main, signer, keychain)
    run(
        [
            "/usr/bin/codesign",
            "--force",
            "--sign",
            signer,
            "--keychain",
            keychain,
            "--options",
            "runtime",
            "--timestamp",
            str(APP),
        ],
        "Sign PlateFold application",
    )
    for path in [*nested, main, APP]:
        check_signature(path, team)
    run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(APP)],
        "Verify complete application",
    )
    return [*nested, main]


def smoke_runtime() -> dict[str, str]:
    worker = APP / "Contents/Resources/runtime/platefold"
    browsers = [path for path in APP.rglob("chrome-headless-shell") if path.is_file()]
    if len(browsers) != 1:
        raise Stop("Expected one bundled Chromium executable")
    worker_output = run([str(worker), "--version"], "Run signed PlateFold worker", timeout=90)
    browser_output = run([str(browsers[0]), "--version"], "Run signed Chromium", timeout=90)
    return {
        "worker": public_diagnostic(worker_output)[:256],
        "chromium": public_diagnostic(browser_output)[:256],
    }


def notarize(receipt: dict) -> None:
    submission = STATE / "notary-submission.zip"
    run(
        ["/usr/bin/ditto", "-c", "-k", "--keepParent", str(APP), str(submission)],
        "Package notarization submission",
        timeout=600,
    )
    receipt["submitted_zip_sha256"] = digest(submission)
    credentials = [
        "--keychain-profile",
        PROFILE,
        "--keychain",
        os.environ["PF_KEYCHAIN_PATH"],
    ]
    result = json.loads(
        run(
            [
                "/usr/bin/xcrun",
                "notarytool",
                "submit",
                str(submission),
                *credentials,
                "--output-format",
                "json",
            ],
            "Submit application to Apple",
            timeout=180,
        )
    )
    submission_id = result.get("id", "")
    if not re.fullmatch(r"[A-Fa-f0-9-]{36}", submission_id):
        raise Stop("Apple returned no valid notarization submission ID")
    receipt["notarization_submitted"] = True
    receipt["notarization_submission_id"] = submission_id
    save_status(receipt)
    print("Application submitted; waiting up to 30 minutes for Apple.", flush=True)
    result = json.loads(
        run(
            [
                "/usr/bin/xcrun",
                "notarytool",
                "wait",
                submission_id,
                *credentials,
                "--timeout",
                "30m",
                "--output-format",
                "json",
            ],
            "Wait for Apple notarization",
            timeout=1860,
        )
    )
    receipt["notarization_status"] = result.get("status", "Unknown")
    save_status(receipt)
    if result.get("status") != "Accepted":
        raise Stop("Apple did not accept the notarization submission")
    run(["/usr/bin/xcrun", "stapler", "staple", str(APP)], "Staple notarization ticket")
    run(["/usr/bin/xcrun", "stapler", "validate", str(APP)], "Validate stapled ticket")
    run(
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", str(APP)],
        "Verify stapled application",
    )
    run(
        ["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", str(APP)],
        "Assess application with Gatekeeper",
    )
    receipt["stapled_ticket_verified"] = True
    receipt["gatekeeper_accepted"] = True


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo:
    info.uid = 0
    info.gid = 0
    info.uname = "root"
    info.gname = "wheel"
    return info


def package_outputs(receipt: dict, version: str) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=False)
    updater = OUTPUT / f"PlateFold_{version}_macos-arm64.app.tar.gz"
    with tarfile.open(updater, "w:gz", dereference=False) as archive:
        archive.add(APP, arcname="PlateFold.app", recursive=True, filter=_tar_filter)

    dmg = OUTPUT / f"PlateFold_{version}_macos-arm64.dmg"
    with tempfile.TemporaryDirectory(prefix="platefold-dmg-", dir=STATE) as directory:
        staging = Path(directory)
        run(["/usr/bin/ditto", str(APP), str(staging / "PlateFold.app")], "Stage DMG app")
        (staging / "Applications").symlink_to("/Applications")
        run(
            [
                "/usr/bin/hdiutil",
                "create",
                "-volname",
                "PlateFold",
                "-srcfolder",
                str(staging),
                # LZMA (ULMO) opens on macOS 10.15 and later. The app inside is
                # arm64-only, and every Mac that can run it has macOS 11 or later.
                "-format",
                "ULMO",
                "-ov",
                str(dmg),
            ],
            "Create final DMG",
            timeout=900,
        )
    run(["/usr/bin/hdiutil", "verify", str(dmg)], "Verify final DMG", timeout=300)

    artifacts = [
        {"file": path.name, "size": path.stat().st_size, "sha256": digest(path)}
        for path in (dmg, updater)
    ]
    receipt["artifacts"] = artifacts
    receipt["updater_signed"] = False
    receipt["publicly_published"] = False
    release_receipt = OUTPUT / "MACOS-RELEASE-RECEIPT.json"
    release_receipt.write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    bundle = OUTPUT / f"PlateFold_{version}_macos-arm64-release-bundle.zip"
    with zipfile.ZipFile(bundle, "x", compression=zipfile.ZIP_STORED) as archive:
        archive.write(dmg, dmg.name)
        archive.write(updater, updater.name)
        archive.write(release_receipt, release_receipt.name)


def main() -> int:
    receipt = {
        "schema_version": 1,
        "product": "PlateFold",
        "architecture": "arm64",
        "certificate_sha256": EXPECTED_CERTIFICATE_SHA256,
        "developer_id_signed": False,
        "notarization_submitted": False,
        "stapled_ticket_verified": False,
        "gatekeeper_accepted": False,
        "updater_signed": False,
        "publicly_published": False,
    }
    try:
        source, signer, team = require_environment()
        receipt.update(source_commit=source, signer_sha1=signer, team_id=team)
        unsigned = validate_source(source)
        receipt["version"] = unsigned["version"]
        receipt["source_dirty"] = False
        receipt["unsigned_build_receipt_sha256"] = digest(UNSIGNED)
        receipt["build_inputs_sha256"] = unsigned.get("build_inputs_sha256")
        receipt["toolchain"] = unsigned.get("toolchain")
        receipt["bundled_browser"] = unsigned.get("bundled_browser")
        receipt["verified_downloads"] = unsigned.get("verified_downloads")
        receipt["signing_host"] = {
            "macos": run(["/usr/bin/sw_vers", "-productVersion"], "Read macOS version")
            .decode()
            .strip(),
            "machine": platform.machine(),
            "xcode": public_diagnostic(
                run(["/usr/bin/xcodebuild", "-version"], "Read Xcode version")
            ),
        }
        STATE.mkdir(parents=True, exist_ok=True)
        prepare_credentials(os.environ["PF_KEYCHAIN_PATH"], team)
        receipt["notarization_authentication_verified"] = True
        binaries = sign_application(signer, team)
        receipt["nested_macho_count"] = len(binaries) - 1
        receipt["signed_runtime_smoke"] = smoke_runtime()
        receipt["developer_id_signed"] = True
        save_status(receipt)
        notarize(receipt)
        package_outputs(receipt, unsigned["version"])
        save_status(receipt)
        print("PlateFold signed, notarized, stapled and packaged for private review.")
        return 0
    except Stop as error:
        receipt["failure"] = str(error)
        print(f"STOP: {error}")
    except (KeyError, OSError, ValueError, json.JSONDecodeError, tarfile.TarError):
        receipt["failure"] = "Input, tool output or local packaging operation failed"
        print("STOP: release packaging failed; credentials were not printed")
    finally:
        save_status(receipt)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
