"""Prepare signed updater metadata from one explicitly approved artifact manifest.

This command writes a local envelope only. It never uploads or activates a feed.
Credentials are inherited through the metadata-specific environment, never argv.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
# The app downloads an artifact only from <prefix>v<version>/<file>, with the
# prefix compiled in from this policy, so the feed must list exactly that.
RELEASE_DOWNLOAD = json.loads((ROOT / "packaging/update-policy.json").read_text(encoding="utf-8"))[
    "artifact_url_prefix"
]
TARGETS = {"windows-x86_64-nsis", "linux-x86_64-appimage", "macos-aarch64-app", "macos-x86_64-app"}
MAX_ARTIFACT = 512 * 1024 * 1024
VERSION = (
    r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:-[0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?"
)

# The metadata key, held by the daily renewal job, only says which release is
# offered. Each updater artifact's own signature names the release it belongs
# to, in a Minisign trusted comment that the global signature covers, so only
# the artifact key can write it. The app installs a file only when that comment
# equals the version, target and file its feed offers. The format is defined
# once for the app and the release tools in desktop/src-tauri/src/updates/binding.rs.
BINDING_SCHEME = "platefold-artifact v1"
_BINDING_PARTS = (
    re.compile(r"[0-9A-Za-z][0-9A-Za-z.-]{0,127}"),
    re.compile(r"[0-9A-Za-z][0-9A-Za-z_-]{0,127}"),
    re.compile(r"[0-9A-Za-z][0-9A-Za-z_.-]{0,127}"),
)
# The release file names that carry an updater signature, and the update
# target each serves. Arm64 files serve Rust's aarch64 targets.
_UPDATER_FILES = (
    (
        re.compile(rf"PlateFold_(?P<v>{VERSION})_x64-(?:online|offline)-setup\.exe"),
        "windows-{a}-nsis",
    ),
    (
        re.compile(rf"PlateFold_(?P<v>{VERSION})_macos-(?P<a>arm64|x86_64)\.app\.tar\.gz"),
        "macos-{a}-app",
    ),
    (
        re.compile(rf"PlateFold_(?P<v>{VERSION})_linux-(?P<a>x86_64|arm64)\.AppImage"),
        "linux-{a}-appimage",
    ),
)


def artifact_binding(version, platform, file):
    """The trusted comment an updater artifact's signature must carry."""
    if not all(
        isinstance(part, str) and pattern.fullmatch(part)
        for part, pattern in zip((version, platform, file), _BINDING_PARTS, strict=True)
    ):
        raise ValueError("Updater release binding has an unsafe version, platform or file")
    return f"{BINDING_SCHEME} version={version} platform={platform} file={file}"


def release_binding(name):
    """(version, update target) for a release updater artifact, from its file name."""
    for pattern, target in _UPDATER_FILES:
        match = pattern.fullmatch(name)
        if match:
            arch = match.groupdict().get("a") or "x86_64"
            return match["v"], target.format(a="aarch64" if arch == "arm64" else arch)
    raise ValueError(f"{name} is not a release updater artifact")


