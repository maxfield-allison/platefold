"""Cosign release signing, exercised with a recorded fake cosign and no real key."""

from __future__ import annotations

import hashlib
import io
import os
import subprocess
from pathlib import Path

import pytest

from tools import assemble_release
from tools import cosign_release as c

# Throwaway public keys made for these tests; no private half was kept.
P256_KEY = """-----BEGIN PUBLIC KEY-----
MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEspTw2MbJpkiNutFAxzKGmU11jaY+
fc1bWj1+QZoh1l/F7i7dsjqOPukQKg/WwBugVb1dncQ/BHpB6vWMltPK0Q==
-----END PUBLIC KEY-----
"""
P256_FINGERPRINT = "db96d928845bffe2779d27af5c6523dfe12350d2608390cfbe5d68203c2e560e"
P384_KEY = """-----BEGIN PUBLIC KEY-----
MHYwEAYHKoZIzj0CAQYFK4EEACIDYgAE4MQMOxQ1PZiFEVhFm8lsNBNRUboERuJi
MJDhbuC3EN6/dcNcrJo55CNwY8xy7NDK2vNbxvZsLtMjKz66D3riAmiRKpkV1Zw4
i+V7Ol27WXftnw+JQIODWR4/Lrg3NLPn
-----END PUBLIC KEY-----
"""
PASSWORD = "synthetic-password-value"
PRIVATE_KEY = "synthetic-encrypted-signing-key"
DIGEST_REF = "ghcr.io/example/platefold@sha256:" + "b" * 64


class FakeCosign:
    """Records each cosign call; fails the subcommands named in `fail`."""

    def __init__(self, fail=(), stderr="error: verification failed"):
        self.calls = []
        self.fail = set(fail)
        self.stderr = stderr

    def __call__(self, argv, **kwargs):
        self.calls.append((argv[1:], kwargs["env"]))
        args = argv[1:]
        if "--bundle" in args and args[0] == "sign-blob":
            Path(args[args.index("--bundle") + 1]).write_text("{}")
        failed = args[0] in self.fail
        return subprocess.CompletedProcess(
            argv, 1 if failed else 0, "", self.stderr if failed else ""
        )

    def commands(self):
        return [args[0] for args, _ in self.calls]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    key = tmp_path / "cosign.pub"
    key.write_text(P256_KEY)
    binary = tmp_path / "cosign"
    binary.write_bytes(b"pinned cosign stand-in")
    monkeypatch.setattr(c, "COSIGN_SHA256", hashlib.sha256(b"pinned cosign stand-in").hexdigest())
    environ = {
        "PATH": "/usr/bin",
        c.KEY_VARIABLE: PRIVATE_KEY,
        c.PASSWORD_VARIABLE: PASSWORD,
        c.BINARY_VARIABLE: str(binary),
    }
    return {"key": key, "environ": environ, "binary": binary, "tmp": tmp_path}


def signer(setup, fake=None, **overrides):
    environ = {**setup["environ"], **overrides}
    return c.Cosign(public_key=setup["key"], environ=environ, runner=fake or FakeCosign())


# --- the committed public key --------------------------------------------------------


def test_a_p256_public_key_is_accepted_and_named_by_its_der_fingerprint(tmp_path):
    key = tmp_path / "cosign.pub"
    key.write_text(P256_KEY)
    assert c.public_key_fingerprint(key) == P256_FINGERPRINT


def test_a_public_key_with_windows_line_endings_is_the_same_key(tmp_path):
    key = tmp_path / "cosign.pub"
    key.write_bytes(P256_KEY.replace("\n", "\r\n").encode())
    assert c.public_key_fingerprint(key) == P256_FINGERPRINT


def test_a_missing_public_key_refuses_signing(tmp_path):
    with pytest.raises(c.SigningRefused, match="missing; commit the release public key"):
        c.public_key_fingerprint(tmp_path / "cosign.pub")


def test_the_marked_placeholder_refuses_signing(tmp_path):
    key = tmp_path / "cosign.pub"
    key.write_text("PLACEHOLDER: this is not a Cosign public key.\n")
    with pytest.raises(c.SigningRefused, match="still the placeholder"):
        c.public_key_fingerprint(key)


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "not one PEM public key"),
        ("not a key\n", "not one PEM public key"),
        (P256_KEY + P256_KEY, "not one PEM public key"),
        ("-----BEGIN PUBLIC KEY-----\n!!!!\n-----END PUBLIC KEY-----\n", "not one PEM"),
        ("-----BEGIN PUBLIC KEY-----\nAAAA=A\n-----END PUBLIC KEY-----\n", "not valid base64"),
        (P384_KEY, "not an ECDSA P-256 public key"),
        (P256_KEY.replace("BEGIN PUBLIC", "BEGIN RSA PUBLIC"), "not one PEM public key"),
    ],
)
def test_anything_but_one_p256_public_key_refuses_signing(tmp_path, text, reason):
    key = tmp_path / "cosign.pub"
    key.write_text(text)
    with pytest.raises(c.SigningRefused, match=reason):
        c.public_key_fingerprint(key)


