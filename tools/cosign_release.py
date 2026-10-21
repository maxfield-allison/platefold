#!/usr/bin/env python3
"""Sign PlateFold release images and checksum lists with the project's Cosign key.

    install DIRECTORY      download the pinned cosign into DIRECTORY and check
                           its SHA-256 before it is made executable
    check                  refuse unless packaging/cosign.pub is a real public
                           key, the signing key is in the environment and the
                           two belong together
    sign-checksums FOLDER  sign FOLDER/SHA256SUMS of an assembled release, which
                           must already carry a valid release-evidence
                           signature, and verify the new Cosign bundle

The committed public key, packaging/cosign.pub, is the only trust root. Every
signature is verified against it straight after it is made, and nothing signs
while that file is missing, still the placeholder, or not an ECDSA P-256 public
key. Before the first signature of a run, an offline self-test signs a scratch
file with no transparency-log upload and verifies it with the committed key, so
a key that belongs to another public key stops the job before anything reaches
a public log or registry.

The encrypted private key and its password arrive only through the
environment, as COSIGN_PRIVATE_KEY and COSIGN_PASSWORD. cosign reads the key as
env://COSIGN_PRIVATE_KEY and the password from its own environment; neither is
ever a command argument or printed. PLATEFOLD_COSIGN names the cosign binary,
which must be the pinned release byte for byte.

Release signatures are recorded in the public Sigstore transparency log, so
anyone can verify them with `cosign verify --key cosign.pub` or
`cosign verify-blob --key cosign.pub` and no extra flags.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import os
import platform
import re
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path

try:
    from tools import assemble_release
except ModuleNotFoundError:  # Direct execution puts tools/, not the root, on sys.path.
    import assemble_release

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_KEY = ROOT / "packaging" / "cosign.pub"
# The release cosign. Bump both together after reviewing the new release.
COSIGN_VERSION = "v3.1.3"
COSIGN_SHA256 = "4629c757b7618056f8ddd7e2625ae9fdd94c0372a65049520bc7d9df9efc7f71"
COSIGN_URL = (
    f"https://github.com/sigstore/cosign/releases/download/{COSIGN_VERSION}/cosign-linux-amd64"
)
COSIGN_DOWNLOAD_HOSTS = frozenset(
    {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
MAX_COSIGN_BYTES = 512 * 1024 * 1024
KEY_VARIABLE = "COSIGN_PRIVATE_KEY"
PASSWORD_VARIABLE = "COSIGN_PASSWORD"
BINARY_VARIABLE = "PLATEFOLD_COSIGN"
SECRETS = (KEY_VARIABLE, PASSWORD_VARIABLE)
PLACEHOLDER = "PLACEHOLDER"
# DER SubjectPublicKeyInfo header for an uncompressed ECDSA P-256 point, the
# key type `cosign generate-key-pair` writes. The 65 bytes that follow are 0x04,
# then X and Y.
P256_SPKI_PREFIX = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
P256_SPKI_LENGTH = len(P256_SPKI_PREFIX) + 65
PEM = re.compile(
    r"-----BEGIN PUBLIC KEY-----\n([A-Za-z0-9+/=\n]+?)\n?-----END PUBLIC KEY-----\n?\Z"
)
IMAGE_BY_DIGEST = re.compile(r"[a-z0-9][a-z0-9._/-]*(?::[0-9]+)?/[a-z0-9._/-]+@sha256:[0-9a-f]{64}")
COSIGN_TIMEOUT_SECONDS = 600
# Without these, a signature goes to the public transparency log. The offline
# self-test must leave no trace there.
OFFLINE_SIGN = ("--use-signing-config=false", "--tlog-upload=false")
OFFLINE_VERIFY = ("--insecure-ignore-tlog=true",)

Runner = Callable[..., subprocess.CompletedProcess]


class SigningRefused(Exception):
    """Signing must not go ahead; the message says why and names no secret."""


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def public_key_fingerprint(path: Path | None = None) -> str:
    """Return the SHA-256 of the committed public key's DER bytes, or refuse.

    Refuses a missing file, the marked placeholder, and anything that is not a
    single PEM ECDSA P-256 public key. The fingerprint names the key in logs and
    receipts; it is public.
    """
    path = path or PUBLIC_KEY
    if not path.is_file():
        raise SigningRefused(f"{path.name} is missing; commit the release public key first")
    text = path.read_text(encoding="ascii", errors="replace").replace("\r\n", "\n")
    if text.lstrip().startswith(PLACEHOLDER):
        raise SigningRefused(
            f"{path.name} is still the placeholder; commit the release public key first"
        )
    match = PEM.fullmatch(text.strip() + "\n")
    if not match:
        raise SigningRefused(f"{path.name} is not one PEM public key")
    try:
        der = base64.b64decode(match.group(1).replace("\n", ""), validate=True)
    except binascii.Error:
        raise SigningRefused(f"{path.name} is not valid base64") from None
    if (
        len(der) != P256_SPKI_LENGTH
        or not der.startswith(P256_SPKI_PREFIX)
        or der[len(P256_SPKI_PREFIX)] != 0x04
    ):
        raise SigningRefused(f"{path.name} is not an ECDSA P-256 public key")
    return hashlib.sha256(der).hexdigest()


class Cosign:
    """The pinned cosign, signing with the environment's key and verifying with the committed one.

    Construction is the fail-closed gate: it refuses unless the committed public
    key, the binary and both secret variables are all in place.
    """

    def __init__(
        self,
        public_key: Path | None = None,
        environ: Mapping[str, str] | None = None,
        runner: Runner = subprocess.run,
    ) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        self.public_key = public_key or PUBLIC_KEY
        self.fingerprint = public_key_fingerprint(self.public_key)
        if not self.environ.get(KEY_VARIABLE):
            raise SigningRefused(f"{KEY_VARIABLE} is required")
        # The release key is always password-protected; an empty password means
        # the variable is missing or mis-scoped, not that the key has none.
        if not self.environ.get(PASSWORD_VARIABLE):
            raise SigningRefused(f"{PASSWORD_VARIABLE} is required")
        binary = self.environ.get(BINARY_VARIABLE, "")
        if not binary or not Path(binary).is_file():
            raise SigningRefused(f"{BINARY_VARIABLE} must name the installed cosign")
        if sha256_file(Path(binary)) != COSIGN_SHA256:
            raise SigningRefused(f"{binary} is not cosign {COSIGN_VERSION}")
        self.binary = binary
        self.runner = runner

    def _run(self, *args: str, signing: bool) -> subprocess.CompletedProcess:
        environment = dict(self.environ)
        if not signing:
            # Verification needs only the public key.
            for name in SECRETS:
                environment.pop(name, None)
        result = self.runner(
            [self.binary, *args],
            env=environment,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=COSIGN_TIMEOUT_SECONDS,
            check=False,
        )
        if result.returncode:
            raise SigningRefused(
                f"cosign {args[0]} failed (exit {result.returncode}){self._hint(result.stderr)}"
            )
        return result

    def _hint(self, stderr: str | None) -> str:
        """The last line of cosign's error, unless it could quote a secret."""
        lines = (stderr or "").strip().splitlines()
        if not lines:
            return ""
        line = lines[-1][:300]
        # The private key is multi-line PEM, so one of its lines can appear on
        # its own; compare each line of each secret as well as the whole value.
        fragments = {
            fragment.strip()
            for name in SECRETS
            for fragment in [self.environ.get(name, ""), *self.environ.get(name, "").splitlines()]
            if len(fragment.strip()) >= 8
        }
        if "PRIVATE KEY" in line or any(fragment in line for fragment in fragments):
            return ""
        return f": {line}"

    def self_test(self, scratch: Path) -> None:
        """Prove the environment's key belongs to the committed public key, offline."""
        blob = scratch / "cosign-self-test.txt"
        bundle = scratch / "cosign-self-test.sigstore.json"
        blob.write_text(f"Plate|Fold release key self-test {self.fingerprint}\n")
        key = f"env://{KEY_VARIABLE}"
        self._run(
            "sign-blob",
            "--yes",
            "--key",
            key,
            *OFFLINE_SIGN,
            "--bundle",
            str(bundle),
            str(blob),
            signing=True,
        )
        try:
            self._run(
                "verify-blob",
                "--key",
                str(self.public_key),
                *OFFLINE_VERIFY,
                "--bundle",
                str(bundle),
                str(blob),
                signing=False,
            )
        except SigningRefused as error:
            raise SigningRefused(
                f"the signing key does not match {self.public_key.name} ({error})"
            ) from None

    def sign_blob(self, path: Path, bundle: Path) -> None:
        """Sign path, verify the new bundle with the committed key, then place it at bundle.

        cosign writes into a scratch folder beside bundle. The new bundle moves
        into place only after it verifies. A signing or verification failure
        publishes no new bundle. An existing destination is refused unchanged.
        """
        if bundle.exists():
            raise SigningRefused(f"{bundle.name} already exists; nothing is signed twice")
        key = f"env://{KEY_VARIABLE}"
        # Beside the destination, so the final move is a same-filesystem rename.
        with tempfile.TemporaryDirectory(
            prefix=".cosign-unverified-", dir=bundle.parent
        ) as scratch:
            candidate = Path(scratch) / bundle.name
            self._run(
                "sign-blob",
                "--yes",
                "--key",
                key,
                "--bundle",
                str(candidate),
                str(path),
                signing=True,
            )
            self.verify_blob(path, candidate)
            os.replace(candidate, bundle)

    def verify_blob(self, path: Path, bundle: Path) -> None:
        self._run(
            "verify-blob",
            "--key",
            str(self.public_key),
            "--bundle",
            str(bundle),
            str(path),
            signing=False,
        )

    def sign_image(self, reference: str) -> None:
        """Sign an image by digest, then verify that signature with the committed key."""
        if not IMAGE_BY_DIGEST.fullmatch(reference):
            raise SigningRefused(f"images are signed by digest only, not {reference!r}")
        self._run("sign", "--yes", "--key", f"env://{KEY_VARIABLE}", reference, signing=True)
        self.verify_image(reference)

    def verify_image(self, reference: str) -> None:
        self._run("verify", "--key", str(self.public_key), reference, signing=False)

    def ensure_image_signed(self, reference: str) -> None:
        """Sign unless a signature by the committed key already verifies.

        A retried publication then adds no second signature or log entry.
        """
        if not IMAGE_BY_DIGEST.fullmatch(reference):
            raise SigningRefused(f"images are signed by digest only, not {reference!r}")
        try:
            self.verify_image(reference)
        except SigningRefused:
            self.sign_image(reference)
            return
        print(f"{reference} already carries a valid release signature")


