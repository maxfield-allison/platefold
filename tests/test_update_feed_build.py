"""Release metadata boundaries; cryptographic checks live in the Rust verifier."""

import base64
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import build_update_feed as feed


def candidate(tmp_path):
    name = "PlateFold_1.0.0_x64-online-setup.exe"
    path = tmp_path / name
    path.write_bytes(b"synthetic artifact")
    (tmp_path / (name + ".sig")).write_text("c2ln")
    item = {
        "file": name,
        "size": path.stat().st_size,
        "sha256": feed.digest(path),
        "url": f"https://github.com/maxfield-allison/platefold/releases/download/v1.0.0/{name}",
    }
    manifest = {
        "version": "1.0.0",
        "channel": "stable",
        "notes": "Release",
        "published": True,
        "artifacts": {"windows-x86_64-nsis": item},
    }
    return manifest, path


def verified(path, signature, *, binding):
    # verify_updater_artifact's receipt for an artifact signed as binding.
    version, target = binding
    return {
        "verified": True,
        "sha256": feed.digest(path),
        "size": path.stat().st_size,
        "public_key": "test-public-key",
        "key_kind": "artifact",
        "binding": {"version": version, "platform": target, "file": path.name},
        "trusted_comment": feed.artifact_binding(version, target, path.name),
    }


def test_manifest_binds_exact_version_artifact_signature_and_expiry(tmp_path):
    manifest, _ = candidate(tmp_path)
    result = feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604800, verified)
    assert result["expires_at"] == 2000604800
    assert result["version"] == "1.0.0"
    assert result["artifacts"]["windows-x86_64-nsis"]["signature"] == "c2ln"
    assert "file" not in result["artifacts"]["windows-x86_64-nsis"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("url", "https://evil.invalid/setup.exe"),
        ("url", "https://github.com/maxfield-allison/platefold/releases/latest/setup.exe"),
        ("sha256", "0" * 64),
        ("size", 512 * 1024 * 1024 + 1),
        ("file", "../escape.exe"),
    ],
)
def test_substituted_manifest_fields_fail_before_crypto(tmp_path, field, value):
    manifest, _ = candidate(tmp_path)
    manifest["artifacts"]["windows-x86_64-nsis"][field] = value

    def forbidden(*args):
        pytest.fail("Invalid manifest reached signature verification")

    with pytest.raises(ValueError):
        feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604800, forbidden)


def test_mutated_artifact_and_unavailable_or_expired_release_refused(tmp_path):
    manifest, path = candidate(tmp_path)
    path.write_bytes(b"altered")
    with pytest.raises(ValueError, match="bytes differ"):
        feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604800, verified)
    manifest, path = candidate(tmp_path)
    manifest["published"] = False
    with pytest.raises(ValueError, match="available"):
        feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604800, verified)
    manifest["published"] = True
    with pytest.raises(ValueError, match="lifetime"):
        feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604801, verified)


def test_signing_uses_metadata_environment_and_exact_serialized_payload(tmp_path, monkeypatch):
    manifest, _ = candidate(tmp_path)
    payload = feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604800, verified)
    monkeypatch.setenv("PLATEFOLD_METADATA_SIGNING_PRIVATE_KEY", "synthetic-metadata-key")
    monkeypatch.setenv("PLATEFOLD_METADATA_SIGNING_PRIVATE_KEY_PASSWORD", "synthetic-password")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "synthetic-artifact-key")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PATH", "do-not-use")
    signed = []

    def sign(command, **kwargs):
        assert all("synthetic-" not in arg for arg in command)
        assert kwargs["env"]["TAURI_SIGNING_PRIVATE_KEY"] == "synthetic-metadata-key"
        assert "TAURI_SIGNING_PRIVATE_KEY_PATH" not in kwargs["env"]
        source = Path(command[-1])
        signed.append(source.read_text())
        source.with_suffix(".json.sig").write_text(
            base64.b64encode(b"synthetic signature").decode()
        )
        return SimpleNamespace(returncode=0)

    def verify_metadata(path, signature, *, metadata):
        assert metadata is True
        assert path.read_text() == signed[-1]

    monkeypatch.setattr(feed.subprocess, "run", sign)
    monkeypatch.setattr(feed, "verify", verify_metadata)
    output = tmp_path / "stable.json"
    feed.sign_payload(payload, output)
    envelope = json.loads(output.read_text())
    assert envelope["payload"] == signed[-1]
    assert envelope["signature"] == "synthetic signature"
    assert (
        envelope["platforms"]["windows-x86_64-nsis"]["url"]
        == payload["artifacts"]["windows-x86_64-nsis"]["url"]
    )
    with pytest.raises(FileExistsError):
        feed.sign_payload(payload, output)


def test_wrong_signature_or_missing_metadata_key_cannot_write_feed(tmp_path, monkeypatch):
    manifest, _ = candidate(tmp_path)

    def wrong(*args, **kwargs):
        raise ValueError("wrong signature")

    with pytest.raises(ValueError, match="wrong signature"):
        feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604800, wrong)
    monkeypatch.delenv("PLATEFOLD_METADATA_SIGNING_PRIVATE_KEY", raising=False)
    with pytest.raises(ValueError, match="separate metadata"):
        feed.sign_payload({}, tmp_path / "stable.json")
    assert not (tmp_path / "stable.json").exists()


