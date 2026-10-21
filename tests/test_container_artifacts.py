"""OCI evidence must bind both attestations to the exact packaged image."""

import hashlib
import io
import json
import tarfile

import pytest

from tools.container_artifacts import extract


def archive(tmp_path, *, corrupt=False, wrong_subject=False, missing_sbom=False, arch="amd64"):
    files = {}

    def blob(value):
        data = json.dumps(value).encode()
        digest = hashlib.sha256(data).hexdigest()
        files["blobs/sha256/" + digest] = data
        return {"digest": "sha256:" + digest, "size": len(data)}

    config = blob({"config": {"Labels": {"org.opencontainers.image.revision": "candidate"}}})
    native = blob({"config": config, "layers": []})
    native["platform"] = {"architecture": arch, "os": "linux"}
    layers = []
    predicates = ["https://slsa.dev/provenance/v0.2"]
    if not missing_sbom:
        predicates.append("https://spdx.dev/Document")
    for predicate in predicates:
        layers.append(
            blob(
                {
                    "subject": [
                        {"digest": {"sha256": "0" * 64 if wrong_subject else native["digest"][7:]}}
                    ],
                    "predicateType": predicate,
                    "predicate": {"packages": [{"name": "platefold"}]},
                }
            )
        )
    attestation = blob({"layers": layers})
    attestation["annotations"] = {
        "vnd.docker.reference.type": "attestation-manifest",
        "vnd.docker.reference.digest": native["digest"],
    }
    index = blob({"manifests": [native, attestation]})
    files["index.json"] = json.dumps({"manifests": [index]}).encode()
    if corrupt:
        files["blobs/sha256/" + config["digest"][7:]] = b"{}"
    target = tmp_path / "image.tar"
    with tarfile.open(target, "w") as stream:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            stream.addfile(info, io.BytesIO(data))
    return target


def test_extracts_both_attestations_without_claiming_signatures(tmp_path):
    extract(archive(tmp_path), tmp_path / "evidence")
    receipt = json.loads((tmp_path / "evidence/image.json").read_text())
    assert receipt["revision"] == "candidate"
    assert receipt["sbom_packages"] == 1
    assert receipt["signed"] is False
    assert (tmp_path / "evidence/image.spdx.json").is_file()
    assert (tmp_path / "evidence/provenance.json").is_file()


def test_binds_evidence_to_the_requested_architecture(tmp_path):
    extract(archive(tmp_path, arch="arm64"), tmp_path / "evidence", "arm64")
    receipt = json.loads((tmp_path / "evidence/image.json").read_text())
    assert receipt["architecture"] == "arm64"
    assert receipt["linux_arm64_manifest"].startswith("sha256:")
    assert "linux_amd64_manifest" not in receipt


def test_rejects_an_archive_without_the_requested_architecture(tmp_path):
    with pytest.raises(ValueError):
        extract(archive(tmp_path), tmp_path / "evidence", "arm64")
    assert not (tmp_path / "evidence/image.json").exists()


@pytest.mark.parametrize("failure", ["corrupt", "wrong_subject", "missing_sbom"])
def test_rejects_unbound_or_incomplete_evidence(tmp_path, failure):
    with pytest.raises(ValueError):
        extract(archive(tmp_path, **{failure: True}), tmp_path / "evidence")
    assert not (tmp_path / "evidence/image.json").exists()


@pytest.mark.parametrize(
    ("arch", "native", "foreign"),
    [("amd64", "SCMP_ARCH_X86_64", "set_tls"), ("arm64", "SCMP_ARCH_AARCH64", "arch_prctl")],
)
def test_kubernetes_profile_does_not_carry_docker_privilege_conditions(arch, native, foreign):
    from pathlib import Path

    from tools.container_seccomp import specialize

    source = Path(__file__).resolve().parents[1] / "packaging/container/seccomp.json"
    profile = specialize(json.loads(source.read_text()), arch)
    assert profile["architectures"][0] == native
    assert "archMap" not in profile
    assert all(not {"includes", "excludes"} & rule.keys() for rule in profile["syscalls"])
    allowed = {name for rule in profile["syscalls"] for name in rule["names"]}
    assert {"chroot", "clone", "unshare", "setns"} <= allowed
    assert not {"mount", "bpf", "ptrace", "reboot", "init_module"} & allowed
    # Another architecture's syscall rules do not leak into this profile.
    assert foreign not in allowed


def test_kubernetes_profile_rejects_unconditional_privileged_rules():
    from tools.container_seccomp import specialize

    with pytest.raises(ValueError):
        specialize(
            {
                "defaultAction": "SCMP_ACT_ERRNO",
                "syscalls": [{"names": ["mount"], "action": "SCMP_ACT_ALLOW"}],
            }
        )


def test_kubernetes_profiles_exist_for_every_image_architecture():
    from tools.container_seccomp import ARCHITECTURES, TARGETS

    assert set(TARGETS) == set(ARCHITECTURES) == {"amd64", "arm64"}
