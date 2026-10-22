"""Verify receipt-bound Windows bytes and extract into a new qualification directory."""

import argparse
import hashlib
import json
import re
import stat
import zipfile
from email.parser import Parser
from pathlib import Path, PurePosixPath


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def checked_archive(archive):
    """Reject unsafe or ambiguous names before writing any extracted file."""
    seen = set()
    for item in archive.infolist():
        name = item.orig_filename
        path = PurePosixPath(name)
        if (
            not path.parts
            or path.parts[0] != "PlateFold"
            or ".." in path.parts
            or "\\" in name
            or ":" in name
            or path.as_posix() != name.rstrip("/")
            or any(part.endswith((".", " ")) for part in path.parts)
            or stat.S_ISLNK(item.external_attr >> 16)
            or name.casefold() in seen
        ):
            raise ValueError(f"Unsafe or duplicate ZIP member: {name}")
        seen.add(name.casefold())
    if bad := archive.testzip():
        raise ValueError(f"ZIP CRC failure: {bad}")


def check_packages(runtime, requirements):
    metadata = {}
    for folder in runtime.glob("*.dist-info"):
        message = Parser().parsestr((folder / "METADATA").read_text(encoding="utf-8"))
        name = re.sub(r"[-_.]+", "-", message["Name"]).lower()
        licenses = [
            file
            for file in folder.rglob("*")
            if file.is_file()
            and any(
                word in file.relative_to(folder).as_posix().lower()
                for word in ("license", "licence", "copying", "notice")
            )
        ]
        metadata[name] = {
            "version": message["Version"],
            "licenses": [
                {"file": file.relative_to(runtime).as_posix(), "sha256": digest(file)}
                for file in sorted(licenses)
            ],
        }
    for line in requirements.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        name, version = line.split("==")
        name = re.sub(r"[-_.]+", "-", name).lower()
        if (
            name not in metadata
            or metadata[name]["version"] != version
            or not metadata[name]["licenses"]
        ):
            raise ValueError(f"Missing or mismatched runtime metadata/license: {line}")
    return metadata


def check_artifacts(receipt_path, receipt):
    artifacts = receipt["artifacts"]
    seen = set()
    for artifact in artifacts:
        name = artifact["file"]
        if Path(name).name != name or ":" in name or "\\" in name or name.casefold() in seen:
            raise ValueError(f"Invalid artifact filename: {name}")
        seen.add(name.casefold())
        file = receipt_path.parent / name
        if file.stat().st_size != artifact["size"] or digest(file) != artifact["sha256"]:
            raise ValueError(f"Artifact size/hash mismatch: {name}")
    validate_installer_variants(receipt)
    archives = [item for item in artifacts if item["file"].endswith(".zip")]
    if len(archives) != 1:
        raise ValueError("Expected one Windows ZIP")
    return receipt_path.parent / archives[0]["file"]


def validate_installer_variants(receipt):
    variants = receipt.get("installer_variants", {})
    modes = {"online": "downloadBootstrapper", "offline": "offlineInstaller"}
    installers = {item["file"] for item in receipt["artifacts"] if item["file"].endswith(".exe")}
    declared = set()
    for variant, item in variants.items():
        if (
            variant not in modes
            or item.get("webview_install_mode") != modes[variant]
            or item.get("pdf_renderer") != "bundled"
            or item.get("file") not in installers
            or item["file"] in declared
            or item.get("gui", {}).get("file") != "platefold-desktop.exe"
        ):
            raise ValueError(f"Invalid Windows installer variant evidence: {variant}")
        declared.add(item["file"])
    if receipt.get("schema_version") == 2 and (not variants or declared != installers):
        raise ValueError("Receipt must describe every Windows installer variant")


def installer_gui(receipt, variant=None):
    variants = receipt.get("installer_variants", {})
    if variants:
        if variant is None and len(variants) == 1:
            variant = next(iter(variants))
        if variant not in variants:
            raise ValueError("Choose --installer-variant online or offline for this installation")
        return variants[variant]["gui"]
    if variant is not None:
        raise ValueError("Legacy receipt does not identify an installer variant")
    gui = receipt.get("installer_gui")
    if not gui:
        raise ValueError("Receipt has no NSIS GUI hash; use a newer build receipt")
    return gui


