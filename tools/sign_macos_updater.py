"""Sign and verify an exact notarized Mac updater archive in protected CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

try:
    from tools.build_desktop import (
        preflight_updater_key,
        sanitized_build_tail,
        sign_updater_artifact,
    )
    from tools.build_update_feed import artifact_binding, release_binding
except ModuleNotFoundError:  # Direct execution adds tools/, not the repository root.
    from build_desktop import preflight_updater_key, sanitized_build_tail, sign_updater_artifact
    from build_update_feed import artifact_binding, release_binding

ROOT = Path(__file__).resolve().parents[1]


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def validate_inputs(directory: Path, expected_sha: str) -> tuple[Path, Path, dict]:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("Expected source SHA is invalid")
    key = os.environ.get("TAURI_SIGNING_PRIVATE_KEY", "")
    password = os.environ.get("TAURI_SIGNING_PRIVATE_KEY_PASSWORD")
    if not key or password is None:
        raise ValueError("Protected updater signing variables are missing")
    updaters = list(directory.glob("*.app.tar.gz"))
    if len(updaters) != 1:
        raise ValueError("Expected one Mac updater archive")
    updater = updaters[0]
    receipt_path = directory / "MACOS-RELEASE-RECEIPT.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("source_commit") != expected_sha or receipt.get("updater_signed") is not False:
        raise ValueError("Mac updater receipt source or signing state is invalid")
    if release_binding(updater.name)[0] != receipt.get("version"):
        raise ValueError("Mac updater archive is not named for the receipt's version")
    record = next(
        (
            item
            for item in receipt.get("artifacts", [])
            if isinstance(item, dict) and item.get("file") == updater.name
        ),
        None,
    )
    if (
        not record
        or record.get("size") != updater.stat().st_size
        or record.get("sha256") != digest(updater)
    ):
        raise ValueError("Mac updater bytes differ from the notarized-host receipt")
    return updater, receipt_path, receipt


def sign(directory: Path, expected_sha: str) -> dict:
    updater, receipt_path, receipt = validate_inputs(directory, expected_sha)

    npm = shutil.which("npm")
    if not npm:
        raise ValueError("Node.js/npm is required for updater signing")
    preflight_updater_key(npm)
    # The signature names this archive's version, update target and file, which
    # the app requires before installing it (build_update_feed.artifact_binding).
    version, target = release_binding(updater.name)
    try:
        signature = sign_updater_artifact(updater, version)
    except SystemExit as error:
        raise ValueError(f"Updater signing failed: {error}") from None
    if not signature.is_file() or not 0 < signature.stat().st_size <= 8192:
        raise ValueError("Updater signing did not produce a bounded signature")

    verification = subprocess.run(
        [
            "cargo",
            "run",
            "--quiet",
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
            str(updater.resolve()),
            str(signature.resolve()),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if verification.returncode:
        summary = ROOT / "build/updater-verification-summary.txt"
        summary.parent.mkdir(exist_ok=True)
        summary.write_text(sanitized_build_tail(verification.stderr), encoding="utf-8")
        raise ValueError(
            "Updater signature failed verification against the compiled policy; "
            f"sanitized diagnostics retained at {summary.relative_to(ROOT)}"
        )
    proof = json.loads(verification.stdout)
    if (
        proof.get("verified") is not True
        or proof.get("key_kind") != "artifact"
        or proof.get("file") != updater.name
        or proof.get("size") != updater.stat().st_size
        or proof.get("sha256") != digest(updater)
        or proof.get("binding") != {"version": version, "platform": target, "file": updater.name}
        or proof.get("trusted_comment") != artifact_binding(version, target, updater.name)
    ):
        raise ValueError("Updater verification receipt does not match the final archive")

    receipt["updater_signed"] = True
    receipt["updater_signature"] = {
        "file": signature.name,
        "size": signature.stat().st_size,
        "sha256": digest(signature),
        "verified_public_key": proof.get("public_key"),
        "trusted_comment": proof["trusted_comment"],
    }
    receipt_path.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    checksummed = [
        *sorted(directory.glob("*.dmg")),
        updater,
        signature,
        receipt_path,
    ]
    (directory / "SHA256SUMS").write_text(
        "".join(f"{digest(path)}  {path.name}\n" for path in checksummed), encoding="utf-8"
    )
    result_receipt = {
        "source_sha": expected_sha,
        "artifact": updater.name,
        "artifact_sha256": proof["sha256"],
        "artifact_size": proof["size"],
        "signature": signature.name,
        "signature_sha256": digest(signature),
        "public_key": proof["public_key"],
        "updater_signed": True,
        "private_registry_uploaded": False,
        "publicly_published": False,
        "verified": True,
    }
    (directory / "MACOS-UPDATER-CHECK.json").write_text(
        json.dumps(result_receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result_receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        result = sign(args.directory, args.expected_sha)
    except (
        OSError,
        ValueError,
        KeyError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
