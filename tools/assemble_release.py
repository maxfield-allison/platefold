#!/usr/bin/env python3
"""Assemble and verify the release evidence for one set of final release files.

`assemble` takes a directory holding exactly the files a release publishes
(signed Windows installers and ZIP, macOS disk image and updater archive, Linux
AppImage and .deb, their updater signatures, the wheel and the source
distribution) and
writes three public files beside nothing else:

* release-manifest.json  every file's name, kind, size and SHA-256, bound to
                         the version, tag, public source commit and tree
* SHA256SUMS             every release file plus the manifest, in the format
                         `sha256sum --check` reads
* SHA256SUMS.minisig     a detached minisign signature over SHA256SUMS with the
                         release-evidence key. Because SHA256SUMS lists the
                         manifest, the one signature covers both

A later step, tools/cosign_release.py sign-checksums, adds a fourth public file,
SHA256SUMS.sigstore.json: a Cosign bundle over the same SHA256SUMS, made with
the release Cosign key. `verify` accepts it beside the evidence; cosign itself
checks it.

It also writes a private source-mapping record tying the commit the release
was built from to the public commit and tree it was published as. That record
names the private commit and never belongs with the public files.

    python tools/assemble_release.py assemble --version 1.0.0 \\
        --artifacts final/ --public-repo ../public --public-rev <commit> \\
        --private-commit <sha> --snapshot-receipt ../public.receipt.json \\
        --build-receipt BUILD-RECEIPT.json --output evidence/ --mapping mapping.json

    python tools/assemble_release.py verify evidence-and-downloads/

The signing key arrives only through the environment, as the encrypted
minisign key and its password in PLATEFOLD_PROVENANCE_SIGNING_PRIVATE_KEY and
PLATEFOLD_PROVENANCE_SIGNING_PRIVATE_KEY_PASSWORD; it is never an argument or
a file in the repository. The new signature must verify against the committed
public key (packaging/release-evidence.pub) before anything is written, so a
wrong or cross-purpose key fails closed. Every updater signature is checked
against the compiled update policy by the same verifier the feed builder uses.

Nothing here uploads, tags or publishes.
"""

from __future__ import annotations

import argparse
import base64
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

try:
    from tools.build_update_feed import verify as verify_updater_signature
except ModuleNotFoundError:  # Direct execution puts tools/, not the root, on sys.path.
    from build_update_feed import verify as verify_updater_signature

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_KEY = ROOT / "packaging" / "release-evidence.pub"
MANIFEST = "release-manifest.json"
SUMS = "SHA256SUMS"
SIGNATURE = "SHA256SUMS.minisig"
EVIDENCE_FILES = (MANIFEST, SUMS, SIGNATURE)
# Added after assembly by the Cosign signing step, so optional here.
COSIGN_BUNDLE = "SHA256SUMS.sigstore.json"
KEY_VARIABLE = "PLATEFOLD_PROVENANCE_SIGNING_PRIVATE_KEY"
PASSWORD_VARIABLE = "PLATEFOLD_PROVENANCE_SIGNING_PRIVATE_KEY_PASSWORD"
SHA40 = re.compile(r"[0-9a-f]{40}")
SHA64 = re.compile(r"[0-9a-f]{64}")
STABLE_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
MAX_UPDATER_SIGNATURE = 8192
MAX_EVIDENCE = 1024 * 1024


@dataclass(frozen=True)
class Rule:
    kind: str
    pattern: str  # formatted with the escaped version
    updater: bool = False  # ships with a <name>.sig updater signature


