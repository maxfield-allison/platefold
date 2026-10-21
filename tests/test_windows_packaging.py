"""Exercise preservation, Windows tool resolution and package rejection gates."""

import base64
import hashlib
import io
import json
import os
import shutil
import stat
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import build_desktop, check_windows_package


def test_worker_bundle_supplies_the_license_missing_from_cobble_wheel():
    root = Path(__file__).parents[1]
    spec = (root / "packaging/desktop-worker.spec").read_text(encoding="utf-8")
    license_name = "packaging/licenses/cobble-BSD-2-Clause.txt"

    assert license_name in spec
    assert license_name in build_desktop.build_inputs()
    assert "Copyright (c) 2013, Michael Williamson" in (root / license_name).read_text()


def test_rebuild_preserves_stale_staging_outside_the_new_payload(tmp_path, monkeypatch):
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    staging = tmp_path / "desktop/src-tauri/target/release/runtime"
    staging.mkdir(parents=True)
    (staging / "obsolete.pyd").write_bytes(b"prior build")
    previous = build_desktop.archive_windows_staging()
    assert not staging.exists()
    assert (previous / "obsolete.pyd").read_bytes() == b"prior build"
    assert previous.is_relative_to(tmp_path / "build")


def test_nsis_hash_changes_only_the_unique_bundle_marker(tmp_path):
    portable = b"PE prefix __TAURI_BUNDLE_TYPE_VAR_UNK suffix"
    file = tmp_path / "platefold-desktop.exe"
    file.write_bytes(portable)
    record = build_desktop.windows_installer_gui(tmp_path)
    assert (
        record["sha256"]
        == hashlib.sha256(b"PE prefix __TAURI_BUNDLE_TYPE_VAR_NSS suffix").hexdigest()
    )
    assert file.read_bytes() == portable
    file.write_bytes(portable * 2)
    with pytest.raises(SystemExit, match="one Tauri bundle marker"):
        build_desktop.windows_installer_gui(tmp_path)


def test_upgrade_check_rejects_stale_runtime_but_preserves_user_cache(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "current.dll").write_bytes(b"current")
    (tmp_path / "diagram-cache").mkdir()
    (tmp_path / "diagram-cache/user.svg").write_bytes(b"user cache")
    receipt = {"payload": [{"file": "runtime/current.dll"}]}
    check_windows_package.check_runtime_inventory(tmp_path, receipt)
    stale = runtime / "obsolete.pyd"
    stale.write_bytes(b"stale")
    with pytest.raises(ValueError, match="Unrecorded runtime files"):
        check_windows_package.check_runtime_inventory(tmp_path, receipt)
    assert stale.read_bytes() == b"stale"


def test_existing_artifacts_survive_repackaging(tmp_path):
    sentinel = tmp_path / "previous-setup.exe"
    sentinel.write_bytes(b"retained bytes")
    with pytest.raises(SystemExit, match="Preserving existing artifacts"):
        build_desktop.package_downloads(
            build_desktop.PLATFORMS["win32"], "0.3.0-dev.6", output_dir=tmp_path
        )
    assert sentinel.read_bytes() == b"retained bytes"


@pytest.mark.skipif(
    os.name != "nt" or not shutil.which("npm.cmd"), reason="Needs native Windows npm"
)
def test_windows_receipt_executes_the_real_npm_command():
    assert build_desktop.command_version("npm.cmd", "--version").split(".")[0].isdigit()


@pytest.mark.parametrize(
    "names",
    [
        ["PlateFold/../../escape"],
        ["PlateFold/C:escape"],
        ["PlateFold/a\\b"],
        ["PlateFold/a", "PlateFold/A"],
        ["PlateFold/./a"],
        ["PlateFold/a."],
    ],
)
def test_zip_rejects_unsafe_and_ambiguous_names(names):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name in names:
            item = zipfile.ZipInfo(name)
            item.filename = name  # Preserve hostile separators on Windows too.
            archive.writestr(item, b"test")
    with zipfile.ZipFile(stream) as archive, pytest.raises(ValueError, match="Unsafe or duplicate"):
        check_windows_package.checked_archive(archive)


