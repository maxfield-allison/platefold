#!/usr/bin/env python3
"""Build, qualify and publish PlateFold container images without rebuilding.

  build --arch A        on a native A runner: build, push a candidate to the CI
                        registry, then run the container suite on that digest
  scan --arch A         vulnerability gate for one candidate digest
  assemble              join both qualified digests into one multi-arch index
  publish               copy that exact index to the public registries and sign
                        it there with the release Cosign key
  dockerhub-description update the Docker Hub overview from the committed text
  push [--arch A] REF   push a built image to the CI registry, retrying a failed
                        transfer; --arch pushes only that platform's manifest

A release runs on the private build commit, in a protected pipeline on main
started with PLATEFOLD_CONTAINER_RELEASE=vX.Y.Z. Without that variable the same
steps run as a rehearsal that can never publish. Only publish and
dockerhub-description write outside the CI registry. Publish needs
RELEASE_PUBLISH=yes and a public GitHub tag vX.Y.Z whose files are exactly the
public snapshot of the build commit, and refuses before any public write
unless tools/cosign_release.py can sign: packaging/cosign.pub must be the real
release key and the signing key in the environment must belong to it. Each
public copy is signed by digest and verified against packaging/cosign.pub
before :latest moves. Credentials come from the environment and registry auth
files; none is ever passed as a command argument.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    from tools import cosign_release
    from tools.assemble_release import resolve as resolve_commit
    from tools.assemble_release import tree_listing_digest
    from tools.container_artifacts import extract
except ImportError:  # Run as a script: tools/ itself is on sys.path.
    import cosign_release
    from assemble_release import resolve as resolve_commit
    from assemble_release import tree_listing_digest
    from container_artifacts import extract

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "container-release"
CONTAINER = ROOT / "packaging/container"
INPUTS = CONTAINER / "release-inputs.json"
IGNORE = CONTAINER / "trivyignore"
ARCHES = ("amd64", "arm64")
NATIVE = {"amd64": {"amd64", "x86_64"}, "arm64": {"arm64", "aarch64"}}
PROFILES = {"docker-profile": "seccomp.json"}
OCI_PROFILES = {"amd64": "seccomp-kubernetes.json", "arm64": "seccomp-kubernetes-arm64.json"}
# Receipt keys tools/test_container.py sets to "passed". Qualification and the
# publish-time check both require every one, so a receipt from a suite without
# one of them never publishes.
RUNTIME_CHECKS = (
    "offline_cli",
    "browser_interface_starts",
    "authenticated_web_export",
    "tagged_pdf_multilingual_text",
    "completed_result_after_restart",
    "cancel_render_and_reap_browser",
)
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
VERSION = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?\Z")
ATTESTATION = "attestation-manifest"
# Published images are signed with the release Cosign key (tools/cosign_release.py).
SIGNING_POLICY = "cosign"
# Registry answers that prove a reference is absent. Anything else, including
# "denied" or a network error, is not evidence that it is safe to create.
ABSENT = ("manifest unknown", "MANIFEST_UNKNOWN", "name unknown", "NAME_UNKNOWN")
DOCKERHUB_SHORT_LIMIT = 100
DOCKERHUB_FULL_LIMIT = 25000
# With Docker's containerd image store, a push stops at the first registry
# error instead of retrying the layer. A registry that is slow to answer one
# upload fails the whole job, so the push itself is retried a bounded number
# of times.
PUSH_ATTEMPTS = 3
PUSH_BACKOFF_SECONDS = 30


def take_signing_secrets():
    """Remove the Cosign key and password from this process's environment.

    Every subprocess started afterwards inherits neither, including those that
    imported helpers start without an explicit environment. Only the signer
    built from the returned values receives them.
    """
    return {name: os.environ.pop(name) for name in cosign_release.SECRETS if name in os.environ}


def run(*args, check=True, capture=True, env=None):
    """Run a command. Captured output is returned; streamed output goes to the log.

    Release signing variables are removed from the inherited environment
    before explicit env overrides are applied. Signing is handled by
    tools/cosign_release.py, which passes the key and password to cosign.
    """
    environment = {
        name: value for name, value in os.environ.items() if name not in cosign_release.SECRETS
    }
    result = subprocess.run(
        args,
        cwd=ROOT,
        text=True,
        capture_output=capture,
        timeout=5400,
        check=False,
        env={**environment, **(env or {})},
    )
    if check and result.returncode:
        if capture and result.stderr:
            sys.stderr.write(result.stderr[-2000:])
        raise RuntimeError(f"{args[0]} {args[1]} failed (exit {result.returncode})")
    return result


def push(ref, arch=None, sleep=time.sleep):
    """Push ref, retrying a failed transfer; arch limits it to one platform manifest.

    Blobs are content-addressed, so a retry uploads only what the registry does
    not hold yet, including a blob whose commit finished after the client gave
    up. Callers that need proof still compare the pushed digest with the build.
    """
    command = ["docker", "push"]
    if arch:
        command += ["--platform", "linux/" + arch]
    command.append(ref)
    for attempt in range(1, PUSH_ATTEMPTS + 1):
        result = run(*command, check=False, capture=False)
        if not result.returncode:
            return
        if attempt < PUSH_ATTEMPTS:
            delay = PUSH_BACKOFF_SECONDS * attempt
            print(
                f"docker push failed (exit {result.returncode}), attempt {attempt} of "
                f"{PUSH_ATTEMPTS}; retrying in {delay} s",
                file=sys.stderr,
                flush=True,
            )
            sleep(delay)
    raise RuntimeError(f"docker push {ref} failed after {PUSH_ATTEMPTS} attempts")


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def read_json(path):
    return json.loads(path.read_text())


def env(name):
    value = os.environ.get(name, "")
    if not value:
        raise ValueError(f"{name} is required")
    return value


def project_version():
    return tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]


def dockerfile_defaults(text):
    return dict(re.findall(r"^ARG ([A-Z_]+)=(\S+)$", text, re.M))


def load_inputs():
    """The committed release inputs, cross-checked against the Dockerfile defaults."""
    inputs = read_json(INPUTS)
    if inputs["platforms"] != ["linux/" + arch for arch in ARCHES]:
        raise ValueError("unsupported release platforms")
    if inputs["signing"] != SIGNING_POLICY:
        raise ValueError("unsupported signing policy")
    for key in ("python_image", "node_image", "sbom_generator"):
        if not DIGEST.fullmatch(inputs[key].split("@")[-1]):
            raise ValueError(f"{key} must be pinned by digest")
    if not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", inputs["debian_snapshot"]):
        raise ValueError("debian_snapshot must be a dated snapshot")
    for repository in inputs["public_repositories"]:
        if not re.fullmatch(r"(ghcr\.io|docker\.io)/[a-z0-9_.-]+/[a-z0-9_.-]+", repository):
            raise ValueError("public repositories must be ghcr.io or docker.io names")
    defaults = dockerfile_defaults((ROOT / "Dockerfile").read_text())
    for arg, key in (
        ("PYTHON_IMAGE", "python_image"),
        ("NODE_IMAGE", "node_image"),
        ("DEBIAN_SNAPSHOT", "debian_snapshot"),
    ):
        if defaults.get(arg) != inputs[key]:
            raise ValueError(f"Dockerfile {arg} differs from the committed release inputs")
    return inputs


def source_identity():
    """(commit, version) for a clean checkout of the pipeline's exact commit."""
    sha = env("CI_COMMIT_SHA")
    if (
        not re.fullmatch(r"[0-9a-f]{40}", sha)
        or run("git", "rev-parse", "HEAD").stdout.strip() != sha
    ):
        raise ValueError("build source does not match CI_COMMIT_SHA")
    if run("git", "status", "--porcelain", "--untracked-files=normal").stdout.strip():
        raise ValueError("release source contains modified or untracked files")
    release = os.environ.get("PLATEFOLD_CONTAINER_RELEASE", "")
    if not release:
        # A rehearsal qualifies the commit but can never publish.
        return sha, "sha-" + sha[:12]
    if os.environ.get("CI_COMMIT_TAG") or os.environ.get("CI_COMMIT_BRANCH") != "main":
        raise ValueError("a release runs only in a pipeline on main")
    if not VERSION.fullmatch(release):
        raise ValueError("PLATEFOLD_CONTAINER_RELEASE must be a semantic version tag")
    if release[1:] != project_version():
        raise ValueError("PLATEFOLD_CONTAINER_RELEASE differs from the project version")
    return sha, release