def ready_signer(scratch: Path, **kwargs) -> Cosign:
    """A Cosign that has passed the gate and the offline self-test."""
    cosign = Cosign(**kwargs)
    cosign.self_test(scratch)
    print(f"Release key {cosign.fingerprint} matches the environment's signing key")
    return cosign


def sign_checksums(folder: Path, **kwargs) -> dict:
    """Add a Cosign bundle to an assembled release's evidence, after checking it.

    The folder holds the evidence `assemble_release.py assemble` wrote. Its
    release-evidence signature and manifest must verify first, so this signs only
    a SHA256SUMS the release process already vouched for.
    """
    bundle = folder / assemble_release.COSIGN_BUNDLE
    if bundle.exists():
        raise SigningRefused(f"{bundle.name} already exists; nothing is signed twice")
    try:
        evidence = assemble_release.verify(folder, partial=True)
    except assemble_release.ReleaseError as error:
        raise SigningRefused(f"the release evidence does not verify: {error}") from None
    with tempfile.TemporaryDirectory(prefix="platefold-cosign-") as scratch:
        cosign = ready_signer(Path(scratch), **kwargs)
        cosign.sign_blob(folder / assemble_release.SUMS, bundle)
    return {
        "version": evidence["version"],
        "public_key_sha256": cosign.fingerprint,
        "bundle": bundle.name,
        "bundle_sha256": sha256_file(bundle),
    }