RULES = (
    Rule("windows-installer", r"PlateFold_{v}_x64-(?:online|offline)-setup\.exe", updater=True),
    Rule("windows-portable", r"PlateFold_{v}_windows-x64\.zip"),
    Rule("macos-disk-image", r"PlateFold_{v}_macos-(?:arm64|x86_64)\.dmg"),
    Rule("macos-updater-archive", r"PlateFold_{v}_macos-(?:arm64|x86_64)\.app\.tar\.gz", True),
    Rule("linux-appimage", r"PlateFold_{v}_linux-(?:x86_64|arm64)\.AppImage", updater=True),
    # Updated through the package manager, so it carries no updater signature;
    # the signed SHA256SUMS below is its provenance, as for the ZIP and DMG.
    Rule("linux-deb", r"platefold_{v}_(?:amd64|arm64)\.deb"),
    Rule("linux-support", r"platefold\.apparmor|LINUX-INSTALL\.md"),
    Rule("python-wheel", r"platefold-{v}-py3-none-any\.whl"),
    Rule("python-sdist", r"platefold-{v}\.tar\.gz"),
)
UPDATER_SIGNATURE_KIND = "updater-signature"
UPDATER_KINDS = frozenset(rule.kind for rule in RULES if rule.updater)
# Every file a release distributes must be bound by a build receipt to the
# private source commit, by size and SHA-256, before anything is signed. The one
# exception is the updater signature: the macOS signature is made after the
# notarized build that wrote its receipt, and every updater signature is checked
# instead against the compiled update policy and the receipted bytes of the
# artifact it signs (check_updater_signatures). A receipt that does list one
# must still record its exact bytes.
RECEIPT_EXEMPT_KINDS = frozenset({UPDATER_SIGNATURE_KIND})
# A release is incomplete without one file of each of these kinds.
REQUIRED_KINDS = (
    "windows-installer",
    "windows-portable",
    "macos-disk-image",
    "macos-updater-archive",
    "linux-appimage",
    "linux-deb",
    "python-wheel",
    "python-sdist",
)
# Per-platform build records: useful private evidence, never release files.
KNOWN_PRIVATE = re.compile(r".*RECEIPT.*\.json|.*-CHECK\.json|CI-PACKAGE\.env|SHA256SUMS")


class ReleaseError(ValueError):
    """A release input or output that must not be signed or trusted."""


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def canonical_json(value: object) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()


# --- minisign -----------------------------------------------------------------


def minisign_public_key(text: str) -> tuple[bytes, bytes]:
    """Return (key id, Ed25519 key) from a minisign public key or key document."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = [line for line in lines if not line.startswith("untrusted comment:")]
    if len(candidates) != 1:
        raise ReleaseError("A minisign public key has exactly one key line")
    try:
        raw = base64.b64decode(candidates[0], validate=True)
    except ValueError:
        raise ReleaseError("The minisign public key is not base64") from None
    if len(raw) != 42 or raw[:2] != b"Ed":
        raise ReleaseError("Not an Ed25519 minisign public key")
    return raw[2:10], raw[10:]


def verify_minisign(data: bytes, document: str, public_key: str) -> str:
    """Verify a prehashed minisign signature document; return its trusted comment."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    key_id, key = minisign_public_key(public_key)
    lines = document.rstrip("\n").split("\n")
    if (
        len(lines) != 4
        or not lines[0].startswith("untrusted comment:")
        or not lines[2].startswith("trusted comment: ")
    ):
        raise ReleaseError("Not a minisign signature document")
    try:
        signature = base64.b64decode(lines[1], validate=True)
        global_signature = base64.b64decode(lines[3], validate=True)
    except ValueError:
        raise ReleaseError("The minisign signature is not base64") from None
    if len(signature) != 74 or len(global_signature) != 64:
        raise ReleaseError("The minisign signature has the wrong length")
    if signature[:2] != b"ED":
        raise ReleaseError("Only prehashed (ED) minisign signatures are accepted")
    if signature[2:10] != key_id:
        raise ReleaseError("The signature was made by a different key")
    trusted = lines[2][len("trusted comment: ") :]
    verifier = Ed25519PublicKey.from_public_bytes(key)
    try:
        verifier.verify(signature[10:], hashlib.blake2b(data, digest_size=64).digest())
        verifier.verify(global_signature, signature[10:] + trusted.encode())
    except InvalidSignature:
        raise ReleaseError("The release-evidence signature does not verify") from None
    return trusted