def snapshot_tools():
    """The private snapshot builder; only the build project has it."""
    try:
        from tools import public_snapshot
    except ImportError:
        try:
            import public_snapshot
        except ImportError as error:
            raise ValueError("publishing needs the build project's public_snapshot tool") from error
    return public_snapshot


def public_source(sha, version, url):
    """Require the public tag's files to be exactly the public snapshot of sha.

    The snapshot is the one tools/public_snapshot.py builds, and the comparison
    is the listing digest tools/assemble_release.py checks for the signed
    release: every public path with its content hash. A tag on private history
    carries private files, and an unrelated public commit carries other content;
    both differ.
    """
    snapshot = snapshot_tools()
    with tempfile.TemporaryDirectory(prefix="platefold-snapshot-") as scratch:
        report = snapshot.build(sha, Path(scratch) / "public", packages=False)
        if report.problems:
            raise ValueError("the public snapshot of the build commit has problems")
        # Compare archive exports on both sides: .bat files have CRLF there,
        # while their committed blobs use LF. Keep the committed listing for
        # signed source identity, and refuse hidden or non-regular tag files.
        run("git", "fetch", "--no-tags", "--depth=1", url, "refs/tags/" + version)
        public_commit, public_tree, _ = resolve_commit(ROOT, "FETCH_HEAD")
        public_report = snapshot.Report()
        names = snapshot.tracked_entries(public_commit, public_report)
        contents = snapshot.extract(public_commit, set(names), Path(scratch) / "tag")
        if (
            public_report.problems
            or set(contents) != set(names)
            or snapshot.listing_digest(contents) != report.checks["public_listing_sha256"]
        ):
            raise ValueError(f"public tag {version} is not the public snapshot of the build commit")
        exported = Path(scratch) / "public"
        for name in contents:
            if ((exported / name).stat().st_mode & 0o111) != (
                (Path(scratch) / "tag" / name).stat().st_mode & 0o111
            ):
                raise ValueError(f"public tag changed the executable mode: {name}")
        listing = tree_listing_digest(ROOT, public_commit)
    return {
        "version": version,
        "build_commit": sha,
        "public_commit": public_commit,
        "public_tree": public_tree,
        "public_listing_sha256": listing,
    }