def payload_entries(receipt, installed, variant=None):
    for item in receipt["payload"]:
        if installed and item["file"] == "platefold-desktop.exe":
            item = installer_gui(receipt, variant)
        yield item


def check_payload(package, receipt, installed=False, variant=None):
    runtime = package / "runtime/_internal"
    browser = package / "runtime" / receipt["bundled_browser"]["file"]
    if digest(browser) != receipt["bundled_browser"]["source_sha256"]:
        raise ValueError("Extracted browser does not match receipt")
    for item in payload_entries(receipt, installed, variant):
        file = package / item["file"]
        if not file.resolve().is_relative_to(package.resolve()):
            raise ValueError("Payload path escapes package")
        if digest(file) != item["sha256"] or file.stat().st_size != item["size"]:
            raise ValueError(f"Extracted payload mismatch: {item['file']}")
    check_runtime_inventory(package, receipt)
    for name in ("Sora-OFL.txt", "JetBrains-Mono-OFL.txt"):
        if not (runtime / "platefold/fonts" / name).is_file():
            raise ValueError(f"Missing font license: {name}")
    for path in (runtime / "licenses/LICENSE.txt", browser.parent / "LICENSE.headless_shell"):
        if not path.is_file():
            raise ValueError(f"Missing license: {path.name}")


def check_runtime_inventory(package, receipt):
    expected = {item["file"] for item in receipt["payload"]}
    extras = sorted(
        file.relative_to(package).as_posix()
        for file in (package / "runtime").rglob("*")
        if file.is_file() and file.relative_to(package).as_posix() not in expected
    )
    if extras:
        raise ValueError(f"Unrecorded runtime files (possibly left by an upgrade): {extras}")


def verify(receipt_path, destination, expected_commit, requirements, installed=None, variant=None):
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt["source_commit"] != expected_commit or receipt["source_dirty"]:
        raise ValueError("Receipt must identify the requested clean source commit")
    if receipt["platform"] != "windows" or receipt["architecture"] != "x86_64":
        raise ValueError("Expected Windows x86_64 receipt")
    # Never overwrite evidence, user data or an earlier extraction.
    if destination.exists():
        raise FileExistsError(destination)
    archive_path = check_artifacts(receipt_path, receipt)
    if installed is not None:
        installer_gui(receipt, variant)
    elif variant is not None:
        raise ValueError("--installer-variant requires --installed-directory")
    if installed is None:
        with zipfile.ZipFile(archive_path) as archive:
            checked_archive(archive)
            destination.mkdir(parents=True)
            archive.extractall(destination)
        package = destination / "PlateFold"
    else:
        package = installed
    runtime = package / "runtime/_internal"
    metadata = check_packages(runtime, requirements)
    check_payload(package, receipt, installed=installed is not None, variant=variant)
    result = {
        "status": "passed",
        "source_commit": expected_commit,
        "artifacts": receipt["artifacts"],
        "runtime_distributions": metadata,
        "payload_files": len(receipt["payload"]),
        "extracted_to": str(package),
        "installer_variant": variant if installed is not None else None,
        "qualification": (
            "installed payload only; GUI/upgrade/user-data acceptance is separate"
            if installed is not None
            else "payload only; installer not executed"
        ),
    }
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "PACKAGE-CHECK.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument(
        "--installer-variant",
        choices=("online", "offline"),
        help="Variant used for --installed-directory; required when the receipt contains both",
    )
    parser.add_argument(
        "--installed-directory",
        type=Path,
        help="Check an existing installation without modifying it; destination holds the report",
    )
    parser.add_argument(
        "--requirements",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "requirements-runtime.txt",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            verify(
                args.receipt,
                args.destination,
                args.expected_commit,
                args.requirements,
                args.installed_directory,
                args.installer_variant,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
