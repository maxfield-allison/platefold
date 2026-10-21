"""Linux installation policy must travel with the checksummed package."""

import hashlib
import json
import shutil
import sys
from pathlib import Path

import pytest

from tools import build_desktop, smoke_linux_desktop


def test_native_startup_probe_rejects_an_early_exit():
    with pytest.raises(SystemExit, match="exited before"):
        smoke_linux_desktop.exercise_native_startup(
            [sys.executable, "-c", "raise SystemExit('startup failed')"],
            hold_seconds=1,
        )


@pytest.mark.skipif(
    sys.platform == "win32", reason="the AppImage smoke check manages a POSIX process group"
)
def test_native_startup_probe_stops_a_healthy_process_group():
    result = smoke_linux_desktop.exercise_native_startup(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        hold_seconds=0.1,
    )
    assert result == {
        "native_startup_exercised": True,
        "native_startup_hold_seconds": 0.1,
    }


def test_packaged_smoke_can_reproduce_windows_markdown_line_endings(tmp_path):
    source = tmp_path / "wide.md"
    source.write_bytes(b"# Wide\n\n```mermaid\nflowchart LR\n a-->b\n```\n")
    copy = tmp_path / "copy"
    copy.mkdir()

    copied = smoke_linux_desktop.prepared_fixture(source, copy, True)

    assert copied.read_bytes().count(b"\r\n") == 6
    assert b"\n" not in copied.read_bytes().replace(b"\r\n", b"")


def synthetic_linux_release(release):
    """The Tauri outputs the Linux packaging reads: program, runtime and AppImage."""
    image = release / "bundle/appimage/PlateFold.AppImage"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"synthetic AppImage")
    browser = release / "runtime/_internal/platefold/browser/chrome-headless-shell"
    browser.parent.mkdir(parents=True)
    browser.write_bytes(b"synthetic browser")
    (release / "runtime/platefold").write_bytes(b"synthetic CLI")
    (release / "runtime/platefold").chmod(0o755)
    (release / "platefold-desktop").write_bytes(b"ELF\0__TAURI_BUNDLE_TYPE_VAR_UNK\0program")
    return image


requires_dpkg = pytest.mark.skipif(
    shutil.which("dpkg-deb") is None, reason="the Linux downloads include a .deb"
)


@requires_dpkg
def test_linux_downloads_bind_installation_policy_to_receipt(tmp_path, monkeypatch):
    root = tmp_path / "source"
    release = root / "desktop/src-tauri/target/release"
    synthetic_linux_release(release)
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1790000000")
    shutil.copytree(build_desktop.ROOT / "packaging/linux", root / "packaging/linux")
    monkeypatch.setattr(build_desktop, "ROOT", root)
    monkeypatch.setattr(build_desktop, "source_state", lambda: ("1" * 40, False))
    monkeypatch.setattr(build_desktop, "build_inputs", lambda: {})
    monkeypatch.setattr(build_desktop, "command_version", lambda *args: "test-toolchain")
    monkeypatch.setattr(build_desktop.importlib.metadata, "version", lambda name: "test")
    output = tmp_path / "downloads"
    build_desktop.package_downloads(
        build_desktop.PLATFORMS["linux"], "0.3.0-dev.6", output_dir=output
    )
    receipt = json.loads((output / "BUILD-RECEIPT.json").read_text())
    records = {item["file"]: item for item in receipt["artifacts"]}
    assert len(records) == 4
    assert {"platefold.apparmor", "LINUX-INSTALL.md", "platefold_0.3.0~dev.6_amd64.deb"} <= (
        records.keys()
    )
    assert receipt["debian_package"] == {
        "file": "platefold_0.3.0~dev.6_amd64.deb",
        "package_version": "0.3.0~dev.6",
        "source_date_epoch": 1790000000,
        "apparmor_profile": "platefold-bundled-chromium",
        "bundled_browser": (
            "/usr/lib/PlateFold/runtime/_internal/platefold/browser/chrome-headless-shell"
        ),
    }
    assert receipt["bundled_browser"]["file"] == "_internal/platefold/browser/chrome-headless-shell"
    checksums = (output / "SHA256SUMS").read_text()
    for name, record in records.items():
        payload = (output / name).read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        assert record["sha256"] == digest
        assert record["size"] == len(payload)
        assert f"{digest}  {name}\n" in checksums


@requires_dpkg
def test_linux_updater_signature_is_retained_only_for_explicit_signed_builds(tmp_path, monkeypatch):
    root = tmp_path / "source"
    release = root / "desktop/src-tauri/target/release"
    image = synthetic_linux_release(release)
    Path(f"{image}.sig").write_text("synthetic updater signature")
    monkeypatch.setenv("SOURCE_DATE_EPOCH", "1790000000")
    shutil.copytree(build_desktop.ROOT / "packaging/linux", root / "packaging/linux")
    monkeypatch.setattr(build_desktop, "ROOT", root)
    monkeypatch.setattr(build_desktop, "source_state", lambda: ("1" * 40, False))
    monkeypatch.setattr(build_desktop, "build_inputs", lambda: {})
    monkeypatch.setattr(build_desktop, "command_version", lambda *args: "test-toolchain")
    monkeypatch.setattr(build_desktop.importlib.metadata, "version", lambda name: "test")
    output = tmp_path / "downloads"
    signed = []

    def sign(path, version):
        # The final AppImage is signed again, as its release, under its final name.
        signed.append((path, version))
        signature = Path(f"{path}.sig")
        signature.write_text("bound updater signature")
        return signature

    monkeypatch.setattr(build_desktop, "sign_updater_artifact", sign)

    build_desktop.package_downloads(
        build_desktop.PLATFORMS["linux"],
        "0.3.0-dev.6",
        include_updater=True,
        output_dir=output,
    )

    signature = output / "PlateFold_0.3.0-dev.6_linux-x86_64.AppImage.sig"
    assert signed == [(output / "PlateFold_0.3.0-dev.6_linux-x86_64.AppImage", "0.3.0-dev.6")]
    assert signature.read_text() == "bound updater signature"
    receipt = json.loads((output / "BUILD-RECEIPT.json").read_text())
    assert receipt["updater_artifacts"] is True
    assert signature.name in {item["file"] for item in receipt["artifacts"]}
    # The .deb updates through the package manager, never the in-app updater.
    assert not (output / "platefold_0.3.0~dev.6_amd64.deb.sig").exists()


def test_packaged_smoke_accepts_only_the_supported_sandbox_refusal():
    guidance = "Chromium could not start its security sandbox. See LINUX-INSTALL.md."
    refusal = {
        "status": "failed",
        "error": {"code": "browser_sandbox_unavailable", "message": guidance},
    }
    assert smoke_linux_desktop.sandbox_refusal(refusal)["code"] == "browser_sandbox_unavailable"
    other = {"status": "failed", "error": {"code": "export_failed", "message": "boom"}}
    assert smoke_linux_desktop.sandbox_refusal(other) is None
    assert smoke_linux_desktop.sandbox_refusal({"status": "completed"}) is None
    relayed = {
        "status": "failed",
        "error": {"code": "browser_sandbox_unavailable", "message": guidance + " --no-sandbox"},
    }
    with pytest.raises(SystemExit, match="supported guidance"):
        smoke_linux_desktop.sandbox_refusal(relayed)
