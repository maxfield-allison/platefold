"""Build a native desktop package with a bundled worker and offline browser."""

import argparse
import base64
import binascii
import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from pathlib import Path

from platefold import __version__

try:
    from tools import build_deb, check_macos_minimum, native_downloads
    from tools.build_update_feed import artifact_binding, release_binding
except ModuleNotFoundError:  # Direct execution puts tools/, not the root, on sys.path.
    import build_deb
    import check_macos_minimum
    import native_downloads
    from build_update_feed import artifact_binding, release_binding

ROOT = Path(__file__).resolve().parents[1]

PLATFORMS = {
    "win32": {"name": "windows", "bundles": ["nsis"], "executable": "platefold.exe"},
    "linux": {"name": "linux", "bundles": ["appimage"], "executable": "platefold"},
    "darwin": {"name": "macos", "bundles": ["app", "dmg"], "executable": "platefold"},
}


def normalized_arch(machine=None):
    value = (machine or platform.machine()).lower()
    aliases = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "arm64",
    }
    return aliases.get(value, value.replace(" ", "-"))


def artifact_arch(platform_name, arch=None):
    arch = arch or normalized_arch()
    return "x64" if platform_name == "windows" and arch == "x86_64" else arch


def file_digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def exactly_one(folder, pattern):
    matches = sorted(folder.glob(pattern))
    if len(matches) != 1:
        names = ", ".join(path.name for path in matches) or "none"
        raise SystemExit(f"Expected one {pattern} under {folder}, found: {names}")
    return matches[0]


def bundled_browser(runtime):
    names = (
        "chrome-headless-shell.exe",
        "headless_shell.exe",
        "chrome.exe",
        "chrome-headless-shell",
        "headless_shell",
        "Chromium",
        "chrome",
    )
    matches = [path for name in names for path in runtime.rglob(name) if path.is_file()]
    if len(matches) != 1:
        found = ", ".join(path.relative_to(runtime).as_posix() for path in matches) or "none"
        raise SystemExit(f"Expected one bundled browser executable under {runtime}, found: {found}")
    return matches[0]


def source_state():
    if revision := os.environ.get("CI_COMMIT_SHA"):
        return revision, False
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    status = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=normal"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return revision, bool(status.strip())


def command_version(*command):
    executable = shutil.which(command[0])
    if not executable:
        raise SystemExit(f"Required build tool not found: {command[0]}")
    result = subprocess.run([executable, *command[1:]], check=True, capture_output=True, text=True)
    return (result.stdout or result.stderr).strip()


def check_macos_app(platform_info, release, bundled, stage_file=None):
    """Stop a Mac build whose binaries need a newer macOS than the app declares."""
    if platform_info["name"] != "macos" or not bundled:
        return
    record_build_stage(stage_file, "macos-minimum-version-check")
    app = exactly_one(release / "bundle/macos", "*.app")
    try:
        print(check_macos_minimum.require_supported_minimum(app)[1], flush=True)
    except ValueError as error:
        raise SystemExit(f"macOS minimum version check failed for {app.name}:\n{error}") from None


def ensure_new_destination(destination):
    if destination.exists():
        raise SystemExit(
            f"Preserving existing artifacts at {destination}; choose a new --output-dir"
        )


def record_build_stage(stage_file, stage):
    """Retain a non-sensitive phase marker for CI failure diagnosis."""
    if stage_file is None:
        return
    path = stage_file.resolve()
    allowed = (ROOT / "build").resolve()
    if not path.is_relative_to(allowed):
        raise SystemExit(f"Build stage file must be under {allowed}: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{stage}\n", encoding="ascii")


def sanitized_build_tail(output, limit=240):
    """Keep compiler diagnostics while removing private paths, URLs and secret-like lines."""
    replacements = [str(ROOT.resolve())]
    for name in ("CI_PROJECT_DIR", "USERPROFILE", "CARGO_HOME", "RUSTUP_HOME", "TEMP", "TMP"):
        if value := os.environ.get(name):
            replacements.append(value)
    sanitized = output
    for value in sorted(set(replacements), key=len, reverse=True):
        sanitized = sanitized.replace(value, "<private-path>")
        sanitized = sanitized.replace(value.replace("\\", "/"), "<private-path>")
    sanitized = re.sub(r"https?://\S+", "<url>", sanitized)
    lines = []
    for line in sanitized.splitlines():
        if re.search(r"(?i)(authorization|password|private.?key|secret|token)", line):
            lines.append("[redacted potentially sensitive line]")
        else:
            lines.append(line)
    return "\n".join(lines[-limit:]).rstrip() + "\n"


def short_path(path):
    """The Windows 8.3 form of path, which C compilers sometimes record instead."""
    if sys.platform != "win32":
        return None
    import ctypes

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, len(buffer))
    return buffer.value if 0 < length < len(buffer) and buffer.value != str(path) else None


def build_path_prefixes(root=ROOT):
    """Build-machine paths that must not be compiled into a published binary."""
    checkout = str(root.resolve())
    return [prefix for prefix in (checkout, short_path(checkout)) if prefix]


