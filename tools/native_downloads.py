#!/usr/bin/env python3
"""Verified network inputs for the native desktop builds.

A desktop package bundles a Playwright Chromium headless shell, and on Linux
Tauri's bundler runs linuxdeploy, its plugins and appimagetool, whose AppImage
runtime becomes the first bytes of the shipped AppImage. All of them come from
the network at build time. packaging/native-downloads.json records the reviewed
SHA-256 and size of each one for each build platform.

`prepare` downloads them, refuses any other bytes, and only then:

* lets Playwright install the browser from a loopback server that serves only
  the verified archives, into an empty PLAYWRIGHT_BROWSERS_PATH, and checks
  that every installed file is the corresponding archive member;
* on Linux, writes the AppImage tools where Tauri's bundler looks for them, so
  it never downloads its own copies.

Each download is read once, checked in memory and deleted, and everything
after the check uses those bytes: the loopback server serves them, the AppImage
tools are written from them, and the archive members an installed browser must
match are listed from them before Playwright runs. No file that another process
could change sits between the check and its use.

It writes a receipt of what it verified. build_desktop.py --verified-downloads
checks that receipt again before building and copies it into BUILD-RECEIPT.json.

    python tools/native_downloads.py prepare --receipt build/native-downloads.json \
        --rust-receipt build/rust-toolchain.json
    python tools/native_downloads.py pin    # after reviewing a deliberate upstream change

`pin` recomputes the digests from the listed URLs and rewrites the manifest;
review the change like any other dependency update before committing it.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import importlib.metadata
import io
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

try:
    from tools import download_retry, rust_toolchain
except ModuleNotFoundError:  # `python tools/native_downloads.py`
    import download_retry
    import rust_toolchain

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packaging" / "native-downloads.json"
MAX_DOWNLOAD = 400 * 1024 * 1024
# Playwright writes these into each browser directory after extracting it.
MARKERS = frozenset({"INSTALLATION_COMPLETE", "DEPENDENCIES_VALIDATED"})
# Overrides that would send Playwright anywhere but the loopback server.
DOWNLOAD_OVERRIDES = (
    "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
    "PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST",
    "PLAYWRIGHT_WEBKIT_DOWNLOAD_HOST",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


class DownloadError(ValueError):
    """A download, installation or receipt that does not match the reviewed manifest."""


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def manifest_digest(path: Path = MANIFEST) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_manifest(path: Path = MANIFEST) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def host_platform(system: str | None = None, machine: str | None = None) -> str:
    system = system or sys.platform
    machine = (machine or platform.machine()).lower()
    if system.startswith("linux") and machine in ("x86_64", "amd64"):
        return "linux-x86_64"
    if system == "win32" and machine in ("amd64", "x86_64"):
        return "windows-x86_64"
    if system == "darwin" and machine in ("arm64", "aarch64"):
        return "macos-arm64"
    raise DownloadError(f"No reviewed native downloads for {system} {machine}")


# --- download and verify -------------------------------------------------------------


def http_download(
    url: str, destination: Path, opener=urllib.request.urlopen, sleep=time.sleep
) -> None:
    """Download url to the new file destination, retrying transient network failures."""
    request = urllib.request.Request(url, headers={"User-Agent": "platefold-native-build"})
    try:
        download_retry.save(
            request,
            destination,
            name=url,
            limit=MAX_DOWNLOAD,
            timeout=120,
            opener=opener,
            sleep=sleep,
        )
    except download_retry.TooLarge:
        raise DownloadError(f"{url} exceeds the download limit") from None
    except download_retry.Failed as failure:
        raise DownloadError(f"Download of {url} {failure}: {failure.error}") from None


def fetch_verified(item: dict, destination: Path, download: Callable = http_download) -> bytes:
    """Download one manifest item and return its bytes, refusing any but the reviewed ones.

    The download is read once and removed, so the bytes returned are the bytes
    checked.
    """
    download(item["url"], destination)
    try:
        data = destination.read_bytes()
    finally:
        destination.unlink()
    size, actual = len(data), hashlib.sha256(data).hexdigest()
    if size != item["size"] or actual != item["sha256"]:
        raise DownloadError(
            f"{item['url']} is not the reviewed download: expected {item['size']} bytes "
            f"with SHA-256 {item['sha256']}, received {size} bytes with SHA-256 {actual}. "
            "Nothing from it was installed. If upstream changed it deliberately, review the "
            "new bytes and run tools/native_downloads.py pin."
        )
    return data


def archive_listing(data: bytes, label: str) -> dict[str, str]:
    """SHA-256 of every file member of a zip archive, by its relative path."""
    listing = {}
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename
            if name.startswith("/") or ".." in name.split("/"):
                raise DownloadError(f"Unsafe archive member in {label}: {name}")
            with archive.open(info) as member:
                listing[name] = hashlib.file_digest(member, "sha256").hexdigest()
    return listing


def tree_listing(directory: Path) -> dict[str, str]:
    """SHA-256 of every file under directory except Playwright's markers."""
    listing = {}
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise DownloadError(f"Unexpected link in the browser payload: {path}")
        if path.is_file() and not (path.parent == directory and path.name in MARKERS):
            listing[path.relative_to(directory).as_posix()] = digest(path)
    return listing


