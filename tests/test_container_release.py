"""Container release logic, exercised without credentials, a daemon or registry writes."""

import copy
import hashlib
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from tools import container_release as r

SOURCE = "a" * 40
NEW = "sha256:" + "b" * 64
OLD = "sha256:" + "c" * 64
AMD = "sha256:" + "d" * 64
ARM = "sha256:" + "e" * 64
PUBLIC = "9" * 40
REPOS = ["ghcr.io/example/platefold", "docker.io/example/platefold"]


class FakeRegistry:
    def __init__(self, plan, latest=True):
        self.values = {plan["image"]: NEW}
        if latest:
            for repo in REPOS:
                self.values[repo + ":latest"] = OLD
                self.values[repo + "@" + OLD] = OLD
        self.writes = []
        self.fail = None

    def resolve(self, ref):
        return self.values[ref]

    def state(self, ref):
        return self.values.get(ref)

    def copy(self, source, destination):
        self.values[destination] = self.resolve(source)
        self.writes.append(destination)
        # An ambiguous failure after the registry accepted the write.
        if self.fail == destination:
            self.fail = None
            raise RuntimeError("interrupted response")

    def immutable_copy(self, source, destination):
        if destination in self.values:
            if self.values[destination] != self.resolve(source):
                raise RuntimeError("conflicting immutable tag")
            return
        self.copy(source, destination)


class FakeSigner:
    """Records which references were signed, and when, against the registry's writes."""

    def __init__(self, registry, fail=False):
        self.registry = registry
        self.fail = fail
        self.signed = []

    def __call__(self, reference):
        if self.fail:
            raise RuntimeError("cosign verify failed")
        self.signed.append((reference, list(self.registry.writes)))


class FakeGitHub:
    def __init__(self):
        self.source = PUBLIC

    def tag_commit(self, version):
        return self.source


@pytest.fixture
def promotion(tmp_path):
    plan = {
        "source": SOURCE,
        "version": "v1.0.0",
        "image": "staging.invalid/platefold@" + NEW,
        "digest": NEW,
    }
    journal = tmp_path / "publication.json"
    registry = FakeRegistry(plan)
    state = {
        "plan": plan,
        "registry": registry,
        "github": FakeGitHub(),
        "journal": journal,
        "signer": FakeSigner(registry),
    }

    def promote(**kwargs):
        kwargs.setdefault("sign", state["signer"])
        kwargs.setdefault("signed", REPOS)
        r.promote(plan, PUBLIC, REPOS, state["github"], state["registry"], journal, **kwargs)

    state["promote"] = promote
    return state


def latest(registry):
    return [registry.values.get(repo + ":latest") for repo in REPOS]


def test_promotion_succeeds_and_a_retry_is_idempotent(promotion):
    promotion["promote"]()
    registry = promotion["registry"]
    assert latest(registry) == [NEW, NEW]
    assert all(registry.values[repo + ":v1.0.0"] == NEW for repo in REPOS)
    writes = list(registry.writes)
    promotion["promote"]()
    assert registry.writes == writes + [repo + ":latest" for repo in REPOS]
    assert r.read_json(promotion["journal"])["phase"] == "complete"


def test_each_public_copy_is_signed_after_every_version_copy_and_before_latest(promotion):
    promotion["promote"]()
    versions = [repo + ":v1.0.0" for repo in REPOS]
    assert promotion["signer"].signed == [(repo + "@" + NEW, versions) for repo in REPOS]
    assert promotion["registry"].writes == versions + [repo + ":latest" for repo in REPOS]


def test_a_failed_signature_stops_before_latest_moves(promotion):
    promotion["signer"].fail = True
    with pytest.raises(RuntimeError, match="cosign verify failed"):
        promotion["promote"]()
    assert latest(promotion["registry"]) == [OLD, OLD]
    assert r.read_json(promotion["journal"])["phase"] == "versions-published"


def test_only_published_repositories_are_signed_and_some_must_be(promotion):
    for signed in ([], ["ghcr.io/other/platefold"]):
        with pytest.raises(ValueError, match="signed repository"):
            promotion["promote"](signed=signed)
    assert promotion["registry"].writes == []


