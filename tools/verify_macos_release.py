"""Verify and extract one exact-source Codemagic macOS release bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath

try:
    from tools import check_macos_minimum
except ModuleNotFoundError:  # Direct execution puts tools/, not the root, on sys.path.
    import check_macos_minimum

MAX_RECEIPT = 1024 * 1024
MAX_ARTIFACT = 1024 * 1024 * 1024
MAX_UPDATER = 512 * 1024 * 1024
CERTIFICATE_SHA256 = "fe6bf19b7ca85ea85470c461458fdd5b3de7216792e350ed6916500550707464"
NATIVE_DOWNLOADS = Path(__file__).resolve().parents[1] / "packaging" / "native-downloads.json"


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def safe_zip(archive: zipfile.ZipFile) -> None:
    if len(archive.namelist()) != len(set(archive.namelist())):
        raise ValueError("Mac release archive contains duplicate entries")
    for info in archive.infolist():
        path = PurePosixPath(info.filename)
        mode = info.external_attr >> 16
        if path.is_absolute() or ".." in path.parts or stat.S_ISLNK(mode):
            raise ValueError("Mac release archive contains an unsafe entry")
        if info.file_size > MAX_ARTIFACT:
            raise ValueError("Mac release archive entry exceeds the size limit")


def read_json(archive: zipfile.ZipFile, name: str) -> dict:
    try:
        raw = archive.read(name)
    except KeyError:
        raise ValueError(f"Mac release archive is missing {PurePosixPath(name).name}") from None
    if len(raw) > MAX_RECEIPT:
        raise ValueError("Mac release receipt exceeded the read limit")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ValueError("Mac release receipt is invalid JSON") from None
    if not isinstance(value, dict):
        raise ValueError("Mac release receipt has an unexpected shape")
    return value


def validate_release_receipt(receipt: dict, expected_sha: str) -> dict[str, dict]:
    expected_states = {
        "developer_id_signed": True,
        "notarization_submitted": True,
        "notarization_status": "Accepted",
        "stapled_ticket_verified": True,
        "gatekeeper_accepted": True,
        "updater_signed": False,
        "publicly_published": False,
    }
    if receipt.get("source_commit") != expected_sha or receipt.get("architecture") != "arm64":
        raise ValueError("Mac release receipt source or architecture does not match")
    if receipt.get("certificate_sha256") != CERTIFICATE_SHA256:
        raise ValueError("Mac release receipt names an unexpected signing certificate")
    if any(receipt.get(key) != value for key, value in expected_states.items()):
        raise ValueError("Mac release receipt does not prove the required signing state")
    if not isinstance(receipt.get("nested_macho_count"), int) or receipt["nested_macho_count"] < 2:
        raise ValueError("Mac release receipt does not cover the nested runtime")
    smoke = receipt.get("signed_runtime_smoke")
    if not isinstance(smoke, dict) or set(smoke) != {"worker", "chromium"}:
        raise ValueError("Mac release receipt lacks signed runtime smoke evidence")
    verified = receipt.get("verified_downloads")
    if (
        not isinstance(verified, dict)
        or verified.get("platform") != "macos-arm64"
        or verified.get("manifest_sha256") != digest(NATIVE_DOWNLOADS)
    ):
        raise ValueError("Mac release receipt does not prove a verified browser download")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or len(artifacts) != 2:
        raise ValueError("Mac release receipt must name one DMG and one updater archive")
    records = {item.get("file"): item for item in artifacts if isinstance(item, dict)}
    if len(records) != 2 or sum(str(name).endswith(".dmg") for name in records) != 1:
        raise ValueError("Mac release artifact names are invalid")
    if sum(str(name).endswith(".app.tar.gz") for name in records) != 1:
        raise ValueError("Mac release updater artifact is missing")
    return records


def validate_tar(path: Path) -> None:
    with tarfile.open(path, "r:gz") as archive:
        members = archive.getmembers()
        names = {PurePosixPath(member.name) for member in members}
        required = {
            PurePosixPath("PlateFold.app/Contents/Info.plist"),
            PurePosixPath("PlateFold.app/Contents/MacOS/platefold-desktop"),
            PurePosixPath("PlateFold.app/Contents/Resources/runtime/platefold"),
        }
        if not required.issubset(names):
            raise ValueError("Mac updater archive is missing required application files")
        for member in members:
            member_path = PurePosixPath(member.name)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("Mac updater archive contains an unsafe path")
            if member.issym() or member.islnk():
                target = PurePosixPath(member.linkname)
                if target.is_absolute() or ".." in target.parts:
                    raise ValueError("Mac updater archive contains an unsafe link")


def verify_keychain_receipt(bundle: Path, expected_sha: str) -> None:
    aggregate = list(bundle.parent.glob("platefold_*_artifacts.zip"))
    if len(aggregate) != 1:
        raise ValueError("Expected one Codemagic evidence archive")
    with zipfile.ZipFile(aggregate[0]) as archive:
        safe_zip(archive)
        receipt = read_json(archive, "build/macos-release/keychain-receipt.json")
        try:
            stage = archive.read("build/macos-release/stage.txt").decode().strip()
        except (KeyError, UnicodeDecodeError):
            raise ValueError("Codemagic release stage receipt is invalid") from None
    if stage != "macos-release-complete":
        raise ValueError("Codemagic Mac release build did not complete")
    required = {
        "source_commit": expected_sha,
        "certificate_sha256": CERTIFICATE_SHA256,
        "temporary_keychain_cleanup_verified": True,
        "application_signed_and_notarized": True,
        "production_application_signed": True,
    }
    if any(receipt.get(key) != value for key, value in required.items()):
        raise ValueError("Temporary keychain receipt does not match the release")


def verify(directory: Path, expected_sha: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_sha):
        raise ValueError("Expected source SHA is invalid")
    bundles = list(directory.glob("PlateFold_*_macos-arm64-release-bundle.zip"))
    if len(bundles) != 1:
        raise ValueError("Expected one Mac release bundle")
    bundle = bundles[0]
    verify_keychain_receipt(bundle, expected_sha)
    target = directory / "final"
    if target.exists():
        raise ValueError("Mac release extraction directory must be new")
    target.mkdir()
    with zipfile.ZipFile(bundle) as archive:
        safe_zip(archive)
        names = archive.namelist()
        dmgs = [name for name in names if name.endswith(".dmg")]
        updaters = [name for name in names if name.endswith(".app.tar.gz")]
        if (
            len(dmgs) != 1
            or len(updaters) != 1
            or set(names)
            != {
                "MACOS-RELEASE-RECEIPT.json",
                dmgs[0],
                updaters[0],
            }
        ):
            raise ValueError("Mac release bundle contains unexpected files")
        receipt = read_json(archive, "MACOS-RELEASE-RECEIPT.json")
        records = validate_release_receipt(receipt, expected_sha)
        for name, record in records.items():
            if Path(name).name != name or not re.fullmatch(r"[A-Za-z0-9_.-]+", name):
                raise ValueError("Mac release receipt contains an unsafe filename")
            with archive.open(name) as source, (target / name).open("xb") as destination:
                shutil.copyfileobj(source, destination, 1024 * 1024)
            path = target / name
            limit = MAX_UPDATER if name.endswith(".app.tar.gz") else MAX_ARTIFACT
            if not 0 < path.stat().st_size <= limit:
                raise ValueError("Mac release artifact exceeds its size limit")
            if record.get("size") != path.stat().st_size or record.get("sha256") != digest(path):
                raise ValueError("Mac release artifact bytes do not match the signed-host receipt")
    updater = next(target.glob("*.app.tar.gz"))
    validate_tar(updater)
    try:
        minimum, _ = check_macos_minimum.require_supported_minimum(updater)
    except ValueError as error:
        raise ValueError(f"Mac app declares an unsupported minimum macOS:\n{error}") from None
    (target / "MACOS-RELEASE-RECEIPT.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    result = {
        "source_sha": expected_sha,
        "release_bundle": {
            "file": bundle.name,
            "size": bundle.stat().st_size,
            "sha256": digest(bundle),
        },
        "developer_id_signed": True,
        "notarized": True,
        "stapled": True,
        "gatekeeper_accepted": True,
        "updater_signed": False,
        "minimum_macos": check_macos_minimum.format_version(minimum),
        "verified": True,
    }
    (directory / "MACOS-RELEASE-CHECK.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--expected-sha", required=True)
    args = parser.parse_args()
    try:
        result = verify(args.directory, args.expected_sha)
    except (OSError, ValueError, zipfile.BadZipFile, tarfile.TarError) as error:
        parser.exit(1, f"{error}\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