def listing_digest(listing: dict[str, str]) -> str:
    text = "".join(f"{listing[name]}  {name}\n" for name in sorted(listing))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --- the browser ---------------------------------------------------------------------


def installed_playwright() -> tuple[str, Path]:
    """The installed Playwright's version and the browsers.json its installer follows."""
    import playwright

    browsers_json = Path(playwright.__file__).parent / "driver/package/browsers.json"
    return importlib.metadata.version("playwright"), browsers_json


def check_playwright(manifest: dict, platform_key: str, installed=installed_playwright) -> None:
    """The installed Playwright must request exactly the pinned browser revisions."""
    expected = manifest["playwright"]
    version, browsers_json = installed()
    if version != expected["version"]:
        raise DownloadError(
            f"Playwright {version} is installed; the reviewed browser payloads are for "
            f"{expected['version']}"
        )
    browsers = {
        item["name"]: item for item in json.loads(browsers_json.read_text("utf-8"))["browsers"]
    }
    names = ["chromium-headless-shell", "ffmpeg"]
    if platform_key.startswith("windows"):
        names.append("winldd")
    for name in names:
        pinned = expected["browsers"][name]
        actual = browsers.get(name, {})
        if actual.get("revisionOverrides"):
            raise DownloadError(f"Playwright {version} overrides the {name} revision on some hosts")
        for field, value in pinned.items():
            if actual.get(field) != value:
                raise DownloadError(
                    f"Playwright {version} wants {name} {field} {actual.get(field)}, "
                    f"the reviewed payload is {value}"
                )


@contextlib.contextmanager
def loopback_server(files: dict[str, bytes]) -> Iterator[tuple[str, list[str]]]:
    """Serve exactly these URL paths from 127.0.0.1; record every request."""
    requested: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = self.path.lstrip("/")
            requested.append(path)
            data = files.get(path)
            if data is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", requested
    finally:
        server.shutdown()
        server.server_close()


def run_playwright_install(host: str, browsers: Path) -> None:
    environment = {
        name: value for name, value in os.environ.items() if name not in DOWNLOAD_OVERRIDES
    }
    environment["PLAYWRIGHT_DOWNLOAD_HOST"] = host
    environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "playwright",
            "install",
            "--only-shell",
            "--no-progress",
            "chromium",
        ],
        env=environment,
        stdin=subprocess.DEVNULL,
        check=True,
        timeout=1800,
    )


def browsers_path() -> Path:
    value = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not value or value == "0":
        raise DownloadError("Set PLAYWRIGHT_BROWSERS_PATH to a new, empty directory for the build")
    path = Path(value).expanduser().resolve()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise DownloadError(
            f"{path} is not empty. Install the verified browser into a new directory, so no "
            "earlier, unverified browser can be bundled."
        )
    return path