def test_signing_names_the_signed_repositories_in_the_journal(promotion):
    promotion["promote"](signed=REPOS[:1])
    assert [ref for ref, _ in promotion["signer"].signed] == [REPOS[0] + "@" + NEW]
    assert r.read_json(promotion["journal"])["signed"] == REPOS[:1]


def test_publication_refuses_before_reading_anything_while_the_key_is_a_placeholder(
    monkeypatch, tmp_path
):
    placeholder = tmp_path / "cosign.pub"
    placeholder.write_text("PLACEHOLDER: this is not a Cosign public key.\n")
    monkeypatch.setattr(r.cosign_release, "PUBLIC_KEY", placeholder)
    monkeypatch.setenv("RELEASE_PUBLISH", "yes")
    monkeypatch.setenv("CI_ENVIRONMENT_NAME", "platefold/publish-registry")
    monkeypatch.setenv("COSIGN_PRIVATE_KEY", "synthetic")
    monkeypatch.setenv("COSIGN_PASSWORD", "synthetic")
    monkeypatch.setattr(r, "read_json", lambda path: pytest.fail(f"read {path}"))
    with pytest.raises(r.cosign_release.SigningRefused, match="placeholder"):
        r.publish(r.take_signing_secrets())


def test_the_release_inputs_declare_cosign_signing():
    assert r.load_inputs()["signing"] == "cosign"


def test_a_public_tag_on_another_commit_writes_nothing(promotion):
    promotion["github"].source = "f" * 40
    with pytest.raises(ValueError):
        promotion["promote"]()
    assert promotion["registry"].writes == []


def test_a_changed_candidate_digest_writes_nothing(promotion):
    promotion["registry"].values[promotion["plan"]["image"]] = OLD
    with pytest.raises(ValueError):
        promotion["promote"]()
    assert promotion["registry"].writes == []


def test_an_existing_version_tag_is_never_overwritten(promotion):
    registry = promotion["registry"]
    registry.values[REPOS[0] + ":v1.0.0"] = OLD
    with pytest.raises(RuntimeError):
        promotion["promote"]()
    assert registry.values[REPOS[0] + ":v1.0.0"] == OLD
    assert latest(registry) == [OLD, OLD]


def test_a_partial_latest_update_rolls_every_registry_back(promotion):
    registry = promotion["registry"]
    registry.fail = REPOS[1] + ":latest"
    with pytest.raises(RuntimeError):
        promotion["promote"]()
    assert latest(registry) == [OLD, OLD]
    journal = r.read_json(promotion["journal"])
    assert journal["phase"] == "latest-update-failed" and journal["rollback_failures"] == []
    promotion["promote"]()
    assert latest(registry) == [NEW, NEW]


def test_a_prerelease_never_moves_latest(promotion):
    promotion["plan"]["version"] = "v1.0.0-rc.1"
    promotion["promote"]()
    assert latest(promotion["registry"]) == [OLD, OLD]
    assert promotion["registry"].values[REPOS[0] + ":v1.0.0-rc.1"] == NEW


def test_a_missing_latest_needs_the_first_publication_decision(tmp_path):
    plan = {
        "source": SOURCE,
        "version": "v1.0.0",
        "image": "staging.invalid/platefold@" + NEW,
        "digest": NEW,
    }
    registry = FakeRegistry(plan, latest=False)
    with pytest.raises(ValueError, match="RELEASE_FIRST_PUBLICATION"):
        r.promote(
            plan,
            PUBLIC,
            REPOS,
            FakeGitHub(),
            registry,
            tmp_path / "journal.json",
            sign=FakeSigner(registry),
            signed=REPOS,
        )
    assert registry.writes == []
    r.promote(
        plan,
        PUBLIC,
        REPOS,
        FakeGitHub(),
        registry,
        tmp_path / "journal.json",
        sign=FakeSigner(registry),
        signed=REPOS,
        first_publication=True,
    )
    assert latest(registry) == [NEW, NEW]


def test_a_failed_first_publication_says_it_cannot_roll_back(tmp_path):
    plan = {
        "source": SOURCE,
        "version": "v1.0.0",
        "image": "staging.invalid/platefold@" + NEW,
        "digest": NEW,
    }
    registry = FakeRegistry(plan, latest=False)
    registry.fail = REPOS[1] + ":latest"
    journal = tmp_path / "journal.json"
    with pytest.raises(RuntimeError):
        r.promote(
            plan,
            PUBLIC,
            REPOS,
            FakeGitHub(),
            registry,
            journal,
            sign=FakeSigner(registry),
            signed=REPOS,
            first_publication=True,
        )
    assert r.read_json(journal)["rollback_failures"] == [
        REPOS[1] + " (no earlier latest)",
        REPOS[0] + " (no earlier latest)",
    ]