def test_corrupt_artifact_is_rejected_before_extraction(tmp_path):
    archive = tmp_path / "test.zip"
    archive.write_bytes(b"wrong bytes")
    receipt = {
        "source_commit": "abc",
        "source_dirty": False,
        "platform": "windows",
        "architecture": "x86_64",
        "artifacts": [{"file": archive.name, "size": 11, "sha256": "0" * 64}],
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    destination = tmp_path / "extract"
    with pytest.raises(ValueError, match="Artifact size/hash mismatch"):
        check_windows_package.verify(path, destination, "abc", tmp_path / "requirements.txt")
    assert not destination.exists()


def test_license_check_requires_the_pinned_version_and_license(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n")
    metadata = tmp_path / "example-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: example\nVersion: 1.0\n")
    with pytest.raises(ValueError, match="runtime metadata/license"):
        check_windows_package.check_packages(tmp_path, requirements)
    (metadata / "licenses").mkdir()
    (metadata / "licenses/Apache-2.0.txt").write_text("License fixture")
    assert (
        check_windows_package.check_packages(tmp_path, requirements)["example"]["version"] == "1.0"
    )
    requirements.write_text("example==2.0\n")
    with pytest.raises(ValueError, match="runtime metadata/license"):
        check_windows_package.check_packages(tmp_path, requirements)


def test_license_check_accepts_british_spelling(tmp_path):
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("example==1.0\n")
    metadata = tmp_path / "example-1.0.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text("Name: example\nVersion: 1.0\n")
    (metadata / "LICENCE.rst").write_text("License fixture")

    assert (
        check_windows_package.check_packages(tmp_path, requirements)["example"]["version"] == "1.0"
    )


def test_staging_reparse_point_is_rejected_on_python_311(tmp_path, monkeypatch):
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    staging = tmp_path / "desktop/src-tauri/target/release/runtime"
    staging.mkdir(parents=True)
    original = Path.lstat

    def attributes(path, *args, **kwargs):
        if path == staging:
            return SimpleNamespace(
                st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", attributes)
    with pytest.raises(SystemExit, match="redirected runtime"):
        build_desktop.archive_windows_staging()
    assert staging.exists()


def test_installer_overlays_change_only_webview_distribution():
    root = Path(__file__).parents[1]
    for variant, mode in (("online", "downloadBootstrapper"), ("offline", "offlineInstaller")):
        overlay = root / "packaging" / f"tauri-windows-{variant}.conf.json"
        assert json.loads(overlay.read_text()) == {
            "bundle": {"windows": {"webviewInstallMode": {"type": mode}}}
        }
        assert overlay.relative_to(root).as_posix() in build_desktop.build_inputs()


def simulated_windows_build(tmp_path, monkeypatch, mutate_runtime=False):
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    desktop = tmp_path / "desktop"
    release = desktop / "src-tauri/target/release"
    runtime = release / "runtime"
    runtime.mkdir(parents=True)
    browser = runtime / "chrome-headless-shell.exe"
    browser.write_bytes(b"same offline browser")
    commands = []

    def build(command, cwd, stage_file):
        assert cwd == desktop
        commands.append(command)
        variant = (
            "online" if "../packaging/tauri-windows-online.conf.json" in command else "offline"
        )
        (release / "platefold-desktop.exe").write_bytes(
            variant.encode() + b"__TAURI_BUNDLE_TYPE_VAR_UNK"
        )
        installer = release / "bundle/nsis/PlateFold_test_x64-setup.exe"
        installer.parent.mkdir(parents=True, exist_ok=True)
        installer.write_bytes(variant.encode())
        if mutate_runtime and variant == "offline":
            browser.write_bytes(b"different browser")

    monkeypatch.setattr(build_desktop, "run_native_build", build)
    return desktop, release, commands


def test_both_installer_bytes_and_gui_hashes_survive_second_build(tmp_path, monkeypatch):
    desktop, release, commands = simulated_windows_build(tmp_path, monkeypatch)
    installers = build_desktop.build_windows_installers(
        ["tauri", "build", "--ci"], desktop, "test", ("online", "offline")
    )
    assert len(commands) == 2
    assert all(command[-4:] == ["--bundles", "nsis", "--", "--locked"] for command in commands)
    for variant, evidence in installers.items():
        assert evidence["path"].read_bytes() == variant.encode()
        assert evidence["path"].name == f"PlateFold_test_x64-{variant}-setup.exe"
        assert (
            evidence["gui"]["sha256"]
            == hashlib.sha256(variant.encode() + b"__TAURI_BUNDLE_TYPE_VAR_NSS").hexdigest()
        )
    assert installers["online"]["gui"] != installers["offline"]["gui"]
    assert (release / "platefold-desktop.exe").read_bytes().startswith(b"offline")


def test_installer_variants_cannot_silently_bundle_different_runtimes(tmp_path, monkeypatch):
    desktop, _, _ = simulated_windows_build(tmp_path, monkeypatch, mutate_runtime=True)
    with pytest.raises(SystemExit, match="same worker/PDF runtime"):
        build_desktop.build_windows_installers(
            ["tauri", "build", "--ci"], desktop, "test", ("online", "offline")
        )


def test_variant_receipt_and_package_checks_are_unambiguous(tmp_path, monkeypatch):
    desktop, release, _ = simulated_windows_build(tmp_path, monkeypatch)
    installers = build_desktop.build_windows_installers(
        ["tauri", "build", "--ci"], desktop, "test", ("online", "offline")
    )
    for file in (
        "LICENSE",
        "docs/windows-desktop.md",
        "docs/automation.md",
        "desktop/THIRD_PARTY.md",
    ):
        path = tmp_path / file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    monkeypatch.setattr(build_desktop, "source_state", lambda: ("abc", False))
    monkeypatch.setattr(build_desktop, "build_inputs", lambda: {})
    monkeypatch.setattr(build_desktop, "command_version", lambda *args: "fixture")
    monkeypatch.setattr(build_desktop.importlib.metadata, "version", lambda *args: "fixture")
    output = tmp_path / "downloads"
    build_desktop.package_downloads(
        build_desktop.PLATFORMS["win32"],
        "test",
        output_dir=output,
        windows_installers=installers,
    )
    path = output / "BUILD-RECEIPT.json"
    receipt = json.loads(path.read_text())
    assert receipt["schema_version"] == 2
    assert len(receipt["artifacts"]) == 3
    archive = check_windows_package.check_artifacts(path, receipt)
    with zipfile.ZipFile(archive) as zipped:
        assert (
            zipped.read("PlateFold/platefold-desktop.exe")
            == (release / "platefold-desktop.exe").read_bytes()
        )
    with pytest.raises(ValueError, match="Choose --installer-variant"):
        list(check_windows_package.payload_entries(receipt, installed=True))
    for variant in ("online", "offline"):
        installed_gui = next(check_windows_package.payload_entries(receipt, True, variant))
        assert installed_gui == installers[variant]["gui"]
    del receipt["installer_variants"]["online"]
    with pytest.raises(ValueError, match="every Windows installer variant"):
        check_windows_package.validate_installer_variants(receipt)


def test_legacy_installer_gui_evidence_remains_readable():
    gui = {"file": "platefold-desktop.exe", "sha256": "legacy", "size": 12}
    receipt = {"installer_gui": gui, "payload": [{"file": "platefold-desktop.exe"}]}
    assert list(check_windows_package.payload_entries(receipt, True)) == [gui]
    with pytest.raises(ValueError, match="Legacy receipt"):
        check_windows_package.installer_gui(receipt, "online")


def test_attended_installer_uses_captured_signature_hash_not_predicted_marker(
    tmp_path, monkeypatch
):
    desktop, _release, commands = simulated_windows_build(tmp_path, monkeypatch)
    verified = []
    captured = {
        variant: {"file": "platefold-desktop.exe", "sha256": variant + "-signed"}
        for variant in ("online", "offline")
    }
    signer = SimpleNamespace(
        overlay=lambda variant: variant + "-signing.json",
        build=lambda callback, *args: callback(*args),
        verify_installer=lambda path: verified.append(path.read_bytes()),
        installed_gui=lambda variant: captured[variant],
    )
    monkeypatch.setattr(
        build_desktop,
        "windows_installer_gui",
        lambda *_: pytest.fail("Cannot predict signed bytes from unsigned GUI"),
    )
    installers = build_desktop.build_windows_installers(
        ["tauri", "build", "--ci"],
        desktop,
        "test",
        ("online", "offline"),
        signing=signer,
    )
    assert verified == [b"online", b"offline"]
    for index, variant in enumerate(("online", "offline")):
        assert variant + "-signing.json" in commands[index]
        assert installers[variant]["gui"] == captured[variant]


@pytest.mark.parametrize(
    "policy",
    [
        {"public_keys": [], "artifact_public_keys": [], "stable": None, "prerelease": None},
        {
            "public_keys": ["same"],
            "artifact_public_keys": ["same"],
            "stable": "https://example.invalid/feed",
        },
        {
            "public_keys": ["metadata"],
            "artifact_public_keys": ["artifact"],
            "stable": None,
            "prerelease": None,
        },
    ],
)
def test_updater_build_refuses_unconfigured_trust(tmp_path, monkeypatch, policy):
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    (tmp_path / "packaging").mkdir()
    (tmp_path / "packaging/update-policy.json").write_text(json.dumps(policy))
    with pytest.raises(SystemExit):
        build_desktop.validate_updater_policy()


@pytest.mark.parametrize(
    "failure",
    [None, "wrong_key", "changed_bytes", "missing_signature", "other_release", "wrong_version"],
)
def test_updater_receipts_require_verification_of_each_final_installer(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    output = tmp_path / "dist"
    output.mkdir()
    name = "PlateFold_1.0.0_x64-online-setup.exe"
    artifact = output / name
    artifact.write_bytes(b"final signed installer")
    (output / f"{name}.sig").write_text("signature fixture passed to real Rust verifier")
    item = {
        "file": artifact.name,
        "size": artifact.stat().st_size,
        "sha256": hashlib.sha256(artifact.read_bytes()).hexdigest(),
    }
    receipt = {
        "version": "1.0.1" if failure == "wrong_version" else "1.0.0",
        "artifacts": [item, {"file": f"{name}.sig"}],
        "installer_variants": {"online": {"file": name}},
    }
    if failure == "missing_signature":
        receipt["artifacts"].pop()
    path = output / "BUILD-RECEIPT.json"
    path.write_text(json.dumps(receipt))

    def verifier(command, **kwargs):
        assert command[-5:] == [
            "--binding",
            "1.0.0",
            "windows-x86_64-nsis",
            str(artifact),
            str(output / f"{name}.sig"),
        ]
        assert "verify_updater_artifact" in command
        assert kwargs["capture_output"] is True
        bound = "0.9.0" if failure == "other_release" else "1.0.0"
        proof = dict(
            item,
            verified=True,
            public_key="verified compiled public key",
            key_kind="artifact",
            binding={"version": bound, "platform": "windows-x86_64-nsis", "file": name},
            trusted_comment=build_desktop.artifact_binding(bound, "windows-x86_64-nsis", name),
        )
        if failure == "changed_bytes":
            proof["sha256"] = "0" * 64
        return SimpleNamespace(
            returncode=1 if failure == "wrong_key" else 0,
            stdout=json.dumps(proof),
            stderr="Verifier rejected signature\npassword=private-fixture\n",
        )

    monkeypatch.setattr(build_desktop.subprocess, "run", verifier)
    if failure:
        with pytest.raises(SystemExit):
            build_desktop.verify_updater_downloads(output)
        assert "updater_signatures" not in json.loads(path.read_text())
        if failure == "wrong_key":
            diagnostic = (tmp_path / "build/updater-verification-summary.txt").read_text()
            assert "Verifier rejected signature" in diagnostic
            assert "private-fixture" not in diagnostic
    else:
        build_desktop.verify_updater_downloads(output)
        proof = json.loads(path.read_text())["updater_signatures"][0]
        assert proof["sha256"] == item["sha256"]
        assert proof["verified"] is True


@pytest.mark.parametrize("failure", [None, "signer", "unbound", "wrong_name", "other_version"])
def test_updater_artifacts_are_signed_as_their_release_under_the_final_name(
    tmp_path, monkeypatch, failure
):
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "private-test-fixture")
    name = "setup.exe" if failure == "wrong_name" else "PlateFold_1.0.0_x64-offline-setup.exe"
    installer = tmp_path / name
    installer.write_bytes(b"final installer")
    comment = build_desktop.artifact_binding("1.0.0", "windows-x86_64-nsis", name)
    commands = []

    def signer(command, **kwargs):
        commands.append(command)
        assert kwargs["stdin"] == build_desktop.subprocess.DEVNULL
        Path(f"{installer}.sig").write_text("bound signature")
        stdout = json.dumps({"trusted_comment": "timestamp:1" if failure == "unbound" else comment})
        return SimpleNamespace(
            returncode=int(failure == "signer"),
            stdout=stdout,
            stderr="error: key\nTAURI_SIGNING_PRIVATE_KEY=private-test-fixture\n",
        )

    monkeypatch.setattr(build_desktop.subprocess, "run", signer)
    version = "1.0.1" if failure == "other_version" else "1.0.0"
    if failure:
        with pytest.raises(SystemExit) as error:
            build_desktop.sign_updater_artifact(installer, version)
        assert "private-test-fixture" not in str(error.value)
        if failure == "signer":
            summary = (tmp_path / "build/updater-signing-summary.txt").read_text()
            assert "private-test-fixture" not in summary
        if failure in ("wrong_name", "other_version"):
            assert commands == []
        return
    assert build_desktop.sign_updater_artifact(installer, version) == Path(f"{installer}.sig")
    (command,) = commands
    assert "sign_updater_artifact" in command
    assert command[command.index("--") :] == [
        "--",
        "--version",
        "1.0.0",
        "--platform",
        "windows-x86_64-nsis",
        str(installer.resolve()),
    ]
    assert "private-test-fixture" not in " ".join(command)


def test_updater_signature_retention_requires_the_tauri_updater_build(tmp_path, monkeypatch):
    source = tmp_path / "bundle.exe"
    source.write_bytes(b"installer")
    target = tmp_path / "PlateFold_1.0.0_x64-online-setup.exe"
    target.write_bytes(b"installer")
    downloads = []
    monkeypatch.setattr(
        build_desktop, "sign_updater_artifact", lambda path, version: Path(f"{path}.sig")
    )
    build_desktop.retain_updater_signature(source, target, downloads, False, "1.0.0")
    assert downloads == []
    with pytest.raises(SystemExit, match="Missing signed updater artifact"):
        build_desktop.retain_updater_signature(source, target, downloads, True, "1.0.0")
    Path(f"{source}.sig").write_text("tauri signature")
    build_desktop.retain_updater_signature(source, target, downloads, True, "1.0.0")
    assert downloads == [Path(f"{target}.sig")]


@pytest.mark.parametrize("invalid_key", [None, "", "not-base64", "RWQ=", "bad\nheader"])
def test_updater_build_supplies_boxed_artifact_key_before_native_signing(
    tmp_path, monkeypatch, invalid_key
):
    source = Path(__file__).parents[1] / "packaging"
    packaging = tmp_path / "packaging"
    packaging.mkdir()
    policy = json.loads((source / "update-policy.json").read_text())
    original_key = policy["artifact_public_keys"][0]
    if invalid_key is not None:
        policy["artifact_public_keys"][0] = invalid_key
    (packaging / "update-policy.json").write_text(json.dumps(policy))
    shutil.copy2(source / "tauri-updater.conf.json", packaging)
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    if invalid_key is not None:
        with pytest.raises(SystemExit, match="public key"):
            build_desktop.updater_build_config()
        assert not (tmp_path / "build").exists()
        return
    generated = build_desktop.updater_build_config()
    config = json.loads(generated.read_text())
    # Tauri CLI pub_key() base64-decodes then parses a Minisign PublicKeyBox.
    document = base64.b64decode(config["plugins"]["updater"]["pubkey"], validate=True).decode()
    comment, raw = document.splitlines()
    assert comment.startswith("untrusted comment: ")
    assert raw == original_key
    assert raw not in policy["public_keys"]
    assert len(base64.b64decode(raw, validate=True)) == 42
    assert config["bundle"]["createUpdaterArtifacts"] is True
    assert generated.is_relative_to(tmp_path / "build")
    assert json.loads((packaging / "tauri-updater.conf.json").read_text()).get("plugins") is None
    # A second build gets its own overlay and cannot overwrite an existing one.
    assert build_desktop.updater_build_config() != generated


@pytest.mark.parametrize("case", ["artifact", "metadata", "malformed", "signer_failure", "timeout"])
def test_updater_key_preflight_rejects_miswired_signer_without_leaking_credentials(
    tmp_path, monkeypatch, case
):
    policy = json.loads((build_desktop.ROOT / "packaging/update-policy.json").read_text())
    monkeypatch.setattr(build_desktop, "ROOT", tmp_path)
    monkeypatch.setattr(build_desktop, "validate_updater_policy", lambda: policy)
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", "private-test-fixture")

    def signer(command, **kwargs):
        assert "private-test-fixture" not in str(command)
        assert kwargs["stdin"] == build_desktop.subprocess.DEVNULL
        assert kwargs["timeout"] == 60
        if case == "timeout":
            raise build_desktop.subprocess.TimeoutExpired(command, 60)
        fixture = Path(command[-1])
        assert fixture.read_text() == "PlateFold updater key identity check\n"
        group = "public_keys" if case == "metadata" else "artifact_public_keys"
        key_id = base64.b64decode(policy[group][0])[2:10]
        signature = base64.b64encode(b"ED" + key_id + bytes(64)).decode()
        document = f"untrusted comment: synthetic key identity fixture\n{signature}\n"
        fixture.with_suffix(".txt.sig").write_text(
            "invalid" if case == "malformed" else base64.b64encode(document.encode()).decode()
        )
        return SimpleNamespace(
            returncode=int(case == "signer_failure"),
            stdout="private-test-fixture",
            stderr="private-test-fixture",
        )

    monkeypatch.setattr(build_desktop.subprocess, "run", signer)
    if case == "artifact":
        build_desktop.preflight_updater_key("npm")
    else:
        with pytest.raises(SystemExit) as error:
            build_desktop.preflight_updater_key("npm")
        assert "private-test-fixture" not in str(error.value)
        if case == "metadata":
            assert "artifact secret store, not the metadata store" in str(error.value)
    assert list((tmp_path / "build").iterdir()) == []


def test_updater_key_preflight_with_real_tauri_signatures(tmp_path, monkeypatch):
    desktop = build_desktop.ROOT / "desktop"
    if not (desktop / "node_modules/@tauri-apps/cli").is_dir():
        pytest.skip("requires installed locked Tauri CLI")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if npm is None:
        pytest.skip("requires npm")
    key_file = tmp_path / "disposable.key"
    # A password, as production keys have: Windows cannot hand a child process an
    # empty environment variable, so an empty password would reach the signer as
    # no password at all, and it would wait for one on the console.
    password = "disposable-test-password"
    generate = ["signer", "generate", "--ci", "-p", password, "--write-keys", str(key_file)]
    build_desktop.subprocess.run(
        [npm, "run", "tauri", "--", *generate],
        cwd=desktop,
        stdin=build_desktop.subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=60,
    )
    public_document = base64.b64decode(key_file.with_suffix(".key.pub").read_text()).decode()
    key = public_document.splitlines()[1]
    policy = json.loads((build_desktop.ROOT / "packaging/update-policy.json").read_text())
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY", key_file.read_text())
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PASSWORD", password)
    monkeypatch.setattr(build_desktop, "validate_updater_policy", lambda: policy)
    # A real signature from a different key must fail against the production pin.
    with pytest.raises(SystemExit, match="does not match"):
        build_desktop.preflight_updater_key(npm)
    policy["artifact_public_keys"] = [key]
    build_desktop.preflight_updater_key(npm)