def install_browser(
    manifest: dict,
    platform_key: str,
    work: Path,
    download: Callable = http_download,
    installer: Callable[[str, Path], None] = run_playwright_install,
) -> dict:
    items = manifest["platforms"][platform_key]["browser"]
    target = browsers_path()
    archives, members = {}, {}
    for index, item in enumerate(items):
        data = fetch_verified(item, work / f"browser-{index}.zip", download)
        archives[item["path"]] = data
        members[item["path"]] = archive_listing(data, item["path"])
    with loopback_server(archives) as (host, requested):
        installer(host, target)
    if sorted(requested) != sorted(archives):
        raise DownloadError(f"Playwright requested {requested}, not the reviewed archives")
    expected_directories = {item["directory"] for item in items}
    present = {path.name for path in target.iterdir()} - {".links"}
    if present != expected_directories:
        raise DownloadError(f"Unexpected browser directories after install: {sorted(present)}")
    for item in items:
        directory = target / item["directory"]
        if not (directory / "INSTALLATION_COMPLETE").is_file():
            raise DownloadError(f"Playwright did not finish installing {item['directory']}")
        if tree_listing(directory) != members[item["path"]]:
            raise DownloadError(f"{item['directory']} differs from its verified archive")
    executable = target / manifest["platforms"][platform_key]["executable"]
    shell = target / items[0]["directory"]
    return {
        "browsers_path": str(target),
        "archives": [
            {key: item[key] for key in ("path", "url", "sha256", "size")} for item in items
        ],
        "directory": items[0]["directory"],
        "tree_sha256": listing_digest(tree_listing(shell)),
        "executable": {
            "file": executable.relative_to(target).as_posix(),
            "sha256": digest(executable),
        },
    }


# --- AppImage tools (Linux) ----------------------------------------------------------


TAURI_PROJECT = ROOT / "desktop" / "src-tauri"


def cargo_target_directory(project: Path = TAURI_PROJECT) -> Path:
    """The Cargo target directory, as Tauri's CLI reads it from cargo metadata."""
    result = subprocess.run(
        ["cargo", "metadata", "--format-version", "1", "--no-deps"],
        cwd=project,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    if result.returncode:
        raise DownloadError(f"cargo metadata failed in {project}: {result.stderr.strip()}")
    return Path(json.loads(result.stdout)["target_directory"])


def tauri_tools_directory(project: Path = TAURI_PROJECT, target=cargo_target_directory) -> Path:
    """Where Tauri's bundler looks for, and otherwise downloads, its bundling tools.

    With bundle.useLocalToolsDir (this project sets it) that is <target>/.tauri;
    otherwise the user cache, as tauri-bundler's dirs::cache_dir() gives it.
    """
    config = json.loads((project / "tauri.conf.json").read_text(encoding="utf-8"))
    if config.get("bundle", {}).get("useLocalToolsDir"):
        return target(project) / ".tauri"
    if sys.platform == "win32":
        return Path(os.environ["LOCALAPPDATA"]) / "tauri"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "tauri"
    cache = os.environ.get("XDG_CACHE_HOME")
    base = Path(cache) if cache and Path(cache).is_absolute() else Path.home() / ".cache"
    return base / "tauri"


def place_appimage_tools(
    manifest: dict, platform_key: str, work: Path, download: Callable = http_download
) -> dict:
    items = manifest["platforms"][platform_key].get("appimage_tools", [])
    directory = tauri_tools_directory()
    directory.mkdir(parents=True, exist_ok=True)
    placed = []
    for item in items:
        verified = fetch_verified(item, work / item["file"], download)
        target = directory / item["file"]
        # Replace whatever an earlier build left, never reuse it.
        staged = directory / f".{item['file']}.verified"
        staged.write_bytes(verified)
        staged.chmod(0o755)
        os.replace(staged, target)
        placed.append({key: item[key] for key in ("file", "url", "sha256", "size")})
    runtime = next(item["file"] for item in items if item["file"].startswith("runtime-"))
    return {"directory": str(directory), "files": placed, "runtime_file": str(directory / runtime)}


def check_appimage_tools(record: dict) -> Path:
    """Re-check the placed AppImage tools; return the runtime file for LDAI_RUNTIME_FILE."""
    directory = Path(record["directory"])
    if directory != tauri_tools_directory():
        raise DownloadError("The AppImage tools were placed for a different user or cache")
    for item in record["files"]:
        path = directory / item["file"]
        if not path.is_file() or path.is_symlink() or digest(path) != item["sha256"]:
            raise DownloadError(f"{item['file']} changed after it was verified")
    return Path(record["runtime_file"])


# --- commands ------------------------------------------------------------------------


def prepare(
    receipt: Path,
    manifest_path: Path = MANIFEST,
    platform_key: str | None = None,
    rust_receipt: Path | None = None,
    download: Callable = http_download,
    installer: Callable[[str, Path], None] = run_playwright_install,
    check_installed_playwright: Callable[[dict, str], None] = check_playwright,
) -> dict:
    manifest = load_manifest(manifest_path)
    platform_key = platform_key or host_platform()
    if platform_key not in manifest["platforms"]:
        raise DownloadError(f"No reviewed native downloads for {platform_key}")
    if receipt.exists():
        raise DownloadError(f"{receipt} already exists; write the receipt to a new path")
    check_installed_playwright(manifest, platform_key)
    rust_record = None
    if rust_receipt is not None:
        rust_record = json.loads(rust_receipt.read_text(encoding="utf-8"))
        try:
            rust_toolchain.check_receipt(rust_record, platform_key=platform_key)
        except rust_toolchain.ToolchainError as error:
            raise DownloadError(str(error)) from None
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="native-downloads-", dir=receipt.parent) as folder:
        work = Path(folder)
        record = {
            "schema_version": 1,
            "platform": platform_key,
            "manifest_sha256": manifest_digest(manifest_path),
            "playwright": manifest["playwright"]["version"],
            "browser": install_browser(manifest, platform_key, work, download, installer),
        }
        if manifest["platforms"][platform_key].get("appimage_tools"):
            record["appimage_tools"] = place_appimage_tools(manifest, platform_key, work, download)
        if rust_record is not None:
            record["rust_toolchain"] = rust_record
    receipt.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def check_receipt(receipt: dict, browser: str | None, manifest_path: Path = MANIFEST) -> dict:
    """Refuse a build whose browser or AppImage tools are not the verified ones.

    browser is the executable the build is about to bundle. Returns the
    environment the native build needs (the pinned AppImage runtime on Linux).
    """
    if receipt.get("schema_version") != 1 or receipt.get("platform") != host_platform():
        raise DownloadError("The verified-downloads receipt is for another platform")
    if receipt.get("manifest_sha256") != manifest_digest(manifest_path):
        raise DownloadError("The verified-downloads receipt predates the current manifest")
    if "rust_toolchain" in receipt:
        try:
            rust_toolchain.check_receipt(receipt["rust_toolchain"])
        except rust_toolchain.ToolchainError as error:
            raise DownloadError(str(error)) from None
    record = receipt["browser"]
    root = Path(record["browsers_path"])
    executable = root / record["executable"]["file"]
    if browser is None or Path(browser).resolve() != executable.resolve():
        raise DownloadError(f"The build would bundle {browser}, not the verified browser")
    if listing_digest(tree_listing(root / record["directory"])) != record["tree_sha256"]:
        raise DownloadError("The verified browser changed after it was installed")
    if digest(executable) != record["executable"]["sha256"]:
        raise DownloadError("The verified browser executable changed after it was installed")
    environment = {}
    if "appimage_tools" in receipt:
        environment["LDAI_RUNTIME_FILE"] = str(check_appimage_tools(receipt["appimage_tools"]))
    return environment