PUBLIC_SOURCE = {
    "version": "v1.0.0",
    "build_commit": SOURCE,
    "public_commit": PUBLIC,
    "public_tree": "8" * 40,
    "public_listing_sha256": "7" * 64,
}


def write_candidate(directory, arch):
    folder = directory / arch
    (folder / "attestations").mkdir(parents=True)
    for name in ("image.spdx.json", "provenance.json"):
        (folder / "attestations" / name).write_bytes(b"{}\n")
    (folder / "scan.private.json").write_bytes(b"{}\n")
    profiles = {}
    for name, profile in (
        ("docker-profile", "seccomp.json"),
        ("oci-profile", r.OCI_PROFILES[arch]),
    ):
        receipt = folder / "runtime" / name / "receipt.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps(dict.fromkeys(r.RUNTIME_CHECKS, "passed")) + "\n")
        profiles[name] = {
            "status": "passed",
            "profile": profile,
            "apparmor_profile": "",
            "receipt_sha256": r.digest(receipt.read_bytes()),
        }
    record = {
        "source": SOURCE,
        "version": "v1.0.0",
        "arch": arch,
        "digest": NEW,
        "image": "staging.invalid/platefold@" + NEW,
        "platform_digest": AMD if arch == "amd64" else ARM,
        "inputs": r.load_inputs(),
        "runtime": {"architecture": arch, "profiles": profiles},
        "scan": {
            "status": "passed",
            "ignore_sha256": r.digest(r.IGNORE.read_bytes()),
            "report_sha256": r.digest(b"{}\n"),
        },
        "attestations": {
            "sbom": {"file": "attestations/image.spdx.json", "sha256": r.digest(b"{}\n")},
            "provenance": {"file": "attestations/provenance.json", "sha256": r.digest(b"{}\n")},
            "sbom_packages": 1,
        },
    }
    record["public_source"] = dict(PUBLIC_SOURCE)
    r.write_json(folder / "qualified.json", record)
    return record


@pytest.fixture
def candidates(tmp_path):
    return tmp_path, {arch: write_candidate(tmp_path, arch) for arch in r.ARCHES}


def test_a_complete_candidate_qualifies(candidates):
    directory, _ = candidates
    assert [record["arch"] for record in r.qualification(directory, SOURCE, "v1.0.0")] == [
        "amd64",
        "arm64",
    ]


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("source", "f" * 40),
        ("version", "v0.9.0"),
        ("arch", "amd64"),
        ("digest", "not-a-digest"),
        ("image", "staging.invalid/platefold:mutable"),
        ("platform_digest", ""),
        ("inputs", {"python_image": "python:latest"}),
    ],
)
def test_a_tampered_record_is_refused(candidates, key, value):
    directory, records = candidates
    record = copy.deepcopy(records["arm64"])
    record[key] = value
    r.write_json(directory / "arm64/qualified.json", record)
    with pytest.raises(ValueError):
        r.qualification(directory, SOURCE, "v1.0.0")


@pytest.mark.parametrize(
    "change",
    [
        "failed_suite",
        "wrong_arch_profile",
        "missing_profile",
        "failed_scan",
        "other_ignore_policy",
        "runtime_arch",
    ],
)
def test_incomplete_checks_are_refused(candidates, change):
    directory, records = candidates
    record = copy.deepcopy(records["arm64"])
    profiles = record["runtime"]["profiles"]
    if change == "failed_suite":
        profiles["docker-profile"]["status"] = "failed"
    elif change == "wrong_arch_profile":
        profiles["oci-profile"]["profile"] = r.OCI_PROFILES["amd64"]
    elif change == "missing_profile":
        del profiles["oci-profile"]
    elif change == "failed_scan":
        record["scan"]["status"] = "failed"
    elif change == "other_ignore_policy":
        record["scan"]["ignore_sha256"] = OLD
    else:
        record["runtime"]["architecture"] = "amd64"
    r.write_json(directory / "arm64/qualified.json", record)
    with pytest.raises(ValueError):
        r.qualification(directory, SOURCE, "v1.0.0")