def trusted_comment(encoded_signature):
    """The trusted comment of a Tauri .sig value. Reading it verifies nothing."""
    try:
        document = base64.b64decode(encoded_signature, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        raise ValueError("Updater signature is not a base64 Minisign document") from None
    lines = document.split("\n")
    if len(lines) < 4 or not lines[2].startswith("trusted comment: "):
        raise ValueError("Updater signature is not a Minisign document")
    return lines[2].removeprefix("trusted comment: ")


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def verify(path, signature, *, metadata=False, binding=None):
    """Verify against the compiled policy with the Rust verifier; return its receipt.

    An artifact must also name its release: binding is (version, update target),
    by default the ones its release file name implies.
    """
    command = ["cargo", "run", "--quiet", "--locked", "--example", "verify_updater_artifact", "--"]
    if metadata:
        command.append("--metadata")
    else:
        binding = binding or release_binding(path.name)
        command.extend(("--binding", *binding))
    command.extend((str(path.resolve()), str(signature.resolve())))
    result = subprocess.run(command, cwd=ROOT / "desktop/src-tauri", capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Signature verification against compiled update policy failed")
    receipt = json.loads(result.stdout)
    if not metadata:
        check_binding_receipt(receipt, *binding, path.name)
    return receipt


def check_binding_receipt(receipt, version, platform, file):
    """Require a verifier receipt to confirm this exact release binding."""
    if receipt.get("binding") != {"version": version, "platform": platform, "file": file} or (
        receipt.get("trusted_comment") != artifact_binding(version, platform, file)
    ):
        raise ValueError(f"{file} is not signed as {version} for {platform}")


def payload_from_manifest(manifest, artifacts, issued_at, ttl, verifier=verify):
    if set(manifest) != {"version", "channel", "notes", "published", "artifacts"}:
        raise ValueError("Unexpected release manifest fields")
    version = manifest["version"]
    if not isinstance(version, str) or not re.fullmatch(VERSION, version):
        raise ValueError("An exact release version without build metadata is required")
    channel = manifest["channel"]
    if channel not in ("stable", "prerelease") or (channel == "stable" and "-" in version):
        raise ValueError("Release version does not match the channel")
    if not isinstance(manifest["notes"], str) or len(manifest["notes"].encode()) > 8192:
        raise ValueError("Release notes exceed the accepted bound")
    if manifest["published"] is not True:
        raise ValueError("Only an approved manifest of available artifacts may enter a feed")
    if type(issued_at) is not int or type(ttl) is not int or not 3600 <= ttl <= 604800:
        raise ValueError("Metadata lifetime must be between one hour and seven days")
    entries = manifest["artifacts"]
    if not isinstance(entries, dict) or not entries or not set(entries).issubset(TARGETS):
        raise ValueError("Unsupported or empty updater targets")
    resolved = {
        target: verified_artifact(item, artifacts, version, target, verifier)
        for target, item in entries.items()
    }
    return {
        "schema_version": 1,
        "version": version,
        "channel": channel,
        "published": True,
        "issued_at": issued_at,
        "expires_at": issued_at + ttl,
        "notes": manifest["notes"],
        "artifacts": resolved,
    }


def verified_artifact(item, artifacts, version, target, verifier):
    if not isinstance(item, dict) or set(item) != {"file", "url", "sha256", "size"}:
        raise ValueError("Unexpected artifact manifest fields")
    name = item["file"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
        raise ValueError("Unsafe updater filename")
    expected_url = f"{RELEASE_DOWNLOAD}v{version}/{quote(name)}"
    if item["url"] != expected_url:
        raise ValueError("Artifact URL differs from the immutable release path")
    path = artifacts / name
    signature = artifacts / (name + ".sig")
    for file in (path, signature):
        if file.is_symlink() or not file.is_file():
            raise ValueError("An ordinary artifact and signature file are required")
    if type(item["size"]) is not int or not 0 < item["size"] <= MAX_ARTIFACT:
        raise ValueError("Artifact size exceeds updater limits")
    if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
        raise ValueError("Artifact bytes differ from the approved manifest")
    if signature.stat().st_size > 8192:
        raise ValueError("Updater signature exceeds the accepted bound")
    # The artifact key must have signed these bytes as this version, for this
    # target, under this file name; the feed can only offer what it signed.
    checked = verifier(path, signature, binding=(version, target))
    if (
        checked.get("verified") is not True
        or checked.get("key_kind") != "artifact"
        or checked.get("sha256") != item["sha256"]
        or checked.get("size") != item["size"]
    ):
        raise ValueError("Verifier receipt differs from the approved artifact")
    check_binding_receipt(checked, version, target, name)
    return {
        "url": item["url"],
        "sha256": item["sha256"],
        "size": item["size"],
        "signature": signature.read_text(encoding="ascii").strip(),
        "public_key": checked["public_key"],
    }


def sign_payload(payload, output):
    key = os.environ.get("PLATEFOLD_METADATA_SIGNING_PRIVATE_KEY")
    password = os.environ.get("PLATEFOLD_METADATA_SIGNING_PRIVATE_KEY_PASSWORD")
    if not key or password is None:
        raise ValueError("The separate metadata signing key and password must be configured")
    env = os.environ.copy()
    env.pop("TAURI_SIGNING_PRIVATE_KEY_PATH", None)
    env["TAURI_SIGNING_PRIVATE_KEY"] = key
    env["TAURI_SIGNING_PRIVATE_KEY_PASSWORD"] = password
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix="platefold-feed-") as directory:
        source = Path(directory) / "payload.json"
        source.write_text(serialized, encoding="utf-8")
        result = subprocess.run(
            [
                "node",
                str(ROOT / "desktop/node_modules/@tauri-apps/cli/tauri.js"),
                "signer",
                "sign",
                str(source),
            ],
            env=env,
            capture_output=True,
        )
        if result.returncode:
            raise ValueError("Metadata signing failed; no feed was written")
        signature = source.with_suffix(".json.sig")
        verify(source, signature, metadata=True)
        full_signature = base64.b64decode(signature.read_text().strip(), validate=True).decode(
            "utf-8"
        )
        envelope = {
            "payload": serialized,
            "signature": full_signature,
            "version": payload["version"],
            "notes": payload["notes"],
            "platforms": {
                target: {"url": item["url"], "signature": item["signature"]}
                for target, item in payload["artifacts"].items()
            },
        }
        encoded = (json.dumps(envelope, ensure_ascii=False, indent=2) + "\n").encode()
        if len(encoded) > 65536:
            raise ValueError("Signed feed exceeds the shipped client's size bound")
        # Exclusive creation preserves any previous approved feed until the
        # publisher deliberately selects the new file. Renewal never overwrites it.
        with output.open("xb") as handle:
            handle.write(encoded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--approved-manifest-sha256", required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--ttl-seconds", type=int, default=604800)
    args = parser.parse_args()
    if (
        not re.fullmatch(r"[0-9a-f]{64}", args.approved_manifest_sha256)
        or digest(args.manifest) != args.approved_manifest_sha256
    ):
        raise SystemExit("Release manifest differs from the explicitly approved digest")
    if args.manifest.stat().st_size > 65536:
        raise SystemExit("Release manifest exceeds the accepted bound")
    try:
        payload = payload_from_manifest(
            json.loads(args.manifest.read_text()),
            args.artifacts,
            int(time.time()),
            args.ttl_seconds,
        )
        sign_payload(payload, args.output)
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    print(
        "Signed feed prepared locally; public deployment and consumer verification remain separate."
    )


if __name__ == "__main__":
    main()
