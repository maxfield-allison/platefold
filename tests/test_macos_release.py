"""Release-boundary tests for the protected macOS candidate path."""

import io
import json
import plistlib
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_macos_minimum import build_version, thin
from tools import sign_macos_updater
from tools.verify_macos_release import CERTIFICATE_SHA256, NATIVE_DOWNLOADS, digest, verify

SHA = "a" * 40


def test_updater_signing_module_loads_from_ci_working_directory():
    root = Path(__file__).parents[1]
    result = subprocess.run(
        [sys.executable, "-m", "tools.sign_macos_updater", "--help"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Sign and verify an exact notarized Mac updater archive" in result.stdout


def updater_archive(path: Path, declared: str = "15.1", runtime: str = "15.1") -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, payload in (
            (
                "PlateFold.app/Contents/Info.plist",
                plistlib.dumps({"LSMinimumSystemVersion": declared}),
            ),
            ("PlateFold.app/Contents/MacOS/platefold-desktop", thin(build_version("11.0"))),
            ("PlateFold.app/Contents/Resources/runtime/platefold", thin(build_version(runtime))),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))


def release_directory(tmp_path: Path, updater_minimums=("15.1", "15.1"), **changes) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    dmg = source / "PlateFold_1.0.0_macos-arm64.dmg"
    updater = source / "PlateFold_1.0.0_macos-arm64.app.tar.gz"
    dmg.write_bytes(b"dmg fixture")
    updater_archive(updater, *updater_minimums)
    receipt = {
        "schema_version": 1,
        "product": "PlateFold",
        "version": "1.0.0",
        "source_commit": SHA,
        "architecture": "arm64",
        "certificate_sha256": CERTIFICATE_SHA256,
        "developer_id_signed": True,
        "notarization_submitted": True,
        "notarization_status": "Accepted",
        "stapled_ticket_verified": True,
        "gatekeeper_accepted": True,
        "nested_macho_count": 4,
        "signed_runtime_smoke": {"worker": "PlateFold 1.0.0", "chromium": "Chromium 151"},
        "updater_signed": False,
        "publicly_published": False,
        "verified_downloads": {
            "platform": "macos-arm64",
            "manifest_sha256": digest(NATIVE_DOWNLOADS),
        },
        "artifacts": [
            {"file": path.name, "size": path.stat().st_size, "sha256": digest(path)}
            for path in (dmg, updater)
        ],
    }
    receipt.update(changes)
    (source / "MACOS-RELEASE-RECEIPT.json").write_text(json.dumps(receipt))
    bundle = tmp_path / "PlateFold_1.0.0_macos-arm64-release-bundle.zip"
    with zipfile.ZipFile(bundle, "w") as archive:
        for path in source.iterdir():
            archive.write(path, path.name)
    keychain = {
        "source_commit": SHA,
        "certificate_sha256": CERTIFICATE_SHA256,
        "temporary_keychain_cleanup_verified": True,
        "application_signed_and_notarized": True,
        "production_application_signed": True,
    }
    with zipfile.ZipFile(tmp_path / "platefold_1_artifacts.zip", "w") as archive:
        archive.writestr("build/macos-release/keychain-receipt.json", json.dumps(keychain))
        archive.writestr("build/macos-release/stage.txt", "macos-release-complete\n")
    return tmp_path


def test_exact_release_bundle_is_verified_and_extracted(tmp_path):
    directory = release_directory(tmp_path)
    result = verify(directory, SHA)
    assert result["verified"] is True
    assert result["notarized"] is True
    assert (directory / "final/PlateFold_1.0.0_macos-arm64.dmg").read_bytes() == b"dmg fixture"
    assert (directory / "final/PlateFold_1.0.0_macos-arm64.app.tar.gz").is_file()
    assert result["minimum_macos"] == "15.1"


@pytest.mark.parametrize(
    "declared,runtime",
    [("10.13", "15.1"), ("15.1", "15.2")],
    ids=["tauri-default", "newer-runtime"],
)
def test_release_refuses_an_app_that_opens_where_its_runtime_cannot_load(
    tmp_path, declared, runtime
):
    directory = release_directory(tmp_path, updater_minimums=(declared, runtime))
    with pytest.raises(ValueError, match="unsupported minimum macOS") as refused:
        verify(directory, SHA)
    assert f"requires macOS {runtime}: Contents/Resources/runtime/platefold" in str(refused.value)
    assert not (directory / "MACOS-RELEASE-CHECK.json").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source_commit", "b" * 40),
        ("notarization_status", "In Progress"),
        ("gatekeeper_accepted", False),
        ("updater_signed", True),
        # The browser the app bundles must have come from the reviewed archive.
        ("verified_downloads", None),
        ("verified_downloads", {"platform": "linux-x86_64", "manifest_sha256": "0" * 64}),
        ("verified_downloads", {"platform": "macos-arm64", "manifest_sha256": "0" * 64}),
    ],
)
def test_release_bundle_rejects_incomplete_or_wrong_state(tmp_path, field, value):
    directory = release_directory(tmp_path, **{field: value})
    with pytest.raises(ValueError, match="receipt"):
        verify(directory, SHA)


BOUND = (
    "platefold-artifact v1 version=1.0.0 platform=macos-aarch64-app "
    "file=PlateFold_1.0.0_macos-arm64.app.tar.gz"
)