def tauri_signer(path: Path) -> str:
    """Sign one file with the release-evidence key through the locked Tauri signer.

    Mirrors the feed builder's metadata signing: the key and password move from
    their purpose-specific variables into the signer's environment, never argv.
    """
    key = os.environ.get(KEY_VARIABLE)
    password = os.environ.get(PASSWORD_VARIABLE)
    if not key or password is None:
        raise ReleaseError(f"{KEY_VARIABLE} and {PASSWORD_VARIABLE} must be set")
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith(("TAURI_SIGNING_", "PLATEFOLD_METADATA_SIGNING_"))
    }
    env["TAURI_SIGNING_PRIVATE_KEY"] = key
    env["TAURI_SIGNING_PRIVATE_KEY_PASSWORD"] = password
    cli = ROOT / "desktop/node_modules/@tauri-apps/cli/tauri.js"
    if not cli.is_file():
        raise ReleaseError("The locked Tauri CLI is not installed (npm ci in desktop/)")
    with tempfile.TemporaryDirectory(prefix="platefold-evidence-") as directory:
        copy = Path(directory) / path.name
        shutil.copyfile(path, copy)
        result = subprocess.run(
            ["node", str(cli), "signer", "sign", str(copy)],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=120,
            check=False,
        )
        if result.returncode:
            raise ReleaseError(
                "Release-evidence signing failed; check the provenance key variables"
            )
        encoded = Path(f"{copy}.sig").read_text(encoding="ascii").strip()
    return base64.b64decode(encoded, validate=True).decode("utf-8")


# --- release files ----------------------------------------------------------------


def kind_of(name: str, compiled: list[tuple[Rule, re.Pattern[str]]]) -> str | None:
    for rule, regex in compiled:
        if regex.fullmatch(name):
            return rule.kind
    if name.endswith(".sig") and kind_of(name.removesuffix(".sig"), compiled) in UPDATER_KINDS:
        return UPDATER_SIGNATURE_KIND
    return None


def classify(directory: Path, version: str) -> dict[str, str]:
    """Map every file in the directory to its kind, refusing anything unexpected."""
    if not STABLE_VERSION.fullmatch(version):
        raise ReleaseError("A stable X.Y.Z version is required")
    escaped = re.escape(version)
    compiled = [(rule, re.compile(rule.pattern.format(v=escaped))) for rule in RULES]
    kinds: dict[str, str] = {}
    unexpected: list[str] = []
    for path in sorted(directory.iterdir()):
        if path.is_symlink() or not path.is_file():
            raise ReleaseError(f"Only ordinary files belong in the release directory: {path.name}")
        kind = kind_of(path.name, compiled)
        if kind:
            kinds[path.name] = kind
        else:
            unexpected.append(path.name)
    private = [name for name in unexpected if KNOWN_PRIVATE.fullmatch(name)]
    if private:
        raise ReleaseError(
            "Per-platform build records are private evidence, not release files; "
            f"pass receipts with --build-receipt instead: {', '.join(private)}"
        )
    if unexpected:
        raise ReleaseError(f"Not a {version} release file: {', '.join(unexpected)}")
    check_complete(kinds)
    return kinds


def check_complete(kinds: dict[str, str]) -> None:
    for name, kind in kinds.items():
        if kind in UPDATER_KINDS and f"{name}.sig" not in kinds:
            raise ReleaseError(f"Missing updater signature: {name}.sig")
    missing = [kind for kind in REQUIRED_KINDS if kind not in kinds.values()]
    if missing:
        raise ReleaseError(f"The release is missing: {', '.join(missing)}")