def public_url(inputs):
    return f"https://github.com/{inputs['github_repository']}.git"


class Registry:
    """Registry reads and copies through skopeo, which copies an index unchanged."""

    def raw(self, ref):
        return run("skopeo", "inspect", "--raw", "docker://" + ref).stdout.encode()

    def resolve(self, ref):
        return digest(self.raw(ref))

    def state(self, ref):
        """The digest at ref, None when the registry confirms it is absent."""
        result = run("skopeo", "inspect", "--raw", "docker://" + ref, check=False)
        if result.returncode == 0:
            return digest(result.stdout.encode())
        if any(marker in result.stderr for marker in ABSENT):
            return None
        raise RuntimeError(f"cannot establish whether {ref} exists")

    def copy(self, source, destination):
        run(
            "skopeo",
            "copy",
            "--all",
            "--preserve-digests",
            "docker://" + source,
            "docker://" + destination,
        )
        if self.resolve(destination) != self.resolve(source):
            raise RuntimeError("registry copy changed the qualified digest")

    def immutable_copy(self, source, destination):
        current = self.state(destination)
        if current is None:
            self.copy(source, destination)
        elif current != self.resolve(source):
            raise RuntimeError(f"{destination} already holds different content")


def split_index(index):
    """(platform manifests, attestation manifests) of an image index."""
    platforms, attestations = [], []
    for descriptor in index.get("manifests", []):
        annotations = descriptor.get("annotations", {})
        if annotations.get("vnd.docker.reference.type") == ATTESTATION:
            attestations.append(descriptor)
        else:
            platforms.append(descriptor)
    return platforms, attestations


def check_index(index, expected):
    """Require exactly the expected {arch: manifest digest}, each with its attestation."""
    platforms, attestations = split_index(index)
    actual = {}
    for descriptor in platforms:
        platform = descriptor.get("platform", {})
        if platform.get("os") != "linux" or platform.get("architecture") in actual:
            raise ValueError("index holds an unexpected or duplicate platform")
        actual[platform.get("architecture")] = descriptor["digest"]
    if actual != expected:
        raise ValueError("index does not contain exactly the qualified architectures")
    referenced = [a.get("annotations", {}).get("vnd.docker.reference.digest") for a in attestations]
    if sorted(referenced) != sorted(expected.values()):
        raise ValueError("every image manifest needs exactly one SBOM/provenance attestation")


def private_hosts():
    hosts = set()
    for name in ("CI_SERVER_HOST", "CI_REGISTRY"):
        value = os.environ.get(name, "").split(":")[0]
        if value:
            hosts.add(value.lower())
    return hosts