def test_the_verifier_is_asked_for_the_manifest_release_and_must_confirm_it(tmp_path):
    manifest, path = candidate(tmp_path)
    asked = []

    def verifier(path, signature, *, binding):
        asked.append(binding)
        return verified(path, signature, binding=binding)

    feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604800, verifier)
    assert asked == [("1.0.0", "windows-x86_64-nsis")]

    # A receipt for another release, or with no binding, refuses the feed.
    for change in (
        {"binding": {"version": "0.9.0", "platform": "windows-x86_64-nsis", "file": path.name}},
        {"binding": {"version": "1.0.0", "platform": "linux-x86_64-appimage", "file": path.name}},
        {"trusted_comment": "timestamp:1\tfile:" + path.name},
        {"key_kind": "metadata"},
    ):

        def other(path, signature, *, binding, change=change):
            return dict(verified(path, signature, binding=binding), **change)

        with pytest.raises(ValueError):
            feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604800, other)
    receipt = verified(path, None, binding=("1.0.0", "windows-x86_64-nsis"))
    del receipt["binding"]
    with pytest.raises(ValueError, match=r"is not signed as 1\.0\.0"):
        feed.payload_from_manifest(manifest, tmp_path, 2000000000, 604800, lambda *a, **k: receipt)


def test_the_rust_verifier_gets_the_release_binding_for_artifacts(tmp_path, monkeypatch):
    _, path = candidate(tmp_path)
    signature = tmp_path / f"{path.name}.sig"
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        binding = command[command.index("--binding") + 1 : command.index("--binding") + 3]
        receipt = verified(path, signature, binding=tuple(binding))
        return SimpleNamespace(returncode=0, stdout=json.dumps(receipt))

    monkeypatch.setattr(feed.subprocess, "run", run)
    # By default the binding is the one the release file name implies.
    assert feed.verify(path, signature)["binding"]["version"] == "1.0.0"
    assert commands[-1][-5:] == [
        "--binding",
        "1.0.0",
        "windows-x86_64-nsis",
        str(path.resolve()),
        str(signature.resolve()),
    ]
    feed.verify(path, signature, binding=("1.0.0", "windows-x86_64-nsis"))
    with pytest.raises(ValueError, match="not a release updater artifact"):
        feed.verify(tmp_path / "setup.exe", signature)

    def mismatched(command, **kwargs):
        receipt = verified(path, signature, binding=("0.9.0", "windows-x86_64-nsis"))
        return SimpleNamespace(returncode=0, stdout=json.dumps(receipt))

    monkeypatch.setattr(feed.subprocess, "run", mismatched)
    with pytest.raises(ValueError, match=r"is not signed as 1\.0\.0"):
        feed.verify(path, signature)


@pytest.mark.parametrize(
    ("name", "release"),
    [
        ("PlateFold_1.0.0_x64-online-setup.exe", ("1.0.0", "windows-x86_64-nsis")),
        ("PlateFold_1.0.0_x64-offline-setup.exe", ("1.0.0", "windows-x86_64-nsis")),
        ("PlateFold_1.1.0-rc.1_macos-arm64.app.tar.gz", ("1.1.0-rc.1", "macos-aarch64-app")),
        ("PlateFold_1.0.0_macos-x86_64.app.tar.gz", ("1.0.0", "macos-x86_64-app")),
        ("PlateFold_2.0.0_linux-x86_64.AppImage", ("2.0.0", "linux-x86_64-appimage")),
        ("PlateFold_2.0.0_linux-arm64.AppImage", ("2.0.0", "linux-aarch64-appimage")),
    ],
)
def test_release_file_names_give_the_release_their_signature_names(name, release):
    assert feed.release_binding(name) == release
    assert feed.artifact_binding(*release, name) == (
        f"platefold-artifact v1 version={release[0]} platform={release[1]} file={name}"
    )


@pytest.mark.parametrize(
    "name",
    [
        "PlateFold_1.0.0_windows-x64.zip",
        "platefold_1.0.0_amd64.deb",
        "PlateFold_1.0.0+build_linux-x86_64.AppImage",
        "PlateFold_1.0_linux-x86_64.AppImage",
        "evil/PlateFold_1.0.0_linux-x86_64.AppImage",
    ],
)
def test_other_files_have_no_release_binding(name):
    with pytest.raises(ValueError):
        feed.release_binding(name)


@pytest.mark.parametrize(
    "parts",
    [
        ("1.0.0 platform=x", "linux-x86_64-appimage", "a.AppImage"),
        ("1.0.0", "linux x86_64", "a.AppImage"),
        ("1.0.0", "linux-x86_64-appimage", "a%20b.AppImage"),
        ("1.0.0", "linux-x86_64-appimage", ".AppImage"),
        ("", "linux-x86_64-appimage", "a.AppImage"),
        (None, "linux-x86_64-appimage", "a.AppImage"),
    ],
)
def test_binding_parts_are_single_safe_tokens(parts):
    with pytest.raises(ValueError):
        feed.artifact_binding(*parts)


def test_trusted_comment_reads_a_tauri_signature_without_trusting_it():
    document = "untrusted comment: x\nRWQ=\ntrusted comment: platefold-artifact v1 a\nZ==\n"
    encoded = base64.b64encode(document.encode()).decode()
    assert feed.trusted_comment(encoded) == "platefold-artifact v1 a"
    for bad in ("not base64!", base64.b64encode(b"one line").decode()):
        with pytest.raises(ValueError):
            feed.trusted_comment(bad)