def check_updater_signatures(directory: Path, kinds: dict[str, str], verifier: Callable) -> None:
    for name, kind in kinds.items():
        if kind != UPDATER_SIGNATURE_KIND:
            continue
        signature = directory / name
        artifact = directory / name.removesuffix(".sig")
        if not 0 < signature.stat().st_size <= MAX_UPDATER_SIGNATURE:
            raise ReleaseError(f"Updater signature has an invalid size: {name}")
        try:
            proof = verifier(artifact, signature)
        except ValueError as error:
            raise ReleaseError(f"{name}: {error}") from None
        if (
            proof.get("verified") is not True
            or proof.get("key_kind") != "artifact"
            or proof.get("sha256") != digest(artifact)
            or proof.get("size") != artifact.stat().st_size
        ):
            raise ReleaseError(f"Updater signature receipt does not match {artifact.name}")


def build_manifest(directory: Path, kinds: dict[str, str], version: str, commit: str, tree: str):
    return {
        "schema_version": 1,
        "version": version,
        "public_tag": f"v{version}",
        "public_source_commit": commit,
        "public_source_tree": tree,
        "artifacts": [
            {
                "name": name,
                "kind": kinds[name],
                "size": (directory / name).stat().st_size,
                "sha256": digest(directory / name),
            }
            for name in sorted(kinds)
        ],
    }


def sums_text(entries: dict[str, str]) -> str:
    return "".join(f"{entries[name]}  {name}\n" for name in sorted(entries))


def parse_sums(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in text.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9_.+-]*)", line)
        if not match or match.group(2) in entries:
            raise ReleaseError(f"Malformed or duplicate SHA256SUMS line: {line!r}")
        entries[match.group(2)] = match.group(1)
    return entries


def check_manifest_schema(manifest: object) -> dict:
    required = {
        "schema_version",
        "version",
        "public_tag",
        "public_source_commit",
        "public_source_tree",
        "artifacts",
    }
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ReleaseError("Unexpected release manifest fields")
    version = manifest["version"]
    if (
        manifest["schema_version"] != 1
        or not isinstance(version, str)
        or not STABLE_VERSION.fullmatch(version)
        or manifest["public_tag"] != f"v{version}"
        or not SHA40.fullmatch(str(manifest["public_source_commit"]))
        or not SHA40.fullmatch(str(manifest["public_source_tree"]))
        or not isinstance(manifest["artifacts"], list)
    ):
        raise ReleaseError("Invalid release manifest header")
    names = set()
    for item in manifest["artifacts"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"name", "kind", "size", "sha256"}
            or type(item["size"]) is not int
            or not SHA64.fullmatch(str(item["sha256"]))
            or item["name"] in names
            or item["name"] in (*EVIDENCE_FILES, COSIGN_BUNDLE)
        ):
            raise ReleaseError(f"Invalid release manifest entry: {item!r}")
        names.add(item["name"])
    return manifest


# --- source mapping ---------------------------------------------------------------


def git(repo: Path, *args: str) -> bytes:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=False)
    if result.returncode:
        raise ReleaseError(f"git {' '.join(args[:2])} failed in {repo}")
    return result.stdout


def resolve(repo: Path, rev: str) -> tuple[str, str, list[str]]:
    commit = git(repo, "rev-parse", "--verify", f"{rev}^{{commit}}").decode().strip()
    tree = git(repo, "rev-parse", f"{commit}^{{tree}}").decode().strip()
    parents = git(repo, "rev-list", "--parents", "-n", "1", commit).decode().split()[1:]
    return commit, tree, parents


def tree_listing_digest(repo: Path, commit: str) -> str:
    """The snapshot builder's listing digest, computed from a committed tree."""
    entries = []
    for record in git(repo, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        meta, name = record.split(b"\t", 1)
        mode, kind, obj = meta.split()
        if kind != b"blob" or mode == b"120000":
            raise ReleaseError(f"Public tree holds a non-file entry: {name.decode()}")
        entries.append((name.decode(), obj.decode()))
    batch = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input="".join(f"{obj}\n" for _, obj in entries).encode(),
        capture_output=True,
        check=True,
    ).stdout
    hashes = {}
    offset = 0
    for name, _obj in entries:
        header_end = batch.index(b"\n", offset)
        size = int(batch[offset:header_end].split()[2])
        content = batch[header_end + 1 : header_end + 1 + size]
        hashes[name] = hashlib.sha256(content).hexdigest()
        offset = header_end + 1 + size + 1
    listing = "".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes))
    return hashlib.sha256(listing.encode("utf-8")).hexdigest()