def check_public_text(paths, hosts):
    """Attestations travel to public registries; they must not name the build host."""
    for path in paths:
        text = path.read_text().lower()
        if any(host in text for host in hosts):
            raise ValueError(f"{path.name} names a private CI host")


def unpassed_checks(receipt):
    return [check for check in RUNTIME_CHECKS if receipt.get(check) != "passed"]


def runtime_checks(ref, arch, directory):
    """Run the whole container suite against the exact candidate digest."""
    run("docker", "pull", "--platform", "linux/" + arch, ref)
    inspected = run(
        "docker", "image", "inspect", ref, "--format", "{{.Architecture}} {{.Id}}"
    ).stdout.split()
    if inspected[0] != arch:
        raise ValueError("runtime architecture mismatch")
    reported = run(
        "docker", "run", "--rm", "--pull=never", "--network", "none", ref, "--version"
    ).stdout.strip()
    if reported != "Plate|Fold " + project_version():
        raise ValueError("image did not report the project version")
    profiles = {**PROFILES, "oci-profile": OCI_PROFILES[arch]}
    results = {}
    for name, profile in profiles.items():
        output = directory / "runtime" / name
        run(
            "python3",
            "tools/test_container.py",
            "--seccomp",
            str(CONTAINER / profile),
            ref,
            str(output),
            capture=False,
        )
        receipt = read_json(output / "receipt.json")
        if failed := unpassed_checks(receipt):
            raise ValueError(f"{name} container suite did not pass: {', '.join(failed)}")
        if (
            receipt.get("uid") != 10001
            or receipt.get("read_only_root") is not True
            or receipt.get("privileged") is not False
            or receipt.get("cap_drop") != ["ALL"]
            or receipt.get("seccomp_sha256")
            != hashlib.sha256((CONTAINER / profile).read_bytes()).hexdigest()
        ):
            raise ValueError(f"{name} container ran without the required confinement")
        results[name] = {
            "status": "passed",
            "profile": profile,
            "apparmor_profile": receipt.get("apparmor_profile", ""),
            "receipt_sha256": digest((output / "receipt.json").read_bytes()),
        }
    return {
        "architecture": arch,
        "image_id": inspected[1],
        "version_output": reported,
        "profiles": results,
    }


def attestation_evidence(ref, index_digest, platform_digest, arch, directory):
    """Extract the SBOM and provenance bound to this candidate and check them for private hosts."""
    archive = directory / "image.tar"
    try:
        run("docker", "save", "--output", str(archive), ref)
        evidence = directory / "attestations"
        extract(archive, evidence, arch)
    finally:
        archive.unlink(missing_ok=True)
    receipt = read_json(evidence / "image.json")
    if (
        receipt["index_digest"] != index_digest
        or receipt[f"linux_{arch}_manifest"] != platform_digest
    ):
        raise ValueError("saved attestations belong to another image")
    files = {"sbom": evidence / "image.spdx.json", "provenance": evidence / "provenance.json"}
    check_public_text(files.values(), private_hosts())
    return {
        key: {"file": f"attestations/{path.name}", "sha256": digest(path.read_bytes())}
        for key, path in files.items()
    } | {"sbom_packages": receipt["sbom_packages"]}