def test_the_committed_key_is_either_the_marked_placeholder_or_a_real_key():
    # Until the release key is generated, the placeholder holds its place and
    # every signing step refuses. Once it is committed it must parse.
    text = c.PUBLIC_KEY.read_text(encoding="ascii")
    if text.startswith(c.PLACEHOLDER):
        with pytest.raises(c.SigningRefused, match="placeholder"):
            c.public_key_fingerprint()
    else:
        assert len(c.public_key_fingerprint()) == 64


# --- the fail-closed gate ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("missing", "reason"),
    [
        (c.KEY_VARIABLE, "COSIGN_PRIVATE_KEY is required"),
        (c.PASSWORD_VARIABLE, "COSIGN_PASSWORD is required"),
        (c.BINARY_VARIABLE, "PLATEFOLD_COSIGN must name the installed cosign"),
    ],
)
def test_signing_refuses_without_each_input(setup, missing, reason):
    with pytest.raises(c.SigningRefused, match=reason):
        signer(setup, **{missing: ""})


def test_signing_refuses_a_cosign_that_is_not_the_pinned_release(setup):
    setup["binary"].write_bytes(b"something else")
    with pytest.raises(c.SigningRefused, match="is not cosign v3"):
        signer(setup)


def test_the_placeholder_refuses_even_with_every_secret_present(setup):
    setup["key"].write_text("PLACEHOLDER: this is not a Cosign public key.\n")
    fake = FakeCosign()
    with pytest.raises(c.SigningRefused, match="placeholder"):
        signer(setup, fake)
    assert fake.calls == []


# --- what cosign is asked to do -------------------------------------------------------


def test_the_self_test_signs_offline_and_verifies_with_the_committed_key(setup):
    fake = FakeCosign()
    signer(setup, fake).self_test(setup["tmp"])
    (sign, sign_env), (verify, verify_env) = fake.calls
    assert sign[:4] == ["sign-blob", "--yes", "--key", "env://COSIGN_PRIVATE_KEY"]
    assert {"--use-signing-config=false", "--tlog-upload=false"} <= set(sign)
    assert verify[:3] == ["verify-blob", "--key", str(setup["key"])]
    assert "--insecure-ignore-tlog=true" in verify
    # Only the signing call receives the key and password, and never as arguments.
    assert sign_env[c.KEY_VARIABLE] == PRIVATE_KEY and sign_env[c.PASSWORD_VARIABLE] == PASSWORD
    assert c.KEY_VARIABLE not in verify_env and c.PASSWORD_VARIABLE not in verify_env
    for args, _ in fake.calls:
        assert PASSWORD not in " ".join(args) and "synthetic" not in " ".join(args)


def test_a_key_that_belongs_to_another_public_key_fails_the_self_test(setup):
    fake = FakeCosign(fail={"verify-blob"})
    with pytest.raises(c.SigningRefused, match=r"does not match cosign\.pub"):
        signer(setup, fake).self_test(setup["tmp"])


def test_a_release_blob_signature_goes_to_the_log_and_is_verified(setup):
    fake = FakeCosign()
    blob, bundle = setup["tmp"] / "SHA256SUMS", setup["tmp"] / "SHA256SUMS.sigstore.json"
    blob.write_text("sums\n")
    signer(setup, fake).sign_blob(blob, bundle)
    (sign, _), (verify, _) = fake.calls
    # cosign writes and verifies a candidate beside the bundle, never the bundle itself.
    candidate = Path(sign[sign.index("--bundle") + 1])
    assert candidate != bundle and candidate.parent.parent == bundle.parent
    assert sign == ["sign-blob", "--yes", "--key", "env://COSIGN_PRIVATE_KEY", "--bundle",
                    str(candidate), str(blob)]  # fmt: skip
    assert verify == ["verify-blob", "--key", str(setup["key"]), "--bundle", str(candidate),
                      str(blob)]  # fmt: skip
    assert bundle.is_file()
    assert sorted(path.name for path in setup["tmp"].iterdir()) == [
        "SHA256SUMS", "SHA256SUMS.sigstore.json", "cosign", "cosign.pub"
    ]  # fmt: skip