def install(directory: Path, opener: Callable = urllib.request.urlopen) -> Path:
    """Download the pinned cosign into directory/cosign, refusing any other bytes."""
    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise SigningRefused("the pinned cosign is the linux-amd64 release")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / "cosign"
    partial = directory / ".cosign.download"
    digest = hashlib.sha256()
    try:
        with opener(COSIGN_URL, timeout=120) as response, partial.open("wb") as handle:
            if urllib.parse.urlparse(response.geturl()).hostname not in COSIGN_DOWNLOAD_HOSTS:
                raise SigningRefused("the cosign download left GitHub")
            size = 0
            while chunk := response.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_COSIGN_BYTES:
                    raise SigningRefused("the cosign download is too large")
                digest.update(chunk)
                handle.write(chunk)
        if digest.hexdigest() != COSIGN_SHA256:
            raise SigningRefused(f"the downloaded cosign is not the pinned {COSIGN_VERSION}")
        partial.chmod(0o755)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("install", help="download and check the pinned cosign")
    setup.add_argument("directory", type=Path)
    commands.add_parser("check", help="refuse unless signing can go ahead")
    sums = commands.add_parser("sign-checksums", help="sign an assembled release's SHA256SUMS")
    sums.add_argument("folder", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "install":
            print(f"Installed cosign {COSIGN_VERSION} at {install(args.directory)}")
        elif args.command == "check":
            with tempfile.TemporaryDirectory(prefix="platefold-cosign-") as scratch:
                ready_signer(Path(scratch))
        else:
            record = sign_checksums(args.folder)
            print(
                f"Signed {args.folder / assemble_release.SUMS} for {record['version']}: "
                f"{record['bundle']} sha256 {record['bundle_sha256']}, verified with "
                f"release key {record['public_key_sha256']}"
            )
    except (SigningRefused, OSError, subprocess.SubprocessError) as error:
        print(f"Refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
