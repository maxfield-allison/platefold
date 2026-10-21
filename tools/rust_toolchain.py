#!/usr/bin/env python3
"""Install or verify the reviewed Rust toolchain used by native builds.

Windows and macOS receive an isolated rustup home. This tool downloads the
reviewed channel manifest and minimal-profile component archives, verifies
their recorded SHA-256 values, and exposes only those bytes to rustup through
a loopback server. Each download is read once, checked in memory and deleted,
and the server serves those bytes, so nothing can change them between the check
and rustup's request. The rustup that installs them is trusted: the installed
toolchain is checked by its reported version, not file by file. Linux uses the toolchain baked into the digest-pinned Tauri
image and verifies its version. Both paths write a receipt that the desktop
build carries into BUILD-RECEIPT.json.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import http.server
import json
import os
import platform
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from collections.abc import Callable, Iterator
from pathlib import Path

try:
    from tools import download_retry
except ModuleNotFoundError:  # `python tools/rust_toolchain.py`
    import download_retry

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "packaging" / "rust-toolchain-downloads.json"
MAX_DOWNLOAD = 128 * 1024 * 1024
NETWORK_OVERRIDES = (
    "RUSTUP_DIST_SERVER",
    "RUSTUP_UPDATE_ROOT",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


class ToolchainError(ValueError):
    """The Rust toolchain does not match the reviewed inputs."""


def digest_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


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
    raise ToolchainError(f"No reviewed Rust toolchain for {system} {machine}")


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
        raise ToolchainError(f"{url} exceeds the download limit") from None
    except download_retry.Failed as failure:
        raise ToolchainError(f"Download of {url} {failure}: {failure.error}") from None


def fetch_verified(item: dict, destination: Path, download: Callable = http_download) -> bytes:
    """Download one reviewed input and return the bytes that were checked."""
    download(item["url"], destination)
    try:
        data = destination.read_bytes()
    finally:
        destination.unlink()
    size, actual = len(data), digest_bytes(data)
    if size != item["size"] or actual != item["sha256"]:
        raise ToolchainError(
            f"{item['url']} is not the reviewed Rust input: expected {item['size']} bytes "
            f"with SHA-256 {item['sha256']}, received {size} bytes with SHA-256 {actual}"
        )
    return data


def validate_channel(channel: bytes, manifest: dict, platform_key: str) -> None:
    try:
        parsed = tomllib.loads(channel.decode("utf-8"))
        rust_version = parsed["pkg"]["rust"]["version"]
    except (KeyError, UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise ToolchainError(f"The reviewed Rust channel manifest is malformed: {error}") from None
    version = manifest["version"]
    if not rust_version.startswith(f"{version} "):
        raise ToolchainError(f"The channel contains {rust_version}, not Rust {version}")
    target = manifest["platforms"][platform_key]["target"]
    for item in manifest["platforms"][platform_key]["components"]:
        try:
            entry = parsed["pkg"][item["package"]]["target"][target]
        except KeyError:
            raise ToolchainError(f"The channel lacks {item['package']} for {target}") from None
        if entry.get("xz_url") != item["url"] or entry.get("xz_hash") != item["sha256"]:
            raise ToolchainError(
                f"The reviewed channel no longer binds {item['package']} to the pinned archive"
            )


@contextlib.contextmanager
def loopback_server(files: dict[str, bytes]) -> Iterator[tuple[str, list[str]]]:
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
            self.send_header("Content-Type", "application/octet-stream")
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


def run_rustup_install(host: str, version: str) -> None:
    environment = {
        name: value for name, value in os.environ.items() if name not in NETWORK_OVERRIDES
    }
    environment["RUSTUP_DIST_SERVER"] = host
    environment["RUSTUP_UPDATE_ROOT"] = f"{host}/rustup"
    environment["NO_PROXY"] = environment["no_proxy"] = "127.0.0.1"
    subprocess.run(
        ["rustup", "toolchain", "install", version, "--profile", "minimal", "--no-self-update"],
        env=environment,
        stdin=subprocess.DEVNULL,
        check=True,
        timeout=1800,
    )
    subprocess.run(
        ["rustup", "default", version], env=environment, stdin=subprocess.DEVNULL, check=True
    )


def command_text(arguments: list[str]) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, check=True, timeout=60)
    return result.stdout.strip()


def verify_versions(
    version: str, target: str, run: Callable[[list[str]], str] = command_text
) -> dict:
    rustc = run(["rustup", "run", version, "rustc", "--version", "--verbose"])
    cargo = run(["rustup", "run", version, "cargo", "--version"])
    if not rustc.startswith(f"rustc {version} ") or f"host: {target}" not in rustc.splitlines():
        raise ToolchainError(f"rustc is not the reviewed {version} toolchain for {target}")
    if not cargo.startswith(f"cargo {version} "):
        raise ToolchainError(f"cargo is not the reviewed {version} release")
    return {"rustc": rustc, "cargo": cargo}


def _isolated_rustup_home() -> Path:
    value = os.environ.get("RUSTUP_HOME")
    if not value:
        raise ToolchainError("Set RUSTUP_HOME to a new job-local directory")
    home = Path(value).expanduser().resolve()
    toolchains = home / "toolchains"
    if toolchains.exists() and any(toolchains.iterdir()):
        raise ToolchainError(f"{toolchains} is not empty; refuse an earlier toolchain")
    return home


def install(
    receipt: Path,
    manifest_path: Path = MANIFEST,
    platform_key: str | None = None,
    download: Callable = http_download,
    installer: Callable[[str, str], None] = run_rustup_install,
    version_check: Callable[[str, str], dict] = verify_versions,
) -> dict:
    manifest = load_manifest(manifest_path)
    platform_key = platform_key or host_platform()
    if platform_key not in manifest["platforms"]:
        raise ToolchainError(f"Rust {manifest['version']} is baked into the Linux CI image")
    if receipt.exists():
        raise ToolchainError(f"{receipt} already exists; write the receipt to a new path")
    _isolated_rustup_home()
    receipt.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rust-toolchain-", dir=receipt.parent) as folder:
        work = Path(folder)
        channel = fetch_verified(manifest["channel_manifest"], work / "channel.toml", download)
        validate_channel(channel, manifest, platform_key)
        archives: dict[str, bytes] = {}
        for index, item in enumerate(manifest["platforms"][platform_key]["components"]):
            archives[item["path"]] = fetch_verified(
                item, work / f"component-{index}.tar.xz", download
            )
        files: dict[str, bytes] = {}
        with loopback_server(files) as (host, requested):
            rewritten = channel.replace(b"https://static.rust-lang.org/", f"{host}/".encode())
            channel_name = f"dist/channel-rust-{manifest['version']}.toml"
            files[channel_name] = rewritten
            files[f"{channel_name}.sha256"] = (
                f"{digest_bytes(rewritten)}  channel-rust-{manifest['version']}.toml\n".encode()
            )
            files.update(archives)
            installer(host, manifest["version"])
        required = {channel_name, f"{channel_name}.sha256", *archives}
        if not required.issubset(requested) or not set(requested).issubset(required):
            raise ToolchainError(f"rustup requested {requested}, not only the reviewed Rust inputs")
        versions = version_check(manifest["version"], manifest["platforms"][platform_key]["target"])
        record = {
            "schema_version": 1,
            "kind": "verified-archives",
            "platform": platform_key,
            "version": manifest["version"],
            "channel_manifest_sha256": manifest["channel_manifest"]["sha256"],
            "components": [
                {key: item[key] for key in ("package", "url", "sha256", "size")}
                for item in manifest["platforms"][platform_key]["components"]
            ],
            **versions,
        }
    receipt.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def baked(
    receipt: Path,
    image: str,
    manifest_path: Path = MANIFEST,
    version_check: Callable[[str, str], dict] = verify_versions,
) -> dict:
    manifest = load_manifest(manifest_path)
    image_digest = manifest["ci_image_digest"]
    if not image.endswith(f"@{image_digest}"):
        raise ToolchainError("The Tauri CI image is not the reviewed digest-pinned image")
    if receipt.exists():
        raise ToolchainError(f"{receipt} already exists; write the receipt to a new path")
    platform_key = host_platform()
    if platform_key != "linux-x86_64":
        raise ToolchainError("The baked Rust toolchain is reviewed only in the Linux CI image")
    versions = version_check(manifest["version"], "x86_64-unknown-linux-gnu")
    record = {
        "schema_version": 1,
        "kind": "digest-pinned-image",
        "platform": platform_key,
        "version": manifest["version"],
        "image_digest": image_digest,
        **versions,
    }
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def check_receipt(
    record: dict,
    manifest_path: Path = MANIFEST,
    platform_key: str | None = None,
    version_check: Callable[[str, str], dict] = verify_versions,
) -> None:
    manifest = load_manifest(manifest_path)
    platform_key = platform_key or host_platform()
    if (
        record.get("schema_version") != 1
        or record.get("platform") != platform_key
        or record.get("version") != manifest["version"]
    ):
        raise ToolchainError("The Rust toolchain receipt is for another build platform or version")
    if record.get("kind") == "digest-pinned-image":
        if record.get("image_digest") != manifest["ci_image_digest"]:
            raise ToolchainError("The Rust receipt names a different CI image")
        target = "x86_64-unknown-linux-gnu"
    elif record.get("kind") == "verified-archives" and platform_key in manifest["platforms"]:
        expected = manifest["platforms"][platform_key]
        if record.get("channel_manifest_sha256") != manifest["channel_manifest"]["sha256"]:
            raise ToolchainError("The Rust receipt predates the reviewed channel manifest")
        components = [
            {key: item[key] for key in ("package", "url", "sha256", "size")}
            for item in expected["components"]
        ]
        if record.get("components") != components:
            raise ToolchainError("The Rust receipt does not cover the reviewed component archives")
        target = expected["target"]
    else:
        raise ToolchainError("The Rust receipt has an unreviewed source kind")
    if version_check(manifest["version"], target) != {
        "rustc": record.get("rustc"),
        "cargo": record.get("cargo"),
    }:
        raise ToolchainError("The active Rust toolchain differs from the verified receipt")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    install_parser = commands.add_parser("install", help="install only reviewed Rust archives")
    install_parser.add_argument("--receipt", type=Path, required=True)
    baked_parser = commands.add_parser("baked", help="verify Rust in the digest-pinned CI image")
    baked_parser.add_argument("--receipt", type=Path, required=True)
    baked_parser.add_argument("--image", required=True)
    args = parser.parse_args(argv)
    try:
        record = (
            install(args.receipt) if args.command == "install" else baked(args.receipt, args.image)
        )
        print(f"Verified Rust {record['version']} for {record['platform']}; receipt {args.receipt}")
    except (ToolchainError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