def build(arch):
    sha, version = source_identity()
    inputs = load_inputs()
    native = run("docker", "info", "--format", "{{.Architecture}}").stdout.strip()
    if native not in NATIVE[arch]:
        raise ValueError("architecture qualification requires a native runner")
    directory = OUT / arch
    directory.mkdir(parents=True, exist_ok=True)
    # A release is checked against its public tag before anything is built, and
    # the image names the public commit, which is the one a user can look up.
    source = public_source(sha, version, public_url(inputs)) if VERSION.fullmatch(version) else None
    revision = source["public_commit"] if source else sha
    repository = env("CI_REGISTRY_IMAGE")
    tag = f"{repository}:candidate-{sha}-{env('CI_PIPELINE_ID')}-{arch}"
    # BuildKit writes the image name into the attestations' subject, and those
    # attestations travel to the public registries. Build under the public
    # name, which is never pushed from here, then push that exact index to the
    # CI registry under the candidate tag. BUILDX_GIT_INFO=false keeps the
    # private clone URL out of the provenance; mode=min records the build's
    # inputs without the full build graph.
    local = f"{inputs['public_repositories'][0]}:{version}-{arch}"
    run(
        "docker",
        "buildx",
        "build",
        "--pull",
        "--platform",
        "linux/" + arch,
        "--provenance=mode=min",
        "--attest",
        "type=sbom,generator=" + inputs["sbom_generator"],
        "--build-arg",
        "PYTHON_IMAGE=" + inputs["python_image"],
        "--build-arg",
        "NODE_IMAGE=" + inputs["node_image"],
        "--build-arg",
        "DEBIAN_SNAPSHOT=" + inputs["debian_snapshot"],
        "--build-arg",
        "SOURCE_REVISION=" + revision,
        "--build-arg",
        "SOURCE_VERSION=" + project_version(),
        "--build-arg",
        # Image history records build arguments; the private build commit
        # must not appear in a public image.
        "CACHE_BUST=" + revision,
        "--label",
        "org.opencontainers.image.source=" + inputs["source_repository"],
        "--metadata-file",
        str(directory / "build.json"),
        "--tag",
        local,
        ".",
        capture=False,
        env={"BUILDX_GIT_INFO": "false"},
    )
    index_digest = read_json(directory / "build.json")["containerimage.digest"]
    if not DIGEST.fullmatch(index_digest):
        raise ValueError("builder returned an invalid digest")
    run("docker", "tag", local, tag)
    # The registry needs an attestation's subject before the attestation, so
    # the platform manifest goes first, then the full index.
    push(tag, arch)
    push(tag)
    ref = repository + "@" + index_digest
    raw = Registry().raw(ref)
    if digest(raw) != index_digest:
        raise ValueError("registry content differs from the build result")
    platforms, _ = split_index(json.loads(raw))
    platform_digest = next(
        (p["digest"] for p in platforms if p.get("platform", {}).get("architecture") == arch), ""
    )
    check_index(json.loads(raw), {arch: platform_digest})
    runtime = runtime_checks(ref, arch, directory)
    attestations = attestation_evidence(ref, index_digest, platform_digest, arch, directory)
    write_json(
        directory / "image.json",
        {
            "schema": 1,
            "source": sha,
            "version": version,
            "arch": arch,
            "image": ref,
            "digest": index_digest,
            "platform_digest": platform_digest,
            "inputs": inputs,
            "runtime": runtime,
            "attestations": attestations,
            "public_source": source,
        },
    )


def scan(arch):
    directory = OUT / arch
    image = read_json(directory / "image.json")
    if image["source"] != env("CI_COMMIT_SHA") or image["arch"] != arch:
        raise ValueError("scan candidate belongs to another commit or architecture")
    report = directory / "scan.private.json"
    # The raw report names the private candidate; it stays a CI artifact.
    result = run(
        "trivy",
        "image",
        "--image-src",
        "remote",
        "--platform",
        "linux/" + arch,
        "--scanners",
        "vuln",
        "--severity",
        "HIGH,CRITICAL",
        "--ignore-unfixed",
        "--ignorefile",
        str(IGNORE),
        "--exit-code",
        "1",
        "--format",
        "json",
        "--output",
        str(report),
        image["image"],
        check=False,
    )
    if result.returncode:
        for line in findings(report):
            print(line)
        raise RuntimeError(
            "vulnerability gate failed; fix, or record a reviewed exception in trivyignore"
        )
    tool = run("trivy", "--version").stdout.strip().splitlines()[0]
    write_json(
        directory / "qualified.json",
        {
            **image,
            "scan": {
                "status": "passed",
                "tool": tool,
                "ignore_sha256": digest(IGNORE.read_bytes()),
                "report_sha256": digest(report.read_bytes()),
                "created_at": read_json(report).get("CreatedAt"),
            },
        },
    )


def findings(report):
    try:
        results = read_json(report).get("Results") or []
    except (OSError, ValueError):
        return ["no readable scan report"]
    return [
        f"{v['Severity']} {v['VulnerabilityID']} {v['PkgName']} {v.get('InstalledVersion', '')} -> {v.get('FixedVersion', '')}"
        for r in results
        for v in r.get("Vulnerabilities") or []
    ]


def check_identity(record, arch, source, version):
    if record["source"] != source or record["version"] != version or record["arch"] != arch:
        raise ValueError("qualification belongs to another candidate")
    if not DIGEST.fullmatch(record["digest"]) or not record["image"].endswith(
        "@" + record["digest"]
    ):
        raise ValueError("qualification is not digest-bound")
    if not DIGEST.fullmatch(record["platform_digest"]):
        raise ValueError("qualification has no platform manifest digest")