def path_mapping_environment(environment, root=ROOT):
    """Map the checkout out of compiled source paths.

    Rust records source paths for panic messages and debug information, and
    C code in dependencies records __FILE__. A CI build would otherwise publish
    the runner's checkout location, which names the private build project. A
    Cargo home inside the checkout is covered by the same prefix.
    check_binary_paths() refuses any build where a prefix survives.
    """
    prefixes = build_path_prefixes(root)
    flags = environment.pop("RUSTFLAGS", "").split()
    if encoded := environment.get("CARGO_ENCODED_RUSTFLAGS"):
        flags = encoded.split("\x1f")
    flags.extend(f"--remap-path-prefix={prefix}=platefold" for prefix in prefixes)
    environment["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(flags)
    if any(any(character.isspace() for character in prefix) for prefix in prefixes):
        # The C compiler flags are whitespace-separated; the binary check still applies.
        return environment
    if sys.platform == "win32":
        # MSVC's /d1trimfile removes the prefix from __FILE__.
        c_flags = [f"/d1trimfile:{prefix}\\" for prefix in prefixes]
    else:
        c_flags = [f"-ffile-prefix-map={prefix}=platefold" for prefix in prefixes]
    for name in ("CFLAGS", "CXXFLAGS"):
        environment[name] = " ".join(filter(None, [environment.get(name, ""), *c_flags]))
    return environment


def check_binary_paths(paths, prefixes=None, project_path=None):
    """Refuse a build whose binaries still name the build machine's checkout."""
    prefixes = build_path_prefixes() if prefixes is None else prefixes
    project_path = os.environ.get("CI_PROJECT_PATH") if project_path is None else project_path
    markers = set()
    for prefix in [*prefixes, *([project_path] if project_path else [])]:
        for form in {prefix, prefix.replace("\\", "/"), prefix.replace("/", "\\")}:
            for encoding in ("utf-8", "utf-16-le"):
                markers.add(form.lower().encode(encoding))
    for path in paths:
        content = Path(path).read_bytes().lower()
        if any(marker in content for marker in markers):
            raise SystemExit(
                f"{Path(path).name} contains the build checkout path; "
                "path mapping did not cover every compiler"
            )


def run_native_build(command, cwd, stage_file=None):
    environment = path_mapping_environment(os.environ.copy())
    # Tauri's macOS bundler checks the environment variable, independently of
    # the CLI's --ci flag, before deciding whether to skip Finder AppleScript.
    # Native builds run without an interactive Finder session, so keep the two
    # CI signals aligned on every supported platform.
    environment["CI"] = "true"
    if stage_file is None:
        subprocess.run(command, cwd=cwd, check=True, env=environment)
        return
    result = subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
    )
    if result.returncode == 0:
        print(result.stdout, end="", flush=True)
        return
    summary = stage_file.resolve().with_name("windows-tauri-summary.txt")
    summary.write_text(sanitized_build_tail(result.stdout), encoding="utf-8")
    raise SystemExit(
        f"Native desktop build failed with exit code {result.returncode}; "
        f"sanitized diagnostics retained at {summary}"
    )


def release_signing():
    """Return the maintainers' Windows release-signing module, if this tree has it.

    Release signing binds to the maintainers' own signing service and pipeline,
    so public source trees omit it and build unsigned packages.
    """
    name = f"{__package__}.windows_signing" if __package__ else "windows_signing"
    if importlib.util.find_spec(name) is None:
        return None
    return importlib.import_module(name)


def build_inputs():
    names = (
        "pyproject.toml",
        "uv.lock",
        "requirements-build.txt",
        "requirements-runtime.txt",
        "packaging/native-requirements/linux-x86_64.txt",
        "packaging/native-requirements/macos-arm64.txt",
        "packaging/native-requirements/windows-x86_64.txt",
        "packaging/native-downloads.json",
        "desktop/package-lock.json",
        "desktop/src-tauri/Cargo.lock",
        "desktop/src-tauri/Cargo.toml",
        "desktop/src-tauri/tauri.conf.json",
        "packaging/tauri-updater.conf.json",
        "packaging/tauri-windows-online.conf.json",
        "packaging/tauri-windows-offline.conf.json",
        "packaging/desktop-worker.spec",
        "packaging/runtime_trim.py",
        "packaging/licenses/cobble-BSD-2-Clause.txt",
        "packaging/windows-hooks.nsh",
        "packaging/update-policy.json",
        "desktop/src-tauri/examples/verify_updater_artifact.rs",
        "tools/build_deb.py",
    )
    signing = release_signing()
    if signing:
        names += signing.SOURCE_INPUTS
    inputs = {name: file_digest(ROOT / name) for name in names}
    for path in sorted((ROOT / "packaging/linux/deb").iterdir()):
        inputs[path.relative_to(ROOT).as_posix()] = file_digest(path)
    vendor = ROOT / "desktop/src-tauri/vendor/tauri-plugin-updater"
    for path in sorted(vendor.rglob("*")):
        if path.is_file():
            inputs[path.relative_to(ROOT).as_posix()] = file_digest(path)
    return inputs