def test_a_blob_signature_that_does_not_verify_stops_and_leaves_no_bundle(setup):
    fake = FakeCosign(fail={"verify-blob"})
    blob, bundle = setup["tmp"] / "SHA256SUMS", setup["tmp"] / "SHA256SUMS.sigstore.json"
    blob.write_text("sums\n")
    with pytest.raises(c.SigningRefused, match="cosign verify-blob failed"):
        signer(setup, fake).sign_blob(blob, bundle)
    assert not bundle.exists()


class TimesOut(FakeCosign):
    """Like FakeCosign, but the named subcommand times out after its side effects."""

    def __init__(self, command):
        super().__init__()
        self.command = command

    def __call__(self, argv, **kwargs):
        result = super().__call__(argv, **kwargs)
        if argv[1] == self.command:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        return result


@pytest.mark.parametrize("command", ["sign-blob", "verify-blob"])
def test_a_timed_out_blob_signature_leaves_no_bundle_or_scratch(setup, command):
    blob, bundle = setup["tmp"] / "SHA256SUMS", setup["tmp"] / "SHA256SUMS.sigstore.json"
    blob.write_text("sums\n")
    before = sorted(path.name for path in setup["tmp"].iterdir())

    with pytest.raises(subprocess.TimeoutExpired):
        signer(setup, TimesOut(command)).sign_blob(blob, bundle)

    assert not bundle.exists()
    assert sorted(path.name for path in setup["tmp"].iterdir()) == before


def test_an_existing_bundle_is_never_signed_over(setup):
    fake = FakeCosign()
    blob, bundle = setup["tmp"] / "SHA256SUMS", setup["tmp"] / "SHA256SUMS.sigstore.json"
    blob.write_text("sums\n")
    bundle.write_text("{}")
    with pytest.raises(c.SigningRefused, match="already exists"):
        signer(setup, fake).sign_blob(blob, bundle)
    assert fake.calls == []


def test_an_error_line_that_quotes_a_secret_is_not_repeated(setup):
    fake = FakeCosign(fail={"sign"}, stderr=f"bad password {PASSWORD}")
    with pytest.raises(c.SigningRefused) as refused:
        signer(setup, fake).sign_image(DIGEST_REF)
    assert PASSWORD not in str(refused.value)
    assert str(refused.value) == "cosign sign failed (exit 1)"


def test_an_error_line_that_quotes_one_line_of_a_multi_line_key_is_not_repeated(setup):
    second_line = "synthetic-line-two"
    multi_line_value = f"synthetic-line-one\n{second_line}\nsynthetic-line-three\n"
    fake = FakeCosign(fail={"sign"}, stderr=f"unexpected data near {second_line}")
    with pytest.raises(c.SigningRefused) as refused:
        signer(setup, fake, **{c.KEY_VARIABLE: multi_line_value}).sign_image(DIGEST_REF)
    assert second_line not in str(refused.value)
    assert str(refused.value) == "cosign sign failed (exit 1)"


@pytest.mark.parametrize(
    "reference", ["ghcr.io/example/platefold:v1.0.0", "ghcr.io/example/platefold@sha256:abc"]
)
def test_images_are_signed_by_digest_only(setup, reference):
    fake = FakeCosign()
    with pytest.raises(c.SigningRefused, match="by digest only"):
        signer(setup, fake).sign_image(reference)
    with pytest.raises(c.SigningRefused, match="by digest only"):
        signer(setup, fake).ensure_image_signed(reference)
    assert fake.calls == []


def test_an_image_is_signed_then_verified_with_the_committed_key(setup):
    fake = FakeCosign(fail=())
    cosign = signer(setup, fake)
    cosign.sign_image(DIGEST_REF)
    assert [args for args, _ in fake.calls] == [
        ["sign", "--yes", "--key", "env://COSIGN_PRIVATE_KEY", DIGEST_REF],
        ["verify", "--key", str(setup["key"]), DIGEST_REF],
    ]


def test_an_image_signature_that_does_not_verify_fails(setup):
    fake = FakeCosign(fail={"verify"})
    with pytest.raises(c.SigningRefused, match="cosign verify failed"):
        signer(setup, fake).sign_image(DIGEST_REF)


def test_a_retried_publication_does_not_sign_an_already_signed_image_again(setup):
    fake = FakeCosign()
    signer(setup, fake).ensure_image_signed(DIGEST_REF)
    assert fake.commands() == ["verify"]