def check_runtime(record, arch, folder):
    runtime = record["runtime"]
    if runtime["architecture"] != arch or set(runtime["profiles"]) != {*PROFILES, "oci-profile"}:
        raise ValueError("runtime evidence belongs to another candidate")
    if runtime["profiles"]["oci-profile"]["profile"] != OCI_PROFILES[arch]:
        raise ValueError("container suite used another architecture's seccomp profile")
    for name, profile in runtime["profiles"].items():
        receipt = folder / "runtime" / name / "receipt.json"
        if profile["status"] != "passed":
            raise ValueError("container suite did not pass")
        if not receipt.is_file() or digest(receipt.read_bytes()) != profile["receipt_sha256"]:
            raise ValueError("runtime receipt does not match qualification")
        if failed := unpassed_checks(read_json(receipt)):
            raise ValueError(f"runtime receipt lacks passed checks: {', '.join(failed)}")


def check_scan(record, folder):
    scan = record["scan"]
    if scan["status"] != "passed" or scan["ignore_sha256"] != digest(IGNORE.read_bytes()):
        raise ValueError("scan did not pass under the committed exception policy")
    if scan["report_sha256"] != digest((folder / "scan.private.json").read_bytes()):
        raise ValueError("scan report does not match qualification")


def check_attestations(record, folder):
    for key in ("sbom", "provenance"):
        entry = record["attestations"][key]
        path = (folder / entry["file"]).resolve()
        if path.parent != (folder / "attestations").resolve() or not path.is_file():
            raise ValueError("attestation evidence is outside its architecture folder")
        if digest(path.read_bytes()) != entry["sha256"]:
            raise ValueError("attestation evidence does not match qualification")


def qualification(directory, source, version):
    """Every architecture's record, checked against the committed inputs and its files."""
    inputs = load_inputs()
    records = []
    for arch in ARCHES:
        folder = directory / arch
        record = read_json(folder / "qualified.json")
        check_identity(record, arch, source, version)
        check_runtime(record, arch, folder)
        if record["inputs"] != inputs:
            raise ValueError("build inputs differ from committed release inputs")
        check_scan(record, folder)
        check_attestations(record, folder)
        records.append(record)
    if records[0]["public_source"] != records[1]["public_source"]:
        raise ValueError("the architectures were checked against different public sources")
    if VERSION.fullmatch(version) and (records[0]["public_source"] or {}).get("version") != version:
        raise ValueError("a release candidate was not checked against its public tag")
    return records


def provenance(records, sha, version, index_digest):
    return {
        "schema": 1,
        "source": {
            "repository": records[0]["inputs"]["source_repository"],
            "public": records[0]["public_source"],
            "build_commit": sha,
        },
        "version": version,
        "index_digest": index_digest,
        "signing": records[0]["inputs"]["signing"],
        "provenance_kind": "declared build and qualification record; not a signed attestation",
        "architectures": [
            {
                "arch": r["arch"],
                "digest": r["platform_digest"],
                "inputs": r["inputs"],
                "runtime": r["runtime"],
                "scan": {
                    k: r["scan"][k] for k in ("status", "tool", "ignore_sha256", "created_at")
                },
                "attestations": r["attestations"],
            }
            for r in records
        ],
    }


def evidence_files():
    return [OUT / "provenance.json"] + [
        OUT / a / "attestations" / n for a in ARCHES for n in ("image.spdx.json", "provenance.json")
    ]


def checksums(paths):
    return "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(OUT).as_posix()}\n"
        for p in paths
    )


def assemble():
    sha, version = source_identity()
    records = qualification(OUT, sha, version)
    repository = env("CI_REGISTRY_IMAGE")
    tag = f"{repository}:candidate-{sha}-{env('CI_PIPELINE_ID')}"
    run("docker", "buildx", "imagetools", "create", "--tag", tag, *[r["image"] for r in records])
    raw = Registry().raw(tag)
    check_index(json.loads(raw), {r["arch"]: r["platform_digest"] for r in records})
    index_digest = digest(raw)
    write_json(
        OUT / "plan.json",
        {
            "schema": 1,
            "source": sha,
            "version": version,
            "image": repository + "@" + index_digest,
            "digest": index_digest,
        },
    )
    write_json(OUT / "provenance.json", provenance(records, sha, version, index_digest))
    (OUT / "SHA256SUMS").write_text(checksums(evidence_files()))