@pytest.mark.parametrize(
    "value",
    [None, {**PUBLIC_SOURCE, "public_commit": "6" * 40}, {**PUBLIC_SOURCE, "version": "v0.9.0"}],
)
def test_a_release_needs_one_public_source_for_every_architecture(candidates, value):
    directory, records = candidates
    record = copy.deepcopy(records["arm64"])
    record["public_source"] = value
    r.write_json(directory / "arm64/qualified.json", record)
    if value is None:
        # Both architectures without a public source is a rehearsal, not a release.
        amd = copy.deepcopy(records["amd64"]) | {"public_source": None}
        r.write_json(directory / "amd64/qualified.json", amd)
    with pytest.raises(ValueError):
        r.qualification(directory, SOURCE, "v1.0.0")


@pytest.mark.parametrize(
    "path",
    [
        "scan.private.json",
        "attestations/image.spdx.json",
        "attestations/provenance.json",
        "runtime/oci-profile/receipt.json",
    ],
)
def test_changed_evidence_files_are_refused(candidates, path):
    directory, _ = candidates
    (directory / "arm64" / path).write_text("tampered")
    with pytest.raises(ValueError):
        r.qualification(directory, SOURCE, "v1.0.0")


@pytest.mark.parametrize("check", r.RUNTIME_CHECKS)
def test_a_receipt_without_every_passed_check_is_refused(candidates, check):
    # A receipt from a suite that predates a check, or skipped one, matches its
    # recorded digest but must still not publish.
    directory, records = candidates
    record = copy.deepcopy(records["arm64"])
    receipt = directory / "arm64/runtime/oci-profile/receipt.json"
    checks = dict.fromkeys(r.RUNTIME_CHECKS, "passed")
    del checks[check]
    receipt.write_text(json.dumps(checks) + "\n")
    record["runtime"]["profiles"]["oci-profile"]["receipt_sha256"] = r.digest(receipt.read_bytes())
    r.write_json(directory / "arm64/qualified.json", record)
    with pytest.raises(ValueError, match=check):
        r.qualification(directory, SOURCE, "v1.0.0")


def test_the_browser_interface_is_a_required_runtime_check():
    assert "browser_interface_starts" in r.RUNTIME_CHECKS


def test_attestation_paths_cannot_leave_their_architecture_folder(candidates):
    directory, records = candidates
    record = copy.deepcopy(records["arm64"])
    record["attestations"]["sbom"]["file"] = "../amd64/attestations/image.spdx.json"
    r.write_json(directory / "arm64/qualified.json", record)
    with pytest.raises(ValueError):
        r.qualification(directory, SOURCE, "v1.0.0")


def index(*platforms, attestations=None):
    manifests = [
        {"digest": d, "platform": {"os": "linux", "architecture": a}} for a, d in platforms
    ]
    for target in attestations if attestations is not None else [d for _, d in platforms]:
        manifests.append(
            {
                "digest": "sha256:" + "0" * 64,
                "platform": {"os": "unknown", "architecture": "unknown"},
                "annotations": {
                    "vnd.docker.reference.type": "attestation-manifest",
                    "vnd.docker.reference.digest": target,
                },
            }
        )
    return {"manifests": manifests}


def test_the_release_index_holds_exactly_the_qualified_images():
    r.check_index(index(("amd64", AMD), ("arm64", ARM)), {"amd64": AMD, "arm64": ARM})


@pytest.mark.parametrize(
    "value",
    [
        index(("amd64", AMD)),
        index(("amd64", AMD), ("arm64", OLD)),
        index(("amd64", AMD), ("arm64", ARM), ("arm64", ARM)),
        index(("amd64", AMD), ("arm64", ARM), ("s390x", OLD)),
        index(("amd64", AMD), ("arm64", ARM), attestations=[AMD]),
        index(("amd64", AMD), ("arm64", ARM), attestations=[AMD, OLD]),
    ],
)
def test_a_release_index_with_other_content_is_refused(value):
    with pytest.raises(ValueError):
        r.check_index(value, {"amd64": AMD, "arm64": ARM})


def completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_a_dirty_or_untracked_source_is_refused(monkeypatch):
    for status in (" M Dockerfile", "?? injected.py"):
        results = iter([completed(SOURCE + "\n"), completed(status + "\n")])
        monkeypatch.setenv("CI_COMMIT_SHA", SOURCE)
        monkeypatch.delenv("CI_COMMIT_TAG", raising=False)
        monkeypatch.delenv("PLATEFOLD_CONTAINER_RELEASE", raising=False)
        monkeypatch.setattr(r, "run", lambda *a, results=results, **k: next(results))
        with pytest.raises(ValueError):
            r.source_identity()


@pytest.mark.parametrize(
    ("release", "branch", "tag", "expected"),
    [
        ("", "main", "", "sha-" + SOURCE[:12]),
        ("", "", "v{version}", "sha-" + SOURCE[:12]),
        ("v{version}", "main", "", "v{version}"),
        ("v9.9.9", "main", "", ValueError),
        ("{version}", "main", "", ValueError),
        ("v{version}", "feature", "", ValueError),
        ("v{version}", "", "v{version}", ValueError),
    ],
)
def test_a_release_version_comes_only_from_a_run_on_main(
    monkeypatch, release, branch, tag, expected
):
    version = r.project_version()
    monkeypatch.setenv("CI_COMMIT_SHA", SOURCE)
    monkeypatch.setenv("PLATEFOLD_CONTAINER_RELEASE", release.format(version=version))
    monkeypatch.setenv("CI_COMMIT_BRANCH", branch)
    monkeypatch.setenv("CI_COMMIT_TAG", tag.format(version=version))
    results = iter([completed(SOURCE + "\n"), completed("")])
    monkeypatch.setattr(r, "run", lambda *a, **k: next(results))
    if expected is ValueError:
        with pytest.raises(ValueError):
            r.source_identity()
    else:
        assert r.source_identity() == (SOURCE, expected.format(version=version))


def test_committed_release_inputs_match_the_dockerfile():
    inputs = r.load_inputs()
    assert inputs["platforms"] == ["linux/amd64", "linux/arm64"]
    assert inputs["public_repositories"] == [
        "ghcr.io/maxfield-allison/platefold",
        "docker.io/probablyfinedev/platefold",
    ]


def test_a_dockerfile_that_drifts_from_the_release_inputs_is_refused(monkeypatch, tmp_path):
    text = (
        (r.ROOT / "Dockerfile")
        .read_text()
        .replace("ARG DEBIAN_SNAPSHOT=", "ARG DEBIAN_SNAPSHOT=1", 1)
    )
    (tmp_path / "Dockerfile").write_text(text)
    monkeypatch.setattr(r, "ROOT", tmp_path)
    with pytest.raises(ValueError, match="DEBIAN_SNAPSHOT"):
        r.load_inputs()


def test_attestations_naming_a_private_host_are_refused(tmp_path):
    path = tmp_path / "provenance.json"
    path.write_text('{"source": "https://ci.internal.example/group/project.git"}')
    with pytest.raises(ValueError):
        r.check_public_text([path], {"ci.internal.example"})
    r.check_public_text([path], {"registry.other.example"})


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (completed('{"manifests": []}'), r.digest(b'{"manifests": []}')),
        (completed(returncode=1, stderr="manifest unknown: manifest unknown"), None),
        (completed(returncode=1, stderr="NAME_UNKNOWN: repository name not known"), None),
    ],
)
def test_registry_state_distinguishes_present_and_absent(monkeypatch, result, expected):
    monkeypatch.setattr(r, "run", lambda *a, **k: result)
    assert r.Registry().state("registry.example/platefold:latest") == expected


@pytest.mark.parametrize(
    "stderr", ["denied: requested access to the resource is denied", "dial tcp: timeout"]
)
def test_an_unreadable_destination_is_not_treated_as_absent(monkeypatch, stderr):
    monkeypatch.setattr(r, "run", lambda *a, **k: completed(returncode=1, stderr=stderr))
    with pytest.raises(RuntimeError):
        r.Registry().state("registry.example/platefold:latest")