def bound_signer(updater, commands, proof_changes=None):
    """The signer and verifier examples, as cargo runs them, for one archive."""

    def fake_run(command, **_kwargs):
        commands.append(command)
        if "sign_updater_artifact" in command:
            Path(f"{updater}.sig").write_text("fixture-signature", encoding="ascii")
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"trusted_comment": BOUND}), stderr=""
            )
        proof = {
            "verified": True,
            "key_kind": "artifact",
            "file": updater.name,
            "size": updater.stat().st_size,
            "sha256": digest(updater),
            "public_key": "fixture-public-key",
            "binding": {
                "version": "1.0.0",
                "platform": "macos-aarch64-app",
                "file": updater.name,
            },
            "trusted_comment": BOUND,
        }
        proof.update(proof_changes or {})
        return SimpleNamespace(returncode=0, stdout=json.dumps(proof), stderr="")

    return fake_run


def test_gitlab_signs_and_verifies_exact_updater(tmp_path, monkeypatch):
    directory = release_directory(tmp_path)
    verify(directory, SHA)
    final = directory / "final"
    updater = next(final.glob("*.app.tar.gz"))
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "password")
    monkeypatch.setattr(sign_macos_updater.shutil, "which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr(sign_macos_updater, "preflight_updater_key", lambda _: None)
    commands = []
    monkeypatch.setattr(sign_macos_updater.subprocess, "run", bound_signer(updater, commands))
    result = sign_macos_updater.sign(final, SHA)
    assert result["verified"] is True
    assert result["updater_signed"] is True
    receipt = json.loads((final / "MACOS-RELEASE-RECEIPT.json").read_text())
    assert receipt["updater_signed"] is True
    assert receipt["updater_signature"]["trusted_comment"] == BOUND
    assert (final / f"{updater.name}.sig").is_file()
    # Signed, then verified, as release 1.0.0 for the macos-aarch64-app target;
    # the key stays in the environment.
    sign_command, verify_command = commands
    assert sign_command[sign_command.index("--") :] == [
        "--",
        "--version",
        "1.0.0",
        "--platform",
        "macos-aarch64-app",
        str(updater.resolve()),
    ]
    assert verify_command[verify_command.index("--binding") :][:3] == [
        "--binding",
        "1.0.0",
        "macos-aarch64-app",
    ]
    assert not any("private-key" in part for command in commands for part in command)


@pytest.mark.parametrize(
    "changes",
    [
        {"binding": {"version": "0.9.0", "platform": "macos-aarch64-app", "file": "x"}},
        {"trusted_comment": "timestamp:1\tfile:PlateFold_1.0.0_macos-arm64.app.tar.gz"},
        {"binding": None},
    ],
)
def test_gitlab_refuses_an_updater_signature_without_its_release_binding(
    tmp_path, monkeypatch, changes
):
    directory = release_directory(tmp_path)
    verify(directory, SHA)
    final = directory / "final"
    updater = next(final.glob("*.app.tar.gz"))
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "password")
    monkeypatch.setattr(sign_macos_updater.shutil, "which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr(sign_macos_updater, "preflight_updater_key", lambda _: None)
    monkeypatch.setattr(sign_macos_updater.subprocess, "run", bound_signer(updater, [], changes))
    with pytest.raises(ValueError, match="does not match the final archive"):
        sign_macos_updater.sign(final, SHA)
    assert json.loads((final / "MACOS-RELEASE-RECEIPT.json").read_text())["updater_signed"] is False


def test_gitlab_refuses_an_archive_named_for_another_version(tmp_path, monkeypatch):
    directory = release_directory(tmp_path, version="1.0.1")
    verify(directory, SHA)
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "password")
    with pytest.raises(ValueError, match="not named for the receipt's version"):
        sign_macos_updater.sign(directory / "final", SHA)


def test_gitlab_keeps_sanitized_verifier_diagnostics(tmp_path, monkeypatch):
    directory = release_directory(tmp_path)
    verify(directory, SHA)
    final = directory / "final"
    updater = next(final.glob("*.app.tar.gz"))
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "password")
    monkeypatch.setattr(sign_macos_updater.shutil, "which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr(sign_macos_updater, "preflight_updater_key", lambda _: None)
    monkeypatch.setattr(sign_macos_updater, "ROOT", tmp_path)

    def fake_run(command, **_kwargs):
        if "sign_updater_artifact" in command:
            Path(f"{updater}.sig").write_text("fixture-signature", encoding="ascii")
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"trusted_comment": BOUND}), stderr=""
            )
        stderr = "error: resource path `runtime/` doesn't exist\nTAURI_SIGNING_PRIVATE_KEY=x\n"
        return SimpleNamespace(returncode=101, stdout="", stderr=stderr)

    monkeypatch.setattr(sign_macos_updater.subprocess, "run", fake_run)
    with pytest.raises(ValueError, match="sanitized diagnostics retained"):
        sign_macos_updater.sign(final, SHA)
    summary = (tmp_path / "build/updater-verification-summary.txt").read_text()
    assert "resource path `runtime/`" in summary
    assert "TAURI_SIGNING_PRIVATE_KEY=x" not in summary


def test_gitlab_refuses_updater_from_another_source(tmp_path, monkeypatch):
    directory = release_directory(tmp_path)
    verify(directory, SHA)
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", "password")
    with pytest.raises(ValueError, match="receipt source"):
        sign_macos_updater.sign(directory / "final", "b" * 40)