def check_snapshot(receipt_path: Path, private_commit: str, listing: str) -> dict:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if receipt.get("problems") != []:
        raise ReleaseError(
            "The snapshot receipt records problems; that snapshot is not publishable"
        )
    if receipt.get("source_commit") != private_commit:
        raise ReleaseError("The snapshot was taken from a different private commit")
    if receipt.get("public_listing_sha256") != listing:
        raise ReleaseError("The public commit's files differ from the checked snapshot")
    return receipt


def receipt_entries(path: Path, receipt: object) -> list[dict]:
    if not isinstance(receipt, dict):
        raise ReleaseError(f"{path.name} is not a build receipt")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not artifacts:
        raise ReleaseError(f"{path.name} records no artifacts")
    for item in artifacts:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("file"), str)
            or type(item.get("size")) is not int
            or not SHA64.fullmatch(str(item.get("sha256")))
        ):
            raise ReleaseError(f"{path.name} has a malformed artifact entry: {item!r}")
    return artifacts


def check_build_receipts(paths: list[Path], private_commit: str, manifest: dict) -> list[dict]:
    """Bind every distributed release file to the private commit through a build receipt.

    Each receipt must name the private commit, a clean tree and the release
    version, and each entry naming a release file must record its exact size and
    SHA-256. Every release file outside RECEIPT_EXEMPT_KINDS must be covered, or
    nothing is signed.
    """
    listed = {item["name"]: item for item in manifest["artifacts"]}
    records = []
    for path in paths:
        receipt = json.loads(path.read_text(encoding="utf-8"))
        entries = receipt_entries(path, receipt)
        if receipt.get("source_commit") != private_commit:
            raise ReleaseError(f"{path.name} was built from a different commit")
        if receipt.get("source_dirty") is not False:
            raise ReleaseError(f"{path.name} does not record a clean source tree")
        if receipt.get("version") not in (None, manifest["version"]):
            raise ReleaseError(f"{path.name} records a different version")
        covers = []
        for item in entries:
            release = listed.get(item["file"])
            if release is None:
                # Build by-products that are not release files (a CycloneDX
                # document, for example) are not signed and need no binding.
                continue
            if release["sha256"] != item["sha256"] or release["size"] != item["size"]:
                raise ReleaseError(f"{item['file']} differs from the bytes {path.name} recorded")
            covers.append(item["file"])
        if not covers:
            raise ReleaseError(f"{path.name} records none of the release files")
        records.append(
            {
                "file": path.name,
                "sha256": digest(path),
                "source_commit": private_commit,
                "covers": sorted(set(covers)),
            }
        )
    covered = {name for record in records for name in record["covers"]}
    uncovered = sorted(
        name
        for name, item in listed.items()
        if item["kind"] not in RECEIPT_EXEMPT_KINDS and name not in covered
    )
    if uncovered:
        raise ReleaseError(
            "No build receipt binds these release files to the private commit; "
            f"nothing was signed: {', '.join(uncovered)}"
        )
    return records


# --- commands ---------------------------------------------------------------------