class GitHub:
    """Read-only, anonymous public tag lookup. Nothing here writes to GitHub.

    The repository is public by the time an image is published, so the lookup
    sends no credential and the release jobs hold no GitHub token at all.
    """

    def __init__(self, repository):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise ValueError("invalid GitHub repository")
        self.base = "https://api.github.com/repos/" + repository

    def request(self, path):
        url = self.base + path
        if urllib.parse.urlparse(url).hostname != "api.github.com":
            raise ValueError("unexpected GitHub API host")
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "platefold-release",
            },
        )

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
            return json.load(response)

    def tag_commit(self, version):
        obj = self.request("/git/ref/tags/" + urllib.parse.quote(version, safe=""))["object"]
        for _ in range(4):
            if obj["type"] == "commit":
                return obj["sha"]
            if obj["type"] != "tag":
                break
            obj = self.request("/git/tags/" + obj["sha"])["object"]
        raise ValueError("public release tag does not resolve to a commit")


def promote(
    plan,
    public_commit,
    repositories,
    github,
    registry,
    journal,
    *,
    sign,
    signed,
    first_publication=False,
):
    """Publish plan's index as :version everywhere, sign it, then move :latest with rollback.

    sign(reference) signs one image by digest and verifies that signature, raising
    if either fails; signed names the repositories it runs for. A repository can
    hold a signature only once it holds the image, so the signatures follow the
    version copies and come before any :latest moves. A failed signature stops
    here, with the journal at "versions-published" and :latest untouched.
    """
    if not signed or not set(signed) <= set(repositories):
        raise ValueError("every signed repository must be one that is published")
    # The caller established that public_commit's files are the snapshot of
    # the build commit; the tag must still name it at the moment of writing.
    if github.tag_commit(plan["version"]) != public_commit:
        raise ValueError("public tag does not name the verified public commit")
    if registry.resolve(plan["image"]) != plan["digest"]:
        raise ValueError("qualified index digest mismatch")
    stable = "-" not in plan["version"]
    previous = {r: registry.state(r + ":latest") for r in repositories} if stable else {}
    missing = [r for r, value in previous.items() if value is None]
    if missing and not first_publication:
        # Without a previous latest there is nothing to roll back to. Saying so
        # is an explicit operator decision for the first release only.
        raise ValueError(
            "latest is missing in "
            + ", ".join(missing)
            + "; set RELEASE_FIRST_PUBLICATION=yes for a first release"
        )
    state = {
        "source": plan["source"],
        "public_commit": public_commit,
        "version": plan["version"],
        "digest": plan["digest"],
        "previous_latest": previous,
    }
    write_json(journal, {**state, "phase": "prepared"})
    for repository in repositories:
        registry.immutable_copy(plan["image"], repository + ":" + plan["version"])
    write_json(journal, {**state, "phase": "versions-published"})
    for repository in signed:
        sign(repository + "@" + plan["digest"])
    state["signed"] = list(signed)
    write_json(journal, {**state, "phase": "versions-signed"})
    move_latest(plan["version"], previous, registry, journal, state)
    write_json(journal, {**state, "phase": "complete"})


def move_latest(version, previous, registry, journal, state):
    """Point :latest at version in every repository, or roll every one back."""
    changed = []
    try:
        for repository in previous:
            changed.append(repository)  # include an ambiguous failed write
            registry.copy(repository + ":" + version, repository + ":latest")
    except Exception:
        failures = []
        for repository in reversed(changed):
            if previous[repository] is None:
                failures.append(repository + " (no earlier latest)")
                continue
            try:
                registry.copy(repository + "@" + previous[repository], repository + ":latest")
            except Exception:
                failures.append(repository)
        write_json(
            journal, {**state, "phase": "latest-update-failed", "rollback_failures": failures}
        )
        raise


RELEASE_ENVIRONMENT = "platefold/publish-registry"


def release_credential(name):
    """A public registry credential, read only inside the environment it is scoped to."""
    if os.environ.get("CI_ENVIRONMENT_NAME") != RELEASE_ENVIRONMENT:
        raise ValueError(f"{name} is read only in the {RELEASE_ENVIRONMENT} environment")
    return env(name)


