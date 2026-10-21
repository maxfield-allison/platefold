"""Release manifest, checksums, evidence signature and source mapping.

Every key here is generated for the test and discarded. The real Tauri signer
is exercised only when the locked CLI is installed, with its own throwaway key.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from tools import assemble_release as release

VERSION = "1.0.0"
PRIVATE_FILES = {"README.md": b"private readme\n", "design/notes.md": b"private notes\n"}
PUBLIC_FILES = {"README.md": b"public readme\n", "platefold/__init__.py": b"__version__ = 1\n"}
NOW = datetime.datetime(2026, 9, 23, 12, 0, tzinfo=datetime.UTC)


class ThrowawayKey:
    """A minisign-format Ed25519 key that exists only for one test."""

    def __init__(self) -> None:
        self.secret = Ed25519PrivateKey.generate()
        self.key_id = os.urandom(8)
        raw = self.secret.public_key().public_bytes_raw()
        line = base64.b64encode(b"Ed" + self.key_id + raw).decode()
        self.public_document = f"untrusted comment: throwaway test key\n{line}\n"

    def sign(self, path: Path) -> str:
        data = path.read_bytes()
        signature = self.secret.sign(hashlib.blake2b(data, digest_size=64).digest())
        trusted = f"timestamp:0\tfile:{path.name}"
        global_signature = self.secret.sign(signature + trusted.encode())
        return "".join(
            f"{line}\n"
            for line in [
                "untrusted comment: signature from throwaway test key",
                base64.b64encode(b"ED" + self.key_id + signature).decode(),
                f"trusted comment: {trusted}",
                base64.b64encode(global_signature).decode(),
            ]
        )


def release_files(directory: Path, version: str = VERSION) -> dict[str, bytes]:
    names = [
        f"PlateFold_{version}_x64-online-setup.exe",
        f"PlateFold_{version}_x64-offline-setup.exe",
        f"PlateFold_{version}_windows-x64.zip",
        f"PlateFold_{version}_macos-arm64.dmg",
        f"PlateFold_{version}_macos-arm64.app.tar.gz",
        f"PlateFold_{version}_linux-x86_64.AppImage",
        f"platefold_{version}_amd64.deb",
        f"platefold-{version}-py3-none-any.whl",
        f"platefold-{version}.tar.gz",
        "platefold.apparmor",
        "LINUX-INSTALL.md",
    ]
    directory.mkdir(parents=True)
    written = {}
    for name in names:
        written[name] = f"synthetic bytes of {name}\n".encode()
    for name in names:
        if name.endswith((".exe", ".app.tar.gz", ".AppImage")):
            written[f"{name}.sig"] = base64.b64encode(f"updater signature {name}".encode())
    for name, data in written.items():
        (directory / name).write_bytes(data)
    return written


def accept_updater(artifact: Path, signature: Path) -> dict:
    return {
        "verified": True,
        "key_kind": "artifact",
        "sha256": release.digest(artifact),
        "size": artifact.stat().st_size,
        "public_key": "throwaway",
    }


def repository(path: Path, files: dict[str, bytes]) -> str:
    path.mkdir(parents=True)
    run = ["git", "-C", str(path), "-c", "user.name=Test", "-c", "user.email=test@example.invalid"]
    run += ["-c", "commit.gpgsign=false"]
    subprocess.run([*run[:3], "init", "-q"], check=True)
    for name, data in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run([*run, "add", "--", *files], check=True)
    subprocess.run([*run, "commit", "-q", "-m", "snapshot"], check=True)
    return subprocess.check_output(["git", "-C", str(path), "rev-parse", "HEAD"], text=True).strip()


def listing_digest(files: dict[str, bytes]) -> str:
    lines = "".join(
        f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(files.items())
    )
    return hashlib.sha256(lines.encode()).hexdigest()


# The files each platform job's receipt records, as the protected CI supplies
# them to release:assemble. The macOS updater signature is made after its
# receipt and is checked against the update policy instead.
RECEIPT_FILES = {
    "linux-BUILD-RECEIPT.json": [
        f"PlateFold_{VERSION}_linux-x86_64.AppImage",
        f"PlateFold_{VERSION}_linux-x86_64.AppImage.sig",
        f"platefold_{VERSION}_amd64.deb",
        "platefold.apparmor",
        "LINUX-INSTALL.md",
    ],
    "macos-MACOS-RELEASE-RECEIPT.json": [
        f"PlateFold_{VERSION}_macos-arm64.dmg",
        f"PlateFold_{VERSION}_macos-arm64.app.tar.gz",
    ],
    "windows-BUILD-RECEIPT.json": [
        f"PlateFold_{VERSION}_windows-x64.zip",
        f"PlateFold_{VERSION}_x64-online-setup.exe",
        f"PlateFold_{VERSION}_x64-online-setup.exe.sig",
        f"PlateFold_{VERSION}_x64-offline-setup.exe",
        f"PlateFold_{VERSION}_x64-offline-setup.exe.sig",
    ],
    "python-BUILD-RECEIPT.json": [
        f"platefold-{VERSION}-py3-none-any.whl",
        f"platefold-{VERSION}.tar.gz",
    ],
}


def file_entry(name: str, data: bytes) -> dict:
    return {"file": name, "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def write_build_receipts(directory: Path, files: dict[str, bytes], commit: str) -> list[Path]:
    directory.mkdir()
    paths = []
    for receipt_name, names in RECEIPT_FILES.items():
        path = directory / receipt_name
        path.write_text(
            json.dumps(
                {
                    "version": VERSION,
                    "source_commit": commit,
                    "source_dirty": False,
                    "artifacts": [file_entry(name, files[name]) for name in names],
                }
            )
        )
        paths.append(path)
    return paths


def edit_receipt(path: Path, change) -> None:
    receipt = json.loads(path.read_text())
    change(receipt)
    path.write_text(json.dumps(receipt))


@pytest.fixture
def case(tmp_path, working_git):
    key = ThrowawayKey()
    public_key = tmp_path / "release-evidence.pub"
    public_key.write_text(key.public_document)
    private_commit = repository(tmp_path / "private", PRIVATE_FILES)
    public_commit = repository(tmp_path / "public", PUBLIC_FILES)
    receipt = tmp_path / "public.receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "problems": [],
                "source_commit": private_commit,
                "public_files": len(PUBLIC_FILES),
                "excluded_files": ["design/notes.md"],
                "public_listing_sha256": listing_digest(PUBLIC_FILES),
            }
        )
    )
    files = release_files(tmp_path / "final")
    build_receipts = write_build_receipts(tmp_path / "receipts", files, private_commit)
    arguments = {
        "version": VERSION,
        "artifacts": tmp_path / "final",
        "public_repo": tmp_path / "public",
        "public_rev": public_commit,
        "private_repo": tmp_path / "private",
        "private_commit": private_commit,
        "snapshot_receipt": receipt,
        "build_receipts": build_receipts,
        "output": tmp_path / "evidence",
        "mapping": tmp_path / "private-mapping.json",
        "public_key": public_key,
        "signer": key.sign,
        "updater_verifier": accept_updater,
        "now": NOW,
    }
    return arguments, files, key


def git_value(repo: Path, rev: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), "rev-parse", rev], text=True).strip()


def test_assembles_manifest_checksums_signature_and_private_mapping(case):
    arguments, files, _key = case
    record = release.assemble(**arguments)
    output = arguments["output"]
    public = arguments["public_repo"]

    assert sorted(path.name for path in output.iterdir()) == sorted(release.EVIDENCE_FILES)
    manifest = json.loads((output / "release-manifest.json").read_text())
    # The exact schema publish-pypi.yml checks.
    assert set(manifest) == {
        "schema_version",
        "version",
        "public_tag",
        "public_source_commit",
        "public_source_tree",
        "artifacts",
    }
    assert manifest["public_tag"] == "v1.0.0"
    assert manifest["public_source_commit"] == git_value(public, "HEAD")
    assert manifest["public_source_tree"] == git_value(public, "HEAD^{tree}")
    kinds = {item["name"]: item["kind"] for item in manifest["artifacts"]}
    assert set(kinds) == set(files)
    assert kinds["platefold-1.0.0-py3-none-any.whl"] == "python-wheel"
    assert kinds["platefold-1.0.0.tar.gz"] == "python-sdist"
    assert kinds["PlateFold_1.0.0_macos-arm64.app.tar.gz.sig"] == "updater-signature"
    assert all(set(item) == {"name", "kind", "size", "sha256"} for item in manifest["artifacts"])

    # SHA256SUMS lists every file and the manifest, so one signature covers both.
    sums = (output / "SHA256SUMS").read_text().splitlines()
    assert len(sums) == len(files) + 1
    assert any(line.endswith("  release-manifest.json") for line in sums)
    if shutil.which("sha256sum"):
        combined = arguments["output"].parent / "combined"
        shutil.copytree(arguments["artifacts"], combined)
        shutil.copy(output / "release-manifest.json", combined)
        shutil.copy(output / "SHA256SUMS", combined)
        subprocess.run(["sha256sum", "--check", "--quiet", "SHA256SUMS"], cwd=combined, check=True)

    mapping = json.loads(arguments["mapping"].read_text())
    assert mapping == record
    assert mapping["private_source"] == {
        "commit": arguments["private_commit"],
        "tree": git_value(arguments["private_repo"], "HEAD^{tree}"),
    }
    assert mapping["public_source"]["commit"] == manifest["public_source_commit"]
    assert mapping["public_source"]["parents"] == []
    assert mapping["release_evidence"]["release-manifest.json"]["sha256"] == release.digest(
        output / "release-manifest.json"
    )
    assert mapping["build_receipts"][0]["covers"] == sorted(
        RECEIPT_FILES["linux-BUILD-RECEIPT.json"]
    )
    assert {name for item in mapping["build_receipts"] for name in item["covers"]} == set(files) - {
        "PlateFold_1.0.0_macos-arm64.app.tar.gz.sig"
    }
    # Only the policy-checked updater signature may lack a receipt.
    assert mapping["artifacts_without_build_receipt"] == [
        "PlateFold_1.0.0_macos-arm64.app.tar.gz.sig"
    ]
    assert mapping["created_at"] == "2026-09-23T12:00:00Z"
    # The private commit appears only in the private record.
    for path in output.iterdir():
        assert arguments["private_commit"] not in path.read_text()


def test_verify_accepts_the_cosign_bundle_beside_the_evidence(case, tmp_path):
    arguments, _files, _key = case
    release.assemble(**arguments)
    folder = tmp_path / "evidence-copy"
    shutil.copytree(arguments["output"], folder)
    assert (
        release.verify(folder, arguments["public_key"], partial=True)["cosign_bundle_present"]
        is False
    )

    (folder / release.COSIGN_BUNDLE).write_text("{}")
    result = release.verify(folder, arguments["public_key"], partial=True)
    assert result["cosign_bundle_present"] is True
    assert release.COSIGN_BUNDLE not in result["checked_files"]

    (folder / release.COSIGN_BUNDLE).unlink()
    (folder / release.COSIGN_BUNDLE).symlink_to(folder / release.SUMS)
    with pytest.raises(release.ReleaseError, match="Not an ordinary evidence file"):
        release.verify(folder, arguments["public_key"], partial=True)


def test_verify_accepts_the_release_and_rejects_any_changed_byte(case, tmp_path):
    arguments, _files, _key = case
    release.assemble(**arguments)
    downloads = tmp_path / "downloads"
    shutil.copytree(arguments["artifacts"], downloads)
    for name in release.EVIDENCE_FILES:
        shutil.copy(arguments["output"] / name, downloads)

    result = release.verify(downloads, arguments["public_key"])
    assert result["verified"] is True and result["not_present"] == []

    subset = tmp_path / "subset"
    subset.mkdir()
    for name in (*release.EVIDENCE_FILES, "platefold-1.0.0.tar.gz"):
        shutil.copy(downloads / name, subset)
    with pytest.raises(release.ReleaseError, match="missing"):
        release.verify(subset, arguments["public_key"])
    assert release.verify(subset, arguments["public_key"], partial=True)["checked_files"] == [
        "platefold-1.0.0.tar.gz"
    ]

    (downloads / "platefold-1.0.0.tar.gz").write_bytes(b"substituted")
    with pytest.raises(release.ReleaseError, match="differs from the signed release"):
        release.verify(downloads, arguments["public_key"])


@pytest.mark.parametrize("target", ["SHA256SUMS", "release-manifest.json"])
def test_verify_rejects_edited_evidence(case, target):
    arguments, _files, _key = case
    release.assemble(**arguments)
    output = arguments["output"]
    path = output / target
    path.write_text(path.read_text().replace("1.0.0", "1.0.1", 1))
    with pytest.raises(release.ReleaseError):
        release.verify(output, arguments["public_key"], partial=True)


def test_signature_from_another_key_fails_closed_and_writes_nothing(case):
    arguments, _files, _key = case
    arguments["signer"] = ThrowawayKey().sign
    with pytest.raises(release.ReleaseError, match="different key"):
        release.assemble(**arguments)
    assert not arguments["output"].exists()
    assert not arguments["mapping"].exists()
    assert [p.name for p in arguments["output"].parent.iterdir() if p.name.startswith(".")] == []


def test_verify_rejects_a_forged_signature_under_the_right_key_id(case):
    arguments, _files, key = case
    release.assemble(**arguments)
    forged = ThrowawayKey()
    forged.key_id = key.key_id
    path = arguments["output"] / "SHA256SUMS.minisig"
    path.write_text(forged.sign(arguments["output"] / "SHA256SUMS"))
    with pytest.raises(release.ReleaseError, match="does not verify"):
        release.verify(arguments["output"], arguments["public_key"], partial=True)


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda d: (d / "BUILD-RECEIPT.json").write_text("{}"), "--build-receipt"),
        (lambda d: (d / "SHA256SUMS").write_text(""), "private evidence"),
        (lambda d: (d / "notes.txt").write_text("x"), "Not a 1.0.0 release file"),
        (lambda d: (d / "PlateFold_1.0.1_windows-x64.zip").write_text("x"), "Not a 1.0.0"),
        (lambda d: (d / "PlateFold_1.0.0_linux-x86_64.AppImage.sig").unlink(), "Missing updater"),
        (lambda d: (d / "PlateFold_1.0.0_windows-x64.zip.sig").write_text("x"), "Not a 1.0.0"),
        (lambda d: (d / "platefold-1.0.0.tar.gz").unlink(), "python-sdist"),
        (lambda d: (d / "PlateFold_1.0.0_macos-arm64.dmg").unlink(), "macos-disk-image"),
        (lambda d: (d / "nested").mkdir(), "ordinary files"),
    ],
)
def test_release_directory_must_hold_exactly_the_release(case, change, message):
    arguments, _files, _key = case
    change(arguments["artifacts"])
    with pytest.raises(release.ReleaseError, match=message):
        release.assemble(**arguments)
    assert not arguments["output"].exists()


def test_every_updater_signature_goes_through_the_policy_verifier(case):
    arguments, _files, _key = case
    checked = []

    def verifier(artifact, signature):
        checked.append(signature.name)
        receipt = accept_updater(artifact, signature)
        if artifact.name.endswith(".AppImage"):
            receipt["key_kind"] = "metadata"
        return receipt

    arguments["updater_verifier"] = verifier
    with pytest.raises(release.ReleaseError, match="AppImage"):
        release.assemble(**arguments)
    assert "PlateFold_1.0.0_linux-x86_64.AppImage.sig" in checked


@pytest.mark.parametrize(
    "edit,message",
    [
        ({"problems": ["internal host in README.md"]}, "records problems"),
        ({"source_commit": "0" * 40}, "different private commit"),
        ({"public_listing_sha256": "0" * 64}, "differ from the checked snapshot"),
    ],
)
def test_public_commit_must_be_the_checked_snapshot_of_the_private_commit(case, edit, message):
    arguments, _files, _key = case
    receipt = json.loads(arguments["snapshot_receipt"].read_text())
    receipt.update(edit)
    arguments["snapshot_receipt"].write_text(json.dumps(receipt))
    with pytest.raises(release.ReleaseError, match=message):
        release.assemble(**arguments)


@pytest.mark.parametrize(
    "edit,message",
    [
        ({"source_commit": "1" * 40}, "different commit"),
        ({"source_dirty": True}, "clean source tree"),
        ({"version": "1.0.0-rc.1"}, "different version"),
    ],
)
def test_build_receipts_must_name_the_private_commit_and_the_same_bytes(case, edit, message):
    arguments, _files, _key = case
    path = arguments["build_receipts"][0]
    receipt = json.loads(path.read_text())
    receipt.update(edit)
    path.write_text(json.dumps(receipt))
    with pytest.raises(release.ReleaseError, match=message):
        release.assemble(**arguments)


def refuse_before_signing(arguments, message):
    """Assemble must fail with message before the signer runs or anything is written."""
    signed = []

    def signer(path):
        signed.append(path)
        raise AssertionError("the signer must not run")

    arguments["signer"] = signer
    with pytest.raises(release.ReleaseError, match=message):
        release.assemble(**arguments)
    assert signed == []
    assert not arguments["output"].exists()
    assert not arguments["mapping"].exists()


@pytest.mark.parametrize(
    "field,value",
    [("sha256", "0" * 64), ("size", 1)],
)
def test_build_receipt_with_other_bytes_is_refused(case, field, value):
    arguments, _files, _key = case
    path = arguments["build_receipts"][0]
    edit_receipt(path, lambda receipt: receipt["artifacts"][0].update({field: value}))
    refuse_before_signing(arguments, "differs from the bytes")


def test_no_build_receipts_signs_nothing_and_names_every_uncovered_file(case):
    arguments, files, _key = case
    arguments["build_receipts"] = []
    signed = []
    arguments["signer"] = signed.append
    with pytest.raises(release.ReleaseError) as refused:
        release.assemble(**arguments)
    assert signed == []
    assert not arguments["output"].exists()
    assert not arguments["mapping"].exists()
    message = str(refused.value)
    assert "No build receipt binds" in message and "nothing was signed" in message
    # Updater signatures are checked against the update policy instead.
    for name in files:
        assert (name in message) is not name.endswith(".sig")


def test_a_receipt_with_the_right_commit_and_no_artifacts_is_refused(case):
    arguments, _files, _key = case
    path = arguments["build_receipts"][0]
    edit_receipt(path, lambda receipt: receipt.update(artifacts=[]))
    refuse_before_signing(arguments, "records no artifacts")
    edit_receipt(path, lambda receipt: receipt.pop("artifacts"))
    refuse_before_signing(arguments, "records no artifacts")


@pytest.mark.parametrize(
    "receipt_name,omitted",
    [
        ("windows-BUILD-RECEIPT.json", "PlateFold_1.0.0_x64-offline-setup.exe"),
        ("windows-BUILD-RECEIPT.json", "PlateFold_1.0.0_windows-x64.zip"),
        ("linux-BUILD-RECEIPT.json", "PlateFold_1.0.0_linux-x86_64.AppImage"),
        ("linux-BUILD-RECEIPT.json", "platefold_1.0.0_amd64.deb"),
        ("linux-BUILD-RECEIPT.json", "platefold.apparmor"),
        ("macos-MACOS-RELEASE-RECEIPT.json", "PlateFold_1.0.0_macos-arm64.dmg"),
        ("python-BUILD-RECEIPT.json", "platefold-1.0.0-py3-none-any.whl"),
    ],
)
def test_a_receipt_that_omits_one_release_file_is_refused(case, receipt_name, omitted):
    arguments, _files, _key = case
    (path,) = [p for p in arguments["build_receipts"] if p.name == receipt_name]
    edit_receipt(
        path,
        lambda receipt: receipt.update(
            artifacts=[item for item in receipt["artifacts"] if item["file"] != omitted]
        ),
    )
    refuse_before_signing(arguments, f"No build receipt binds these release files.*{omitted}")


def test_a_whole_platform_receipt_left_out_is_refused(case):
    arguments, _files, _key = case
    arguments["build_receipts"] = [
        path for path in arguments["build_receipts"] if not path.name.startswith("macos-")
    ]
    refuse_before_signing(arguments, "PlateFold_1.0.0_macos-arm64.app.tar.gz, ")


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda receipt: receipt.update(source_commit="1" * 40), "different commit"),
        (lambda receipt: receipt.pop("source_commit"), "different commit"),
        (lambda receipt: receipt.pop("source_dirty"), "clean source tree"),
        (lambda receipt: receipt.update(source_dirty=None), "clean source tree"),
        (lambda receipt: receipt["artifacts"][0].pop("size"), "malformed artifact entry"),
        (lambda receipt: receipt["artifacts"][0].update(size="1"), "malformed artifact entry"),
        (lambda receipt: receipt["artifacts"][0].update(sha256="x"), "malformed artifact entry"),
        (lambda receipt: receipt["artifacts"].append("file"), "malformed artifact entry"),
    ],
)
def test_a_receipt_must_bind_its_entries_to_the_clean_private_commit(case, change, message):
    arguments, _files, _key = case
    edit_receipt(arguments["build_receipts"][2], change)
    refuse_before_signing(arguments, message)


def test_a_receipt_that_covers_no_release_file_is_refused(case, tmp_path):
    arguments, _files, _key = case
    stray = tmp_path / "receipts" / "stray-BUILD-RECEIPT.json"
    stray.write_text(
        json.dumps(
            {
                "version": VERSION,
                "source_commit": arguments["private_commit"],
                "source_dirty": False,
                "artifacts": [file_entry("platefold-source.cdx.json", b"{}")],
            }
        )
    )
    arguments["build_receipts"].append(stray)
    refuse_before_signing(arguments, "records none of the release files")


def test_receipt_entries_for_build_by_products_are_ignored(case):
    arguments, _files, _key = case
    extra = file_entry("platefold-source.cdx.json", b"{}")
    edit_receipt(arguments["build_receipts"][3], lambda receipt: receipt["artifacts"].append(extra))
    record = release.assemble(**arguments)
    assert "platefold-source.cdx.json" not in record["build_receipts"][3]["covers"]


def test_a_receipt_that_is_not_an_object_is_refused(case):
    arguments, _files, _key = case
    arguments["build_receipts"][0].write_text("[]")
    refuse_before_signing(arguments, "is not a build receipt")


def test_existing_outputs_and_a_mapping_inside_public_files_are_refused(case):
    arguments, _files, _key = case
    arguments["mapping"] = arguments["output"] / "mapping.json"
    with pytest.raises(release.ReleaseError, match="outside the public files"):
        release.assemble(**arguments)
    arguments["mapping"] = arguments["artifacts"].parent / "private-mapping.json"
    arguments["output"].mkdir()
    with pytest.raises(release.ReleaseError, match="must not exist"):
        release.assemble(**arguments)


def test_tauri_signer_takes_the_key_from_its_own_variables_never_argv(monkeypatch, tmp_path):
    cli = tmp_path / "desktop/node_modules/@tauri-apps/cli/tauri.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    monkeypatch.setattr(release, "ROOT", tmp_path)
    monkeypatch.setenv(release.KEY_VARIABLE, "throwaway-provenance-key")
    monkeypatch.setenv(release.PASSWORD_VARIABLE, "throwaway-password")
    monkeypatch.setenv("TAURI_SIGNING_PRIVATE_KEY_PATH", "do-not-use")
    monkeypatch.setenv("PLATEFOLD_METADATA_SIGNING_PRIVATE_KEY", "do-not-use")
    seen = {}

    def run(command, **kwargs):
        seen["command"] = command
        seen["env"] = kwargs["env"]
        Path(f"{command[-1]}.sig").write_text(base64.b64encode(b"minisign document").decode())
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(release.subprocess, "run", run)
    source = tmp_path / "SHA256SUMS"
    source.write_text("sums\n")
    assert release.tauri_signer(source) == "minisign document"
    assert "throwaway-provenance-key" not in " ".join(seen["command"])
    assert seen["env"]["TAURI_SIGNING_PRIVATE_KEY"] == "throwaway-provenance-key"
    assert seen["env"]["TAURI_SIGNING_PRIVATE_KEY_PASSWORD"] == "throwaway-password"
    assert "TAURI_SIGNING_PRIVATE_KEY_PATH" not in seen["env"]
    assert "PLATEFOLD_METADATA_SIGNING_PRIVATE_KEY" not in seen["env"]


def test_missing_provenance_key_is_refused_before_any_work(case, monkeypatch):
    arguments, _files, _key = case
    monkeypatch.delenv(release.KEY_VARIABLE, raising=False)
    arguments["signer"] = release.tauri_signer
    with pytest.raises(release.ReleaseError, match=release.KEY_VARIABLE):
        release.assemble(**arguments)
    assert not arguments["output"].exists()


def test_committed_release_evidence_key_parses():
    key_id, key = release.minisign_public_key(release.PUBLIC_KEY.read_text())
    assert len(key_id) == 8 and len(key) == 32


def test_real_tauri_signature_verifies_with_a_throwaway_key(case, monkeypatch, tmp_path):
    desktop = release.ROOT / "desktop"
    npm = shutil.which("npm")
    if not (desktop / "node_modules/@tauri-apps/cli").is_dir() or npm is None:
        pytest.skip("requires the installed locked Tauri CLI")
    key_file = tmp_path / "throwaway.key"
    subprocess.run(
        [npm, "run", "tauri", "--", "signer", "generate", "--ci", "--write-keys", str(key_file)],
        cwd=desktop,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=True,
        timeout=60,
    )
    public_document = base64.b64decode(key_file.with_suffix(".key.pub").read_text()).decode()
    arguments, _files, _key = case
    arguments["public_key"].write_text(public_document)
    arguments["signer"] = release.tauri_signer
    monkeypatch.setenv(release.KEY_VARIABLE, key_file.read_text())
    monkeypatch.setenv(release.PASSWORD_VARIABLE, "")
    release.assemble(**arguments)
    result = release.verify(arguments["output"], arguments["public_key"], partial=True)
    assert result["trusted_comment"].endswith("file:SHA256SUMS")


def test_verify_command_reports_json_and_fails_with_a_message(case, capsys):
    arguments, _files, _key = case
    release.assemble(**arguments)
    output = str(arguments["output"])
    key = str(arguments["public_key"])
    assert release.main(["verify", output, "--public-key", key, "--partial"]) == 0
    assert json.loads(capsys.readouterr().out)["public_tag"] == "v1.0.0"
    assert release.main(["verify", output, "--public-key", key]) == 1
    assert "Release files missing" in capsys.readouterr().err