def suite_runner(checks, cap_drop=("ALL",)):
    """A fake docker/suite runner whose receipts report ``checks`` as passed."""
    version = r.project_version()

    def fake_run(*args, **kwargs):
        if args[:3] == ("docker", "image", "inspect"):
            return completed("arm64 " + NEW + "\n")
        if args[-1] == "--version":
            return completed(f"Plate|Fold {version}\n")
        if args[1] == "tools/test_container.py":
            output = Path(args[-1])
            output.mkdir(parents=True)
            receipt = dict.fromkeys(checks, "passed")
            profile = Path(args[3])
            receipt |= {
                "uid": 10001,
                "read_only_root": True,
                "privileged": False,
                "cap_drop": list(cap_drop),
                "seccomp_sha256": hashlib.sha256(profile.read_bytes()).hexdigest(),
            }
            (output / "receipt.json").write_text(json.dumps(receipt))
        return completed()

    return fake_run


def test_the_runtime_gate_rejects_an_unconfined_suite(monkeypatch, tmp_path):
    monkeypatch.setattr(r, "run", suite_runner(r.RUNTIME_CHECKS, cap_drop=()))
    with pytest.raises(ValueError, match="confinement"):
        r.runtime_checks("staging.invalid/platefold@" + NEW, "arm64", tmp_path)


def test_the_runtime_gate_passes_a_confined_suite_with_every_check(monkeypatch, tmp_path):
    monkeypatch.setattr(r, "run", suite_runner(r.RUNTIME_CHECKS))
    runtime = r.runtime_checks("staging.invalid/platefold@" + NEW, "arm64", tmp_path)
    assert {p["status"] for p in runtime["profiles"].values()} == {"passed"}


def test_the_runtime_gate_requires_the_browser_interface_check(monkeypatch, tmp_path):
    checks = [check for check in r.RUNTIME_CHECKS if check != "browser_interface_starts"]
    monkeypatch.setattr(r, "run", suite_runner(checks))
    with pytest.raises(ValueError, match="browser_interface_starts"):
        r.runtime_checks("staging.invalid/platefold@" + NEW, "arm64", tmp_path)


def test_the_runtime_gate_requires_the_project_version(monkeypatch, tmp_path):
    def fake_run(*args, **kwargs):
        if args[:3] == ("docker", "image", "inspect"):
            return completed("amd64 " + NEW + "\n")
        return completed("Plate|Fold 0.0.0\n")

    monkeypatch.setattr(r, "run", fake_run)
    with pytest.raises(ValueError, match="version"):
        r.runtime_checks("staging.invalid/platefold@" + NEW, "amd64", tmp_path)


def test_github_lookups_follow_annotated_tags_and_stay_on_the_api_host(monkeypatch):
    client = r.GitHub("example/platefold")
    replies = iter(
        [
            {"object": {"type": "tag", "sha": "1" * 40}},
            {"object": {"type": "commit", "sha": SOURCE}},
        ]
    )
    monkeypatch.setattr(client, "request", lambda path: next(replies))
    assert client.tag_commit("v1.0.0") == SOURCE
    with pytest.raises(ValueError):
        r.GitHub("example/platefold/../other")


def test_the_public_tag_lookup_is_anonymous(monkeypatch):
    seen = {}

    class Reply:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, *args):
            return b"{}"

    class Opener:
        def open(self, request, timeout):
            seen.update({key.lower(): value for key, value in request.header_items()})
            return Reply()

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    r.GitHub("example/platefold").request("/git/ref/tags/v1.0.0")
    assert "authorization" not in seen
    assert seen["accept"] == "application/vnd.github+json"


def test_a_missing_public_tag_is_an_error_not_a_pass(monkeypatch):
    client = r.GitHub("example/platefold")

    def missing(path):
        raise urllib.error.HTTPError("https://api.github.com", 404, "not found", {}, None)

    monkeypatch.setattr(client, "request", missing)
    with pytest.raises(urllib.error.HTTPError):
        client.tag_commit("v1.0.0")


def test_docker_hub_text_fits_its_limits():
    payload = r.dockerhub_payload()
    assert 0 < len(payload["description"]) <= r.DOCKERHUB_SHORT_LIMIT
    assert "\n" not in payload["description"]
    assert payload["full_description"].startswith("# Plate|Fold")
    for repository in r.load_inputs()["public_repositories"]:
        name = repository.split("/", 1)[1]
        assert name in payload["full_description"] or repository in payload["full_description"]