def publish(signing):
    if os.environ.get("RELEASE_PUBLISH") != "yes":
        raise ValueError("publication requires RELEASE_PUBLISH=yes")
    if os.environ.get("CI_ENVIRONMENT_NAME") != RELEASE_ENVIRONMENT:
        raise ValueError(f"publication runs only in the {RELEASE_ENVIRONMENT} environment")
    # Fail closed before reading anything else: no signing key, no publication.
    with tempfile.TemporaryDirectory(prefix="platefold-cosign-") as scratch:
        signer = cosign_release.ready_signer(Path(scratch), environ={**os.environ, **signing})
    plan = read_json(OUT / "plan.json")
    sha, version = source_identity()
    if plan["source"] != sha or plan["version"] != version or not VERSION.fullmatch(version):
        raise ValueError("publication requires the qualified release run")
    records = qualification(OUT, sha, version)
    if not DIGEST.fullmatch(plan["digest"]) or not plan["image"].endswith("@" + plan["digest"]):
        raise ValueError("release plan is not digest-bound")
    if read_json(OUT / "provenance.json") != provenance(records, sha, version, plan["digest"]):
        raise ValueError("provenance differs from qualification")
    if (OUT / "SHA256SUMS").read_text() != checksums(evidence_files()):
        raise ValueError("release checksums differ from qualification")
    inputs = load_inputs()
    source = public_source(sha, version, public_url(inputs))
    if source != records[0]["public_source"]:
        raise ValueError("the public tag changed after the candidates were qualified")
    write_json(OUT / "public-source.json", source)
    # The CI registry keeps the same stable aliases for private users.
    repositories = inputs["public_repositories"] + [env("CI_REGISTRY_IMAGE")]
    promote(
        plan,
        source["public_commit"],
        repositories,
        GitHub(inputs["github_repository"]),
        Registry(),
        OUT / "publication.json",
        # The CI registry's private aliases stay unsigned: a signature there
        # would put the private registry's name in the public transparency log.
        sign=signer.ensure_image_signed,
        signed=inputs["public_repositories"],
        first_publication=os.environ.get("RELEASE_FIRST_PUBLICATION") == "yes",
    )


def dockerhub_payload():
    full = (CONTAINER / "DOCKERHUB.md").read_text()
    short = (CONTAINER / "DOCKERHUB-SHORT.txt").read_text().strip()
    if not short or len(short) > DOCKERHUB_SHORT_LIMIT or "\n" in short:
        raise ValueError(
            f"short description must be one line of at most {DOCKERHUB_SHORT_LIMIT} characters"
        )
    if len(full) > DOCKERHUB_FULL_LIMIT:
        raise ValueError(f"overview exceeds {DOCKERHUB_FULL_LIMIT} characters")
    return {"full_description": full, "description": short}


def hub(url, body, token=None, method="POST"):
    if urllib.parse.urlparse(url).hostname != "hub.docker.com":
        raise ValueError("unexpected Docker Hub host")
    headers = {"Content-Type": "application/json", "User-Agent": "platefold-release"}
    if token:
        headers["Authorization"] = "JWT " + token
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), method=method, headers=headers
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
        return json.load(response)


def dockerhub_description():
    payload = dockerhub_payload()
    (repository,) = [r for r in load_inputs()["public_repositories"] if r.startswith("docker.io/")]
    namespace, name = repository.split("/")[1:]
    token = hub(
        "https://hub.docker.com/v2/users/login/",
        {
            "username": release_credential("DOCKERHUB_USERNAME"),
            "password": release_credential("DOCKERHUB_TOKEN"),
        },
    ).get("token")
    if not token:
        raise RuntimeError("Docker Hub login failed")
    hub(f"https://hub.docker.com/v2/repositories/{namespace}/{name}/", payload, token, "PATCH")
    print(
        f"Updated {namespace}/{name}: {len(payload['full_description'])} byte overview, "
        f"{len(payload['description'])} character summary"
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "command",
        choices=("build", "scan", "assemble", "publish", "dockerhub-description", "push"),
    )
    parser.add_argument("--arch", choices=ARCHES)
    parser.add_argument("ref", nargs="?", help="image reference, for push only")
    args = parser.parse_args()
    # Before any other work, so no subprocess of any command can inherit them.
    signing = take_signing_secrets()
    if (args.command == "push") != bool(args.ref):
        parser.error("a reference is required with push, and only with push")
    if args.command == "push":
        push(args.ref, args.arch)
    elif args.command in ("build", "scan"):
        if not args.arch:
            parser.error("--arch is required")
        {"build": build, "scan": scan}[args.command](args.arch)
    elif args.command == "publish":
        publish(signing)
    else:
        {"assemble": assemble, "dockerhub-description": dockerhub_description}[args.command]()


if __name__ == "__main__":
    main()