def assemble(
    *,
    version: str,
    artifacts: Path,
    public_repo: Path,
    public_rev: str,
    private_repo: Path,
    private_commit: str,
    snapshot_receipt: Path,
    build_receipts: list[Path],
    output: Path,
    mapping: Path,
    public_key: Path = PUBLIC_KEY,
    signer: Callable[[Path], str] = tauri_signer,
    updater_verifier: Callable = verify_updater_signature,
    now: datetime.datetime | None = None,
) -> dict:
    if not SHA40.fullmatch(private_commit):
        raise ReleaseError("--private-commit must be a full 40-character commit")
    if output.exists() or mapping.exists():
        raise ReleaseError("The output directory and mapping file must not exist")
    if mapping.resolve().is_relative_to(output.resolve()) or mapping.resolve().is_relative_to(
        artifacts.resolve()
    ):
        raise ReleaseError("The private mapping record must live outside the public files")
    if not os.environ.get(KEY_VARIABLE) and signer is tauri_signer:
        raise ReleaseError(f"{KEY_VARIABLE} and {PASSWORD_VARIABLE} must be set")
    key_text = public_key.read_text(encoding="utf-8")
    minisign_public_key(key_text)

    kinds = classify(artifacts, version)
    check_updater_signatures(artifacts, kinds, updater_verifier)
    private_tree = resolve(private_repo, private_commit)[1]
    public_commit, public_tree, public_parents = resolve(public_repo, public_rev)
    listing = tree_listing_digest(public_repo, public_commit)
    snapshot = check_snapshot(snapshot_receipt, private_commit, listing)

    manifest = build_manifest(artifacts, kinds, version, public_commit, public_tree)
    receipts = check_build_receipts(build_receipts, private_commit, manifest)
    manifest_bytes = canonical_json(manifest)
    entries = {item["name"]: item["sha256"] for item in manifest["artifacts"]}
    entries[MANIFEST] = hashlib.sha256(manifest_bytes).hexdigest()
    sums = sums_text(entries).encode()

    # Stage beside the destination so a failure leaves no partial evidence.
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{output.name}-", dir=output.parent))
    try:
        (staging / MANIFEST).write_bytes(manifest_bytes)
        (staging / SUMS).write_bytes(sums)
        document = signer(staging / SUMS)
        trusted = verify_minisign(sums, document, key_text)
        (staging / SIGNATURE).write_text(document.rstrip("\n") + "\n", encoding="utf-8")
        staging.rename(output)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # Only RECEIPT_EXEMPT_KINDS can remain here: check_build_receipts refused
    # anything else before signing.
    covered = {name for record in receipts for name in record["covers"]}
    record = {
        "schema_version": 1,
        "created_at": (now or datetime.datetime.now(datetime.UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "version": version,
        "public_tag": manifest["public_tag"],
        "private_source": {"commit": private_commit, "tree": private_tree},
        "public_source": {
            "commit": public_commit,
            "tree": public_tree,
            "parents": public_parents,
            "listing_sha256": listing,
        },
        "snapshot_receipt": {
            "file": snapshot_receipt.name,
            "sha256": digest(snapshot_receipt),
            "public_files": snapshot.get("public_files"),
            "excluded_files": snapshot.get("excluded_files"),
        },
        "release_evidence": {
            name: {"sha256": digest(output / name), "size": (output / name).stat().st_size}
            for name in EVIDENCE_FILES
        },
        "signature_trusted_comment": trusted,
        "release_evidence_public_key": minisign_public_key_line(key_text),
        "build_receipts": receipts,
        "artifacts_without_build_receipt": sorted(set(entries) - covered - {MANIFEST}),
    }
    with mapping.open("xb") as handle:
        handle.write(canonical_json(record))
    return record


def minisign_public_key_line(text: str) -> str:
    return next(
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.startswith("untrusted comment:")
    )


def check_file(path: Path, item: dict, summed: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise ReleaseError(f"Not an ordinary file: {path.name}")
    if path.stat().st_size != item["size"] or digest(path) != item["sha256"]:
        raise ReleaseError(f"{path.name} differs from the signed release")
    if summed != item["sha256"]:
        raise ReleaseError(f"{path.name} has different digests in SHA256SUMS and the manifest")


def verify(directory: Path, public_key: Path = PUBLIC_KEY, partial: bool = False) -> dict:
    """Check the evidence signature, the manifest and every release file present."""
    for name in EVIDENCE_FILES:
        path = directory / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_EVIDENCE:
            raise ReleaseError(f"Missing or oversized evidence file: {name}")
    bundle = directory / COSIGN_BUNDLE
    cosign_bundle = bundle.exists() or bundle.is_symlink()
    if cosign_bundle and (
        bundle.is_symlink() or not bundle.is_file() or bundle.stat().st_size > MAX_EVIDENCE
    ):
        raise ReleaseError(f"Not an ordinary evidence file: {COSIGN_BUNDLE}")
    sums = (directory / SUMS).read_bytes()
    trusted = verify_minisign(
        sums,
        (directory / SIGNATURE).read_text(encoding="utf-8"),
        public_key.read_text(encoding="utf-8"),
    )
    entries = parse_sums(sums.decode("utf-8"))
    if entries.get(MANIFEST) != digest(directory / MANIFEST):
        raise ReleaseError("release-manifest.json differs from the signed SHA256SUMS")
    manifest = check_manifest_schema(json.loads((directory / MANIFEST).read_text("utf-8")))
    listed = {item["name"]: item for item in manifest["artifacts"]}
    if set(entries) != set(listed) | {MANIFEST}:
        raise ReleaseError("SHA256SUMS and the manifest list different files")
    present = {path.name for path in directory.iterdir()} - {*EVIDENCE_FILES, COSIGN_BUNDLE}
    extra = present - set(listed)
    if extra:
        raise ReleaseError(f"Files not in the signed release: {', '.join(sorted(extra))}")
    missing = set(listed) - present
    if missing and not partial:
        raise ReleaseError(f"Release files missing: {', '.join(sorted(missing))}")
    for name in sorted(present):
        check_file(directory / name, listed[name], entries[name])
    return {
        "verified": True,
        "version": manifest["version"],
        "public_tag": manifest["public_tag"],
        "public_source_commit": manifest["public_source_commit"],
        "public_source_tree": manifest["public_source_tree"],
        "checked_files": sorted(present),
        "not_present": sorted(missing),
        "trusted_comment": trusted,
        "cosign_bundle_present": cosign_bundle,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    make = commands.add_parser("assemble", help="write the manifest, checksums and signature")
    make.add_argument("--version", required=True, help="release version, for example 1.0.0")
    make.add_argument("--artifacts", type=Path, required=True, help="final release files")
    make.add_argument("--public-repo", type=Path, required=True, help="public repository")
    make.add_argument("--public-rev", required=True, help="public commit or tag released")
    make.add_argument("--private-repo", type=Path, default=ROOT, help="private repository")
    make.add_argument("--private-commit", required=True, help="commit the release was built from")
    make.add_argument("--snapshot-receipt", type=Path, required=True)
    make.add_argument(
        "--build-receipt",
        type=Path,
        action="append",
        default=[],
        help="a platform build receipt; together they must cover every release file",
    )
    make.add_argument("--output", type=Path, required=True, help="new public evidence directory")
    make.add_argument("--mapping", type=Path, required=True, help="new private mapping record")
    make.add_argument("--public-key", type=Path, default=PUBLIC_KEY)

    check = commands.add_parser("verify", help="verify evidence and the release files present")
    check.add_argument("directory", type=Path)
    check.add_argument("--public-key", type=Path, default=PUBLIC_KEY)
    check.add_argument(
        "--partial", action="store_true", help="allow listed files to be absent (for subsets)"
    )

    args = parser.parse_args(argv)
    try:
        if args.command == "assemble":
            record = assemble(
                version=args.version,
                artifacts=args.artifacts,
                public_repo=args.public_repo,
                public_rev=args.public_rev,
                private_repo=args.private_repo,
                private_commit=args.private_commit,
                snapshot_receipt=args.snapshot_receipt,
                build_receipts=args.build_receipt,
                output=args.output,
                mapping=args.mapping,
                public_key=args.public_key,
            )
            print(
                f"Assembled {record['version']} evidence in {args.output}: "
                f"release-manifest.json sha256 "
                f"{record['release_evidence'][MANIFEST]['sha256']}. "
                f"Private mapping: {args.mapping}. Nothing was uploaded."
            )
        else:
            print(json.dumps(verify(args.directory, args.public_key, args.partial), indent=2))
    except (ReleaseError, OSError, json.JSONDecodeError, subprocess.SubprocessError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