def test_docker_hub_requests_stay_on_the_hub_host():
    with pytest.raises(ValueError):
        r.hub("https://example.invalid/v2/users/login/", {})


@pytest.mark.parametrize(
    "environment", ["", "platefold/linux-signing", "release/other", "release/container"]
)
def test_registry_credentials_are_read_only_in_their_environment(monkeypatch, environment):
    monkeypatch.setenv("CI_ENVIRONMENT_NAME", environment)
    monkeypatch.setenv("DOCKERHUB_TOKEN", "fake-test-token")
    with pytest.raises(ValueError, match="platefold/publish-registry"):
        r.release_credential("DOCKERHUB_TOKEN")
    monkeypatch.setenv("RELEASE_PUBLISH", "yes")
    with pytest.raises(ValueError, match="platefold/publish-registry"):
        r.publish({})


def test_registry_credentials_are_read_in_their_environment(monkeypatch):
    monkeypatch.setenv("CI_ENVIRONMENT_NAME", "platefold/publish-registry")
    monkeypatch.setenv("DOCKERHUB_TOKEN", "fake-test-token")
    assert r.release_credential("DOCKERHUB_TOKEN") == "fake-test-token"


def test_a_failed_push_is_retried_a_bounded_number_of_times(monkeypatch):
    calls, delays = [], []
    outcomes = iter([completed(returncode=1), completed(returncode=1), completed()])

    def fake_run(*args, **kwargs):
        calls.append(args)
        assert kwargs == {"check": False, "capture": False}
        return next(outcomes)

    monkeypatch.setattr(r, "run", fake_run)
    ref = "registry.example/platefold:sha-1-amd64"
    r.push(ref, "amd64", sleep=delays.append)
    assert calls == [("docker", "push", "--platform", "linux/amd64", ref)] * r.PUSH_ATTEMPTS
    assert delays == [r.PUSH_BACKOFF_SECONDS, 2 * r.PUSH_BACKOFF_SECONDS]


def test_a_push_that_keeps_failing_fails_the_job(monkeypatch):
    calls, delays = [], []

    def fake_run(*args, **kwargs):
        calls.append(args)
        return completed(returncode=1)

    monkeypatch.setattr(r, "run", fake_run)
    with pytest.raises(RuntimeError, match="after 3 attempts"):
        r.push("registry.example/platefold:sha-1", sleep=delays.append)
    assert calls == [("docker", "push", "registry.example/platefold:sha-1")] * r.PUSH_ATTEMPTS
    assert len(delays) == r.PUSH_ATTEMPTS - 1


def test_a_successful_push_does_not_wait(monkeypatch):
    monkeypatch.setattr(r, "run", lambda *a, **k: completed())
    r.push("registry.example/platefold:sha-1", sleep=pytest.fail)


def test_only_cosign_receives_the_signing_key(monkeypatch):
    seen = {}

    def record(args, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setenv("COSIGN_PRIVATE_KEY", "synthetic-key")
    monkeypatch.setenv("COSIGN_PASSWORD", "synthetic-password")
    monkeypatch.setenv("REGISTRY_AUTH_FILE", "/tmp/auth.json")
    monkeypatch.setattr(r.subprocess, "run", record)

    r.run("skopeo", "inspect", "--raw", "docker://example.invalid/x", env={"EXTRA": "1"})

    assert "COSIGN_PRIVATE_KEY" not in seen and "COSIGN_PASSWORD" not in seen
    assert seen["REGISTRY_AUTH_FILE"] == "/tmp/auth.json" and seen["EXTRA"] == "1"


def test_the_command_line_takes_the_signing_key_before_any_work(monkeypatch):
    seen = {}

    def publish(signing):
        seen["signing"] = signing
        seen["inherited"] = {name for name in r.cosign_release.SECRETS if name in os.environ}

    monkeypatch.setenv("COSIGN_PRIVATE_KEY", "synthetic-key")
    monkeypatch.setenv("COSIGN_PASSWORD", "synthetic-password")
    monkeypatch.setattr(r, "publish", publish)
    monkeypatch.setattr(r.sys, "argv", ["container_release.py", "publish"])

    r.main()

    assert seen["signing"] == {
        "COSIGN_PRIVATE_KEY": "synthetic-key",
        "COSIGN_PASSWORD": "synthetic-password",
    }
    assert seen["inherited"] == set()