def pin(manifest_path: Path = MANIFEST, download: Callable = http_download) -> None:
    manifest = load_manifest(manifest_path)
    with tempfile.TemporaryDirectory(prefix="native-downloads-pin-") as folder:
        for platform_key, entries in manifest["platforms"].items():
            for group in ("browser", "appimage_tools"):
                for index, item in enumerate(entries.get(group, [])):
                    path = Path(folder) / f"{platform_key}-{group}-{index}"
                    download(item["url"], path)
                    item["sha256"] = digest(path)
                    item["size"] = path.stat().st_size
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    make = commands.add_parser("prepare", help="download, verify and install the inputs")
    make.add_argument("--receipt", type=Path, required=True, help="new receipt file")
    make.add_argument(
        "--rust-receipt",
        type=Path,
        required=True,
        help="receipt from tools/rust_toolchain.py for the active compiler",
    )
    commands.add_parser("pin", help="recompute the manifest digests from its URLs")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            record = prepare(args.receipt, rust_receipt=args.rust_receipt)
            print(
                f"Verified {len(record['browser']['archives'])} browser archives"
                + (
                    f" and {len(record['appimage_tools']['files'])} AppImage tools"
                    if "appimage_tools" in record
                    else ""
                )
                + f" for {record['platform']}; receipt {args.receipt}"
            )
        else:
            pin()
    except (DownloadError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