def windows_payload(release):
    files = [release / "platefold-desktop.exe"]
    files.extend(path for path in (release / "runtime").rglob("*") if path.is_file())
    return [
        {
            "file": path.relative_to(release).as_posix(),
            "sha256": file_digest(path),
            "size": path.stat().st_size,
        }
        for path in sorted(files)
    ]


def windows_installer_gui(release):
    """Tauri patches its bundle marker for NSIS, then restores the portable EXE."""
    executable = release / "platefold-desktop.exe"
    contents = executable.read_bytes()
    marker = b"__TAURI_BUNDLE_TYPE_VAR_UNK"
    if contents.count(marker) != 1:
        raise SystemExit("Expected one Tauri bundle marker in the portable executable")
    patched = contents.replace(marker, b"__TAURI_BUNDLE_TYPE_VAR_NSS", 1)
    return {
        "file": executable.name,
        "sha256": hashlib.sha256(patched).hexdigest(),
        "size": len(patched),
        "transformation": "Tauri bundle marker UNK -> NSS",
    }


def archive_windows_staging():
    """Tauri overlays resources; preserve and replace its previous runtime staging."""
    staging = ROOT / "desktop/src-tauri/target/release/runtime"
    if not staging.exists():
        return None
    # Path.is_junction arrived in Python 3.12; retain the 3.11 build contract.
    reparse = getattr(staging.lstat(), "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if staging.is_symlink() or reparse or not staging.resolve().is_relative_to(ROOT):
        raise SystemExit(f"Refusing redirected runtime staging: {staging}")
    history = ROOT / "build"
    history.mkdir(exist_ok=True)
    archived = Path(tempfile.mkdtemp(prefix="previous-runtime-", dir=history)) / "runtime"
    if not archived.parent.resolve().is_relative_to(ROOT):
        raise SystemExit(f"Refusing runtime history outside the worktree: {archived}")
    staging.rename(archived)
    print(f"Preserved previous Tauri runtime staging at {archived}", flush=True)
    return archived


def validated_desktop_version(root=ROOT):
    """Fail before a costly native build when release metadata has drifted."""
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    python_version = pyproject["project"]["version"]
    expected = python_version.replace(".dev", "-dev.")
    package = json.loads((root / "desktop/package.json").read_text(encoding="utf-8"))
    package_lock = json.loads((root / "desktop/package-lock.json").read_text(encoding="utf-8"))
    tauri = json.loads((root / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    cargo = tomllib.loads((root / "desktop/src-tauri/Cargo.toml").read_text(encoding="utf-8"))
    versions = {
        "platefold.__version__": __version__,
        "desktop/package.json": package["version"],
        "desktop/package-lock.json": package_lock["version"],
        "desktop/package-lock.json package": package_lock["packages"][""]["version"],
        "desktop/src-tauri/tauri.conf.json": tauri["version"],
        "desktop/src-tauri/Cargo.toml": cargo["package"]["version"],
    }
    mismatches = {
        name: value
        for name, value in versions.items()
        if value != (python_version if name == "platefold.__version__" else expected)
    }
    if mismatches:
        details = ", ".join(f"{name}={value}" for name, value in mismatches.items())
        raise SystemExit(
            f"Version metadata does not match pyproject.toml {python_version}: {details}"
        )
    return expected


def sign_updater_artifact(path, version):
    """Sign a final updater artifact as its release; return its new .sig file.

    Tauri's signature names only a timestamp and file, so the update feed's
    metadata key alone would say which version the bytes are. This signature's
    Minisign trusted comment names the version, update target and file under
    the artifact key (build_update_feed.artifact_binding), and the app installs
    the file only when that matches what its feed offers. The key reaches the
    signer through its own environment variables, never argv.
    """
    try:
        bound_version, target = release_binding(path.name)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    if bound_version != version:
        raise SystemExit(f"{path.name} is not named for release {version}")
    command = [
        "cargo",
        "run",
        "--quiet",
        "--locked",
        "--release",
        "--manifest-path",
        str(ROOT / "desktop/src-tauri/Cargo.toml"),
        "--example",
        "sign_updater_artifact",
        "--",
        "--version",
        version,
        "--platform",
        target,
        str(path.resolve()),
    ]
    result = subprocess.run(
        command, cwd=ROOT, stdin=subprocess.DEVNULL, capture_output=True, text=True
    )
    if result.returncode:
        summary = ROOT / "build/updater-signing-summary.txt"
        summary.parent.mkdir(exist_ok=True)
        summary.write_text(sanitized_build_tail(result.stderr), encoding="utf-8")
        raise SystemExit(
            f"Signing {path.name} as release {version} failed; "
            f"sanitized diagnostics retained at {summary}"
        )
    try:
        receipt = json.loads(result.stdout)
    except ValueError:
        receipt = {}
    signature = Path(f"{path}.sig")
    if receipt.get("trusted_comment") != artifact_binding(version, target, path.name) or (
        not signature.is_file()
    ):
        raise SystemExit(f"The signer did not bind {path.name} to release {version}")
    return signature


def retain_updater_signature(source, target, downloads, enabled, version):
    if not enabled:
        return
    # Tauri's signature shows the build produced its updater artifact; the
    # released one is made for the artifact's final name and version.
    if not Path(f"{source}.sig").is_file():
        raise SystemExit(f"Missing signed updater artifact: {source}.sig")
    downloads.append(sign_updater_artifact(target, version))


def retain_macos_updater(release, destination, version, arch, downloads, enabled):
    if not enabled:
        return
    updater = exactly_one(release / "bundle/macos", "*.app.tar.gz")
    target = destination / f"PlateFold_{version}_macos-{arch}.app.tar.gz"
    shutil.copy2(updater, target)
    downloads.append(target)
    retain_updater_signature(updater, target, downloads, True, version)


WEBVIEW2_PUBLISHER = "Microsoft Corporation"
# Get-AuthenticodeSignature checks the file's signature and certificate chain.
# The path arrives through the environment, so it is never parsed as PowerShell.
AUTHENTICODE_SCRIPT = (
    "$s = Get-AuthenticodeSignature -LiteralPath $env:PLATEFOLD_VENDOR_FILE; "
    "$n = if ($s.SignerCertificate) { $s.SignerCertificate.GetNameInfo("
    "[Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false) } else { '' }; "
    "@{status = $s.Status.ToString(); signer = $n} | ConvertTo-Json -Compress"
)


def webview2_offline_installers(tools=None):
    """The WebView2 installers Tauri's bundler downloaded to embed in the offline setup.

    The bundler fetches the current Evergreen standalone installer from
    Microsoft without checking it, into <tools>/x64/<id>/, and embeds that
    file. Its bytes change with every WebView2 release, so it is authenticated
    by its Microsoft signature instead of a pinned digest.
    """
    if tools is None:
        try:
            tools = native_downloads.tauri_tools_directory()
        except (native_downloads.DownloadError, KeyError) as error:
            raise SystemExit(f"Cannot find Tauri's tool directory: {error}") from None
    return sorted((tools / "x64").glob("*/MicrosoftEdgeWebView2RuntimeInstaller*.exe"))


def authenticode_signer(path):
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", AUTHENTICODE_SCRIPT],
        env={**os.environ, "PLATEFOLD_VENDOR_FILE": str(path)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if result.returncode:
        raise SystemExit(f"Could not read the Authenticode signature of {path.name}")
    return json.loads(result.stdout)


def verify_webview2_installers(require=True, tools=None, signer=authenticode_signer):
    """Refuse any cached WebView2 installer that is not validly signed by Microsoft."""
    installers = webview2_offline_installers(tools)
    if require and not installers:
        raise SystemExit("The offline installer was built without a WebView2 installer to check")
    records = []
    for path in installers:
        if path.is_symlink() or not path.is_file():
            raise SystemExit(f"Unexpected WebView2 installer entry: {path}")
        signature = signer(path)
        if signature.get("status") != "Valid" or signature.get("signer") != WEBVIEW2_PUBLISHER:
            raise SystemExit(
                f"{path.parent.name}/{path.name} is not validly signed by {WEBVIEW2_PUBLISHER}; "
                "delete it from Tauri's cache and rebuild"
            )
        records.append(
            {
                "file": f"{path.parent.name}/{path.name}",
                "sha256": file_digest(path),
                "size": path.stat().st_size,
                "signer": WEBVIEW2_PUBLISHER,
            }
        )
    return records


def build_windows_installers(command, desktop, version, variants, stage_file=None, signing=None):
    """Build each UI-runtime policy over the same frozen Python/PDF runtime.

    Changing Tauri configuration can change the compiled GUI. Retain each
    installer's bytes and its own installed GUI hash before the next build.
    """
    release = desktop / "src-tauri/target/release"
    history = ROOT / "build"
    history.mkdir(exist_ok=True)
    retained = Path(tempfile.mkdtemp(prefix="windows-installers-", dir=history))
    installers = {}
    runtime_payload = None
    for variant in variants:
        record_build_stage(stage_file, f"desktop-tauri-{variant}")
        overlay = f"../packaging/tauri-windows-{variant}.conf.json"
        signing_overlay = ["--config", signing.overlay(variant)] if signing else []
        build_command = [
            *command,
            "--config",
            overlay,
            *signing_overlay,
            "--bundles",
            "nsis",
            "--",
            "--locked",
        ]
        if signing:
            signing.build(run_native_build, build_command, desktop, stage_file)
        else:
            run_native_build(build_command, desktop, stage_file)
        payload = [item for item in windows_payload(release) if item["file"].startswith("runtime/")]
        if runtime_payload is not None and payload != runtime_payload:
            raise SystemExit("Windows installer variants must contain the same worker/PDF runtime")
        runtime_payload = payload
        source = release / "bundle/nsis" / f"PlateFold_{version}_x64-setup.exe"
        target = retained / f"PlateFold_{version}_x64-{variant}-setup.exe"
        if signing:
            signing.verify_installer(source)
        shutil.copy2(source, target)
        signature = Path(f"{source}.sig")
        if signature.is_file():
            shutil.copy2(signature, Path(f"{target}.sig"))
        installers[variant] = {
            "path": target,
            "gui": signing.installed_gui(variant) if signing else windows_installer_gui(release),
        }
    return installers


def retain_windows_installers(windows_installers, destination, downloads, include_updater, version):
    if not windows_installers:
        raise SystemExit("Windows bundles require per-installer build evidence")
    variants = {}
    for variant, built in windows_installers.items():
        installer = built["path"]
        target = destination / installer.name
        shutil.copy2(installer, target)
        downloads.append(target)
        retain_updater_signature(installer, target, downloads, include_updater, version)
        variants[variant] = {
            "file": target.name,
            "webview_install_mode": (
                "downloadBootstrapper" if variant == "online" else "offlineInstaller"
            ),
            "pdf_renderer": "bundled",
            "gui": built["gui"],
        }
    return variants


def package_downloads(
    platform_info,
    version,
    include_bundle=True,
    include_updater=False,
    output_dir=None,
    windows_installers=None,
    evidence=None,
):
    release = ROOT / "desktop/src-tauri/target/release"
    destination = output_dir if output_dir is not None else ROOT / "dist/desktop"
    ensure_new_destination(destination)
    destination.mkdir(parents=True)
    arch = normalized_arch()
    download_arch = artifact_arch(platform_info["name"], arch)
    downloads = []
    debian_package = None
    if platform_info["name"] == "windows":
        archive = destination / f"PlateFold_{version}_windows-{download_arch}.zip"
        with zipfile.ZipFile(
            archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as bundle:
            bundle.write(release / "platefold-desktop.exe", "PlateFold/platefold-desktop.exe")
            for path in sorted((release / "runtime").rglob("*")):
                if path.is_file():
                    bundle.write(path, "PlateFold/" + path.relative_to(release).as_posix())
            for source, name in [
                ("LICENSE", "LICENSE"),
                ("docs/windows-desktop.md", "README.md"),
                ("docs/automation.md", "automation.md"),
                ("desktop/THIRD_PARTY.md", "THIRD_PARTY.md"),
            ]:
                bundle.write(ROOT / source, "PlateFold/" + name)
        downloads.append(archive)
    elif not include_bundle:
        raise SystemExit("--no-installer is only supported by the Windows portable build")
    elif platform_info["name"] == "linux":
        source = exactly_one(release / "bundle/appimage", "*.AppImage")
        target = destination / f"PlateFold_{version}_linux-{download_arch}.AppImage"
        shutil.copy2(source, target)
        downloads.append(target)
        retain_updater_signature(source, target, downloads, include_updater, version)
        for source_name, target_name in (
            ("platefold.apparmor", "platefold.apparmor"),
            ("README.md", "LINUX-INSTALL.md"),
        ):
            target = destination / target_name
            shutil.copy2(ROOT / "packaging/linux" / source_name, target)
            downloads.append(target)
        # The .deb installs the same program and runtime the AppImage carries.
        epoch = build_deb.source_date_epoch()
        (ROOT / "build").mkdir(exist_ok=True)
        package = build_deb.build(
            release / "platefold-desktop",
            release / "runtime",
            version,
            destination,
            epoch=epoch,
            workdir=ROOT / "build",
        )
        downloads.append(package)
        debian_package = {
            "file": package.name,
            "package_version": build_deb.debian_version(version),
            "source_date_epoch": epoch,
            "apparmor_profile": build_deb.PROFILE,
            "bundled_browser": f"/{build_deb.BROWSER.as_posix()}",
        }
    else:
        source = exactly_one(release / "bundle/dmg", "*.dmg")
        target = destination / f"PlateFold_{version}_macos-{download_arch}.dmg"
        shutil.copy2(source, target)
        downloads.append(target)
        retain_macos_updater(
            release, destination, version, download_arch, downloads, include_updater
        )
    installer_variants = {}
    if include_bundle and platform_info["name"] == "windows":
        installer_variants = retain_windows_installers(
            windows_installers, destination, downloads, include_updater, version
        )
    artifacts = []
    for path in sorted(downloads):
        digest = file_digest(path)
        artifacts.append({"file": path.name, "sha256": digest, "size": path.stat().st_size})
        print(f"{path}: {path.stat().st_size / 1024**2:.1f} MiB")
    (destination / "SHA256SUMS").write_text(
        "".join(f"{item['sha256']}  {item['file']}\n" for item in artifacts), encoding="utf-8"
    )
    runtime = release / "runtime"
    browser = bundled_browser(runtime)
    source_commit, source_dirty = source_state()
    receipt = {
        "schema_version": 2 if installer_variants else 1,
        "product": "PlateFold",
        "version": version,
        "source_commit": source_commit,
        "source_dirty": source_dirty,
        "build_inputs_sha256": build_inputs(),
        "build_environment": {
            distribution.metadata["Name"]: distribution.version
            for distribution in importlib.metadata.distributions()
        },
        "platform": platform_info["name"],
        "architecture": arch,
        "updater_artifacts": include_updater,
        "artifacts": artifacts,
        "payload": windows_payload(release) if platform_info["name"] == "windows" else [],
        "installer_variants": installer_variants,
        **({"debian_package": debian_package} if debian_package else {}),
        "bundled_browser": {
            "file": browser.relative_to(runtime).as_posix(),
            "source_sha256": file_digest(browser),
        },
        **(evidence or {}),
        "toolchain": {
            "python": platform.python_version(),
            "node": command_version("node", "--version"),
            "npm": command_version("npm.cmd" if sys.platform == "win32" else "npm", "--version"),
            "rustc": command_version("rustc", "--version"),
            "cargo": command_version("cargo", "--version"),
            "pyinstaller": importlib.metadata.version("pyinstaller"),
        },
    }
    (destination / "BUILD-RECEIPT.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def validate_updater_policy():
    policy = json.loads((ROOT / "packaging/update-policy.json").read_text(encoding="utf-8"))
    for name in ("public_keys", "artifact_public_keys"):
        keys = policy.get(name)
        if (
            not isinstance(keys, list)
            or not 1 <= len(keys) <= 4
            or not all(isinstance(key, str) and key.strip() for key in keys)
        ):
            raise SystemExit("Updater builds require committed metadata and artifact public keys")
    if set(policy["public_keys"]) & set(policy["artifact_public_keys"]):
        raise SystemExit("Updater metadata and artifact keys must be separate")
    if not policy.get("stable") and not policy.get("prerelease"):
        raise SystemExit("Updater builds require a committed channel endpoint")
    return policy


def updater_build_config():
    """Supply Tauri's boxed public key from the same artifact pin used at runtime.

    The CLI expects base64 of a complete Minisign public-key document, not the
    raw key line stored in our policy. The first artifact pin is the build key;
    remaining pins can continue to validate previously released artifacts.
    """
    policy = validate_updater_policy()
    key = policy["artifact_public_keys"][0]
    try:
        decoded = base64.b64decode(key, validate=True)
    except (ValueError, binascii.Error):
        raise SystemExit("Invalid updater artifact public key") from None
    if len(decoded) != 42 or decoded[:2] != b"Ed":
        raise SystemExit("Invalid updater artifact public key")
    document = f"untrusted comment: PlateFold updater public key\n{key}\n"
    config = json.loads((ROOT / "packaging/tauri-updater.conf.json").read_text(encoding="utf-8"))
    config.setdefault("plugins", {}).setdefault("updater", {})["pubkey"] = base64.b64encode(
        document.encode("utf-8")
    ).decode("ascii")
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", suffix=".json", prefix="tauri-updater-", dir=build, delete=False
    ) as output:
        json.dump(config, output)
    return Path(output.name).resolve()


def preflight_updater_key(npm):
    """Catch a miswired CI key before building; final bytes still require verification."""
    policy = validate_updater_policy()
    expected = base64.b64decode(policy["artifact_public_keys"][0], validate=True)[2:10]
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="updater-key-check-", dir=build) as directory:
        fixture = Path(directory) / "key-check.txt"
        fixture.write_text("PlateFold updater key identity check\n", encoding="utf-8")
        try:
            result = subprocess.run(
                [npm, "run", "tauri", "--", "signer", "sign", str(fixture)],
                cwd=ROOT / "desktop",
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=60,
            )
        except subprocess.TimeoutExpired:
            raise SystemExit("Updater key check timed out before building") from None
        if result.returncode:
            # Signer output can contain environment-derived values. Never relay it.
            raise SystemExit(
                "Updater key check failed. Check the artifact signing key and its password "
                "in the protected CI variables."
            )
        try:
            encoded = fixture.with_suffix(".txt.sig").read_text(encoding="utf-8")
            document = base64.b64decode(encoded.strip(), validate=True).decode("utf-8")
            signature = base64.b64decode(document.splitlines()[1], validate=True)
            if len(signature) != 74 or signature[:2] not in (b"Ed", b"ED"):
                raise ValueError("Invalid signature header")
        except (OSError, ValueError, IndexError, UnicodeError):
            raise SystemExit("Updater key check produced an invalid signature") from None
        if signature[2:10] != expected:
            raise SystemExit(
                "Updater signing key does not match the configured artifact key. "
                "Set both TAURI_SIGNING_PRIVATE_KEY and TAURI_SIGNING_PRIVATE_KEY_PASSWORD "
                "from the artifact secret store, not the metadata store."
            )
    print("Updater signing key identity matches the configured artifact key", flush=True)


def verify_updater_downloads(destination):
    receipt_path = destination / "BUILD-RECEIPT.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    artifacts = {entry["file"]: entry for entry in receipt["artifacts"]}
    signatures = [name for name in artifacts if name.endswith(".sig")]
    if not signatures:
        raise SystemExit("Updater build produced no final artifact signatures")
    for item in receipt.get("installer_variants", {}).values():
        if item["file"] + ".sig" not in signatures:
            raise SystemExit("Every Windows installer variant needs an updater signature")
    verified = []
    for signature in signatures:
        artifact = signature[:-4]
        if artifact not in artifacts or Path(artifact).name != artifact:
            raise SystemExit("Updater signature has no matching final artifact")
        try:
            version, target = release_binding(artifact)
        except ValueError as error:
            raise SystemExit(str(error)) from None
        if version != receipt.get("version"):
            raise SystemExit(f"{artifact} is not named for release {receipt.get('version')}")
        command = [
            "cargo",
            "run",
            "--locked",
            "--release",
            "--manifest-path",
            str(ROOT / "desktop/src-tauri/Cargo.toml"),
            "--example",
            "verify_updater_artifact",
            "--",
            "--binding",
            version,
            target,
            str((destination / artifact).resolve()),
            str((destination / signature).resolve()),
        ]
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        if result.returncode:
            summary = ROOT / "build/updater-verification-summary.txt"
            summary.parent.mkdir(exist_ok=True)
            summary.write_text(sanitized_build_tail(result.stderr), encoding="utf-8")
            raise SystemExit(
                "Final updater signature failed verification against the compiled policy; "
                f"sanitized diagnostics retained at {summary}"
            )
        proof = json.loads(result.stdout)
        expected = artifacts[artifact]
        if (
            proof.get("verified") is not True
            or proof.get("key_kind") != "artifact"
            or not proof.get("public_key")
            or proof.get("file") != artifact
            or proof.get("sha256") != expected["sha256"]
            or proof.get("size") != expected["size"]
            or proof.get("binding") != {"version": version, "platform": target, "file": artifact}
            or proof.get("trusted_comment") != artifact_binding(version, target, artifact)
        ):
            raise SystemExit("Updater verification does not match the final package receipt")
        verified.append(proof)
    receipt["updater_signatures"] = verified
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def check_verified_downloads(receipt_path):
    """Build only with the browser, and on Linux the AppImage tools, that were verified.

    Returns the receipt fields BUILD-RECEIPT.json records. Local builds without a
    receipt record that they bundled an unverified browser.
    """
    if receipt_path is None:
        return {"verified_downloads": None}
    from platefold.browser import find_bundled

    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if "rust_toolchain" not in receipt:
        raise SystemExit("The verified-downloads receipt lacks a verified Rust toolchain")
    try:
        environment = native_downloads.check_receipt(receipt, find_bundled())
    except native_downloads.DownloadError as error:
        raise SystemExit(str(error)) from None
    # The AppImage runtime appimagetool would otherwise download unverified.
    os.environ.update(environment)
    browser = receipt["browser"]
    record = {
        "platform": receipt["platform"],
        "manifest_sha256": receipt["manifest_sha256"],
        "playwright": receipt["playwright"],
        "browser_archives": browser["archives"],
        "browser_tree_sha256": browser["tree_sha256"],
        "browser_executable": browser["executable"],
        "rust_toolchain": receipt["rust_toolchain"],
    }
    if "appimage_tools" in receipt:
        record["appimage_tools"] = receipt["appimage_tools"]["files"]
    return {"verified_downloads": record}


def validate_build_args(parser, args, platform_info):
    if not platform_info:
        parser.error(f"Unsupported build platform: {sys.platform}")
    if args.no_installer and sys.platform != "win32":
        parser.error("--no-installer is only supported on Windows")
    if args.no_installer and args.updater_artifacts:
        parser.error("--updater-artifacts requires an installer bundle")
    if args.updater_artifacts and not os.environ.get("TAURI_SIGNING_PRIVATE_KEY"):
        parser.error("--updater-artifacts requires TAURI_SIGNING_PRIVATE_KEY")
    if (
        args.gitlab_windows_signing
        and not args.signing_preflight_only
        and not args.updater_artifacts
    ):
        parser.error("GitLab signed candidates require --updater-artifacts")
    signing_requested = args.attended_windows_signing or args.gitlab_windows_signing
    if (
        (args.updater_artifacts or signing_requested)
        and not args.signing_preflight_only
        and not args.verified_downloads
    ):
        parser.error("Signed and updater builds require --verified-downloads")
    if args.signing_preflight_only and not signing_requested:
        parser.error("Signing preflight requires an explicit signing mode")
    if signing_requested and (sys.platform != "win32" or args.no_installer):
        parser.error("Windows signing requires native Windows installer builds")


def attended_signing(args, version):
    signing = None
    if args.attended_windows_signing or args.gitlab_windows_signing:
        module = release_signing()
        if module is None:
            raise SystemExit("Windows release signing is not part of this source tree")
        (ROOT / "build").mkdir(exist_ok=True)
        signing = module.AttendedSigning(
            ROOT,
            version,
            args.approved_source_commit or "",
            args.signtool,
            args.signing_dlib,
            mode="gitlab-workload" if args.gitlab_windows_signing else "attended",
        )
    return signing


def retain_signing_evidence(args, signing):
    if args.updater_artifacts:
        verify_updater_downloads(args.output_dir)
    if signing:
        signing.retain(args.output_dir)
        receipt_path = args.output_dir / "BUILD-RECEIPT.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["windows_signing"] = {
            "mode": signing.context["mode"],
            "evidence": "WINDOWS-SIGNATURES.json",
            "sha256": file_digest(args.output_dir / "WINDOWS-SIGNATURES.json"),
        }
        receipt_path.write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def build_native(args, platform_info, command, desktop, version, signing, evidence):
    """Run the Tauri build; return the Windows installers it retained, if any."""
    if platform_info["name"] == "windows" and not args.no_installer:
        variants = (
            ("online", "offline")
            if args.windows_installers == "both"
            else (args.windows_installers,)
        )
        # A WebView2 installer an earlier build left in Tauri's cache is embedded
        # as it is, so check it before building as well as after.
        verify_webview2_installers(require=False)
        installers = build_windows_installers(
            command, desktop, version, variants, args.stage_file, signing=signing
        )
        if "offline" in variants:
            evidence["webview2_offline_installers"] = verify_webview2_installers()
        return installers
    command.extend(
        ["--no-bundle"] if args.no_installer else ["--bundles", ",".join(platform_info["bundles"])]
    )
    command.extend(["--", "--locked"])
    record_build_stage(args.stage_file, "desktop-tauri-build")
    run_native_build(command, desktop, args.stage_file)
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--signing-preflight-only",
        action="store_true",
        help="Check signing context and tools without building or signing",
    )
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument(
        "--attended-windows-signing",
        action="store_true",
        help="Explicit owner-attended Azure signing; refuses CI",
    )
    modes.add_argument(
        "--gitlab-windows-signing",
        action="store_true",
        help="Protected GitLab workload signing; requires the enrolled isolated runner",
    )
    parser.add_argument(
        "--approved-source-commit", help="Exact reviewed commit required for attended signing"
    )
    parser.add_argument("--signtool", type=Path, help="Existing Microsoft x64 signtool.exe")
    parser.add_argument("--signing-dlib", type=Path, help="Existing Azure.CodeSigning.Dlib.dll")
    parser.add_argument(
        "--windows-installers",
        choices=("both", "online", "offline"),
        default="both",
        help="Windows setup variants (default: both); PDF renderer is bundled in both",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist/desktop",
        help="New artifact directory; existing directories are never overwritten",
    )
    parser.add_argument(
        "--no-installer",
        action="store_true",
        help="Windows only: build the portable ZIP without the NSIS installer",
    )
    parser.add_argument(
        "--updater-artifacts",
        action="store_true",
        help="Create and retain Tauri updater artifacts signed by TAURI_SIGNING_PRIVATE_KEY",
    )
    parser.add_argument(
        "--verified-downloads",
        type=Path,
        help=(
            "Receipt from tools/native_downloads.py prepare; the build bundles only that "
            "verified browser (and on Linux uses its AppImage tools) and records it"
        ),
    )
    parser.add_argument(
        "--stage-file",
        type=Path,
        help="Optional non-sensitive phase marker retained under build/ for CI diagnosis",
    )
    args = parser.parse_args()
    platform_info = PLATFORMS.get(sys.platform)
    validate_build_args(parser, args, platform_info)
    version = validated_desktop_version()
    ensure_new_destination(args.output_dir)
    updater_config = updater_build_config() if args.updater_artifacts else None
    signing = attended_signing(args, version)
    if args.signing_preflight_only:
        print(
            "Signing context and Microsoft tools verified; no application signed or workload token exchanged."
        )
        return
    npm = shutil.which("npm.cmd" if sys.platform == "win32" else "npm")
    if not npm:
        parser.error("Node.js/npm is required to build the desktop interface")
    desktop = ROOT / "desktop"
    record_build_stage(args.stage_file, "desktop-npm-install")
    subprocess.run([npm, "ci"], cwd=desktop, check=True)
    if args.updater_artifacts:
        record_build_stage(args.stage_file, "updater-key-check")
        preflight_updater_key(npm)
    evidence = check_verified_downloads(args.verified_downloads)
    record_build_stage(args.stage_file, "desktop-worker-bundle")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(desktop / "src-tauri"),
            "--workpath",
            str(ROOT / "build/desktop-worker"),
            str(ROOT / "packaging/desktop-worker.spec"),
        ],
        cwd=ROOT,
        check=True,
    )
    if signing:
        signing.sign_worker()
    command = [npm, "run", "tauri", "--", "build", "--ci"]
    if updater_config:
        command.extend(["--config", str(updater_config)])
    if platform_info["name"] == "windows":
        archive_windows_staging()
    windows_installers = build_native(
        args, platform_info, command, desktop, version, signing, evidence
    )
    release = desktop / "src-tauri/target/release"
    executables = [
        path for path in release.glob("platefold-desktop*") if path.suffix in ("", ".exe")
    ]
    if not executables:
        raise SystemExit("No desktop executable found to check for build paths")
    check_binary_paths(executables)
    check_macos_app(platform_info, release, not args.no_installer, args.stage_file)
    if signing:
        signing.sign_portable()
    record_build_stage(args.stage_file, "desktop-package-collection")
    package_downloads(
        platform_info,
        version,
        include_bundle=not args.no_installer,
        include_updater=args.updater_artifacts,
        output_dir=args.output_dir,
        windows_installers=windows_installers,
        evidence=evidence,
    )
    retain_signing_evidence(args, signing)
    record_build_stage(args.stage_file, "desktop-complete")


if __name__ == "__main__":
    main()