def test_an_unsigned_image_is_signed_and_must_then_verify(setup):
    class FirstVerifyFails(FakeCosign):
        def __call__(self, argv, **kwargs):
            result = super().__call__(argv, **kwargs)
            if self.commands() == ["verify"]:
                result.returncode = 1
            return result

    fake = FirstVerifyFails()
    signer(setup, fake).ensure_image_signed(DIGEST_REF)
    assert fake.commands() == ["verify", "sign", "verify"]


# --- signing an assembled release --------------------------------------------------------


@pytest.fixture
def evidence(tmp_path):
    folder = tmp_path / "evidence"
    folder.mkdir()
    for name in assemble_release.EVIDENCE_FILES:
        (folder / name).write_text(name)
    return folder


def test_unverified_release_evidence_is_never_signed(setup, evidence, monkeypatch):
    def refuse(folder, partial):
        raise assemble_release.ReleaseError("The release-evidence signature does not verify")

    monkeypatch.setattr(assemble_release, "verify", refuse)
    fake = FakeCosign()
    with pytest.raises(c.SigningRefused, match="release evidence does not verify"):
        c.sign_checksums(evidence, public_key=setup["key"], environ=setup["environ"], runner=fake)
    assert fake.calls == []
    assert not (evidence / assemble_release.COSIGN_BUNDLE).exists()


def test_verified_release_evidence_gets_a_verified_bundle(setup, evidence, monkeypatch):
    seen = {}

    def accept(folder, partial):
        seen.update(folder=folder, partial=partial)
        return {"version": "1.0.0"}

    monkeypatch.setattr(assemble_release, "verify", accept)
    fake = FakeCosign()
    record = c.sign_checksums(
        evidence, public_key=setup["key"], environ=setup["environ"], runner=fake
    )
    assert seen == {"folder": evidence, "partial": True}
    # Self-test, then the release signature and its verification.
    assert fake.commands() == ["sign-blob", "verify-blob", "sign-blob", "verify-blob"]
    assert fake.calls[2][0][-1] == str(evidence / "SHA256SUMS")
    assert record["bundle"] == "SHA256SUMS.sigstore.json"
    assert record["public_key_sha256"] == P256_FINGERPRINT
    assert (evidence / "SHA256SUMS.sigstore.json").is_file()


def test_the_command_line_refuses_while_the_key_is_the_placeholder(monkeypatch, capsys, tmp_path):
    placeholder = tmp_path / "cosign.pub"
    placeholder.write_text("PLACEHOLDER: this is not a Cosign public key.\n")
    monkeypatch.setattr(c, "PUBLIC_KEY", placeholder)
    assert c.main(["check"]) == 1
    assert "Refused: cosign.pub is still the placeholder" in capsys.readouterr().err


# --- installing the pinned binary -----------------------------------------------------------


class Download(io.BytesIO):
    def __init__(self, data, url=c.COSIGN_URL):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


def test_the_pinned_cosign_is_installed_executable(tmp_path, monkeypatch):
    monkeypatch.setattr(c.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(c, "COSIGN_SHA256", hashlib.sha256(b"cosign bytes").hexdigest())
    seen = []

    def opener(url, timeout):
        seen.append(url)
        return Download(b"cosign bytes")

    path = c.install(tmp_path / "bin", opener=opener)
    assert seen == [c.COSIGN_URL]
    assert path.read_bytes() == b"cosign bytes"
    # Windows reports no execute bits for a file without an executable
    # extension; the binary only ever runs on the Linux release jobs.
    if os.name != "nt":
        assert path.stat().st_mode & 0o111
    assert sorted(p.name for p in (tmp_path / "bin").iterdir()) == ["cosign"]


def test_other_bytes_are_never_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(c.platform, "machine", lambda: "x86_64")
    with pytest.raises(c.SigningRefused, match="not the pinned"):
        c.install(tmp_path / "bin", opener=lambda url, timeout: Download(b"tampered"))
    assert list((tmp_path / "bin").iterdir()) == []


def test_a_download_redirected_off_github_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(c.platform, "machine", lambda: "x86_64")
    download = Download(b"x", url="https://example.invalid/cosign")
    with pytest.raises(c.SigningRefused, match="left GitHub"):
        c.install(tmp_path / "bin", opener=lambda url, timeout: download)
    assert list((tmp_path / "bin").iterdir()) == []


def test_the_pin_is_one_release_and_its_digest():
    assert c.COSIGN_VERSION in c.COSIGN_URL and c.COSIGN_URL.endswith("/cosign-linux-amd64")
    assert len(c.COSIGN_SHA256) == 64 and int(c.COSIGN_SHA256, 16) >= 0
