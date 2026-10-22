"""Native build downloads retry transient network failures and nothing else."""

from __future__ import annotations

import errno
import hashlib
import http.client
import io
import json
import re
import socket
import ssl
import urllib.error
from email.message import Message

import pytest

from tools import download_retry, native_downloads, native_requirements, rust_toolchain

URL = "https://upstream.example/linuxdeploy-x86_64.AppImage"


def handshake() -> urllib.error.URLError:
    """The failure in the CI log that reported this: a TLS handshake timeout."""
    return urllib.error.URLError(TimeoutError("_ssl.c:975: The handshake operation timed out"))


# Each native download tool, with the error it raises.
DOWNLOADERS = [
    pytest.param(native_downloads, native_downloads.DownloadError, id="native_downloads"),
    pytest.param(rust_toolchain, rust_toolchain.ToolchainError, id="rust_toolchain"),
]


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(URL, code, "reason", Message(), io.BytesIO())


class Response:
    """A response body; fail is raised after the body's bytes, as a dropped transfer."""

    def __init__(self, body: bytes, fail: BaseException | None = None, length: int | None = None):
        self.stream = io.BytesIO(body)
        self.fail = fail
        # http.client's count of Content-Length bytes still unread.
        self.length = length

    def read(self, size: int) -> bytes:
        chunk = self.stream.read(size)
        if not chunk and self.fail is not None:
            raise self.fail
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Upstream:
    """An injected opener that answers each attempt from a script."""

    def __init__(self, *outcomes, destination=None):
        self.outcomes = list(outcomes)
        self.destination = destination
        self.calls: list[dict] = []

    def __call__(self, request, timeout):
        self.calls.append(
            {
                "url": request.full_url,
                "timeout": timeout,
                "headers": dict(request.header_items()),
                "partial_present": self.destination is not None and self.destination.exists(),
            }
        )
        outcome = self.outcomes[len(self.calls) - 1]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


# Factories, so every test gets a fresh response or exception.
TRANSIENT = [
    pytest.param(handshake, id="tls-handshake-timeout"),
    pytest.param(
        lambda: urllib.error.URLError(ConnectionRefusedError(111, "refused")), id="refused"
    ),
    pytest.param(
        lambda: urllib.error.URLError(OSError(errno.ENETUNREACH, "unreachable")), id="no-route"
    ),
    pytest.param(
        lambda: urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "again")), id="dns-again"
    ),
    pytest.param(lambda: TimeoutError("The read operation timed out"), id="read-timeout"),
    pytest.param(lambda: http.client.RemoteDisconnected("closed"), id="remote-disconnected"),
    pytest.param(
        lambda: Response(b"part", fail=ConnectionResetError(104, "reset")), id="reset-mid-body"
    ),
    pytest.param(
        lambda: Response(b"part", fail=http.client.IncompleteRead(b"", 9)), id="incomplete"
    ),
    pytest.param(lambda: Response(b"part", length=9), id="short-content-length"),
    pytest.param(lambda: Response(b"part", fail=ssl.SSLEOFError()), id="tls-eof"),
    pytest.param(lambda: http_error(429), id="http-429"),
    pytest.param(lambda: http_error(500), id="http-500"),
    pytest.param(lambda: http_error(502), id="http-502"),
    pytest.param(lambda: http_error(503), id="http-503"),
]

PERMANENT = [
    pytest.param(lambda: http_error(400), id="http-400"),
    pytest.param(lambda: http_error(403), id="http-403"),
    pytest.param(lambda: http_error(404), id="http-404"),
    pytest.param(lambda: http_error(410), id="http-410"),
    pytest.param(
        lambda: urllib.error.URLError(ssl.SSLCertVerificationError("bad cert")), id="cert"
    ),
    pytest.param(
        lambda: urllib.error.URLError(socket.gaierror(socket.EAI_NONAME, "none")), id="no-host"
    ),
    pytest.param(lambda: urllib.error.URLError("unknown url type: ftpx"), id="bad-scheme"),
]


@pytest.mark.parametrize(("tool", "failure"), DOWNLOADERS)
@pytest.mark.parametrize("transient", TRANSIENT)
def test_a_transient_failure_is_retried_until_the_download_succeeds(
    tool, failure, transient, tmp_path, capsys
):
    destination = tmp_path / "linuxdeploy"
    upstream = Upstream(transient(), handshake(), Response(b"reviewed bytes"))
    delays = []
    tool.http_download(URL, destination, opener=upstream, sleep=delays.append)
    assert destination.read_bytes() == b"reviewed bytes"
    assert len(upstream.calls) == download_retry.ATTEMPTS == 3
    assert delays == [download_retry.BACKOFF_SECONDS, 2 * download_retry.BACKOFF_SECONDS]
    assert {call["url"] for call in upstream.calls} == {URL}
    assert {call["timeout"] for call in upstream.calls} == {120}
    assert f"Download of {URL} failed" in capsys.readouterr().err


@pytest.mark.parametrize(("tool", "failure"), DOWNLOADERS)
def test_a_transient_failure_on_every_attempt_names_the_attempts(tool, failure, tmp_path):
    destination = tmp_path / "linuxdeploy"
    upstream = Upstream(*[handshake() for _ in range(download_retry.ATTEMPTS)])
    delays = []
    with pytest.raises(
        failure,
        match=rf"^Download of {re.escape(URL)} failed after 3 attempts: <urlopen error _ssl\.c:975: "
        r"The handshake operation timed out>$",
    ):
        tool.http_download(URL, destination, opener=upstream, sleep=delays.append)
    assert len(upstream.calls) == 3
    assert delays == [10, 20]
    assert not destination.exists()


@pytest.mark.parametrize(("tool", "failure"), DOWNLOADERS)
@pytest.mark.parametrize("permanent", PERMANENT)
def test_a_permanent_failure_fails_at_once(tool, failure, permanent, tmp_path):
    destination = tmp_path / "linuxdeploy"
    upstream = Upstream(permanent())
    with pytest.raises(failure, match=rf"^Download of {re.escape(URL)} failed after 1 attempt: "):
        tool.http_download(URL, destination, opener=upstream, sleep=pytest.fail)
    assert len(upstream.calls) == 1
    assert not destination.exists()


@pytest.mark.parametrize(("tool", "failure"), DOWNLOADERS)
def test_a_404_says_so(tool, failure, tmp_path):
    with pytest.raises(failure, match=r"failed after 1 attempt: HTTP Error 404: reason$"):
        tool.http_download(
            URL, tmp_path / "missing", opener=Upstream(http_error(404)), sleep=pytest.fail
        )


@pytest.mark.parametrize(("tool", "failure"), DOWNLOADERS)
def test_a_hash_mismatch_after_a_successful_download_is_not_retried(tool, failure, tmp_path):
    reviewed = b"reviewed bytes"
    item = {"url": URL, "size": len(reviewed), "sha256": hashlib.sha256(reviewed).hexdigest()}
    upstream = Upstream(Response(b"altered bytes!"), Response(reviewed))
    destination = tmp_path / "linuxdeploy"

    def download(url, path):
        tool.http_download(url, path, opener=upstream, sleep=pytest.fail)

    with pytest.raises(failure, match="is not the reviewed"):
        tool.fetch_verified(item, destination, download)
    assert len(upstream.calls) == 1
    assert not destination.exists()


@pytest.mark.parametrize(("tool", "failure"), DOWNLOADERS)
def test_a_partial_file_is_removed_before_the_retry(tool, failure, tmp_path):
    destination = tmp_path / "linuxdeploy"
    upstream = Upstream(
        Response(b"first half ", fail=http.client.IncompleteRead(b"", 11)),
        Response(b"the whole reviewed download"),
        destination=destination,
    )
    tool.http_download(URL, destination, opener=upstream, sleep=lambda delay: None)
    assert [call["partial_present"] for call in upstream.calls] == [False, False]
    assert destination.read_bytes() == b"the whole reviewed download"


@pytest.mark.parametrize(("tool", "failure"), DOWNLOADERS)
def test_the_size_limit_is_kept_and_not_retried(tool, failure, tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "MAX_DOWNLOAD", 4)
    destination = tmp_path / "linuxdeploy"
    upstream = Upstream(Response(b"12345"), Response(b"1234"))
    with pytest.raises(failure, match=rf"^{re.escape(URL)} exceeds the download limit$"):
        tool.http_download(URL, destination, opener=upstream, sleep=pytest.fail)
    assert len(upstream.calls) == 1
    assert not destination.exists()


@pytest.mark.parametrize(("tool", "failure"), DOWNLOADERS)
def test_an_existing_destination_is_neither_overwritten_nor_removed(tool, failure, tmp_path):
    destination = tmp_path / "linuxdeploy"
    destination.write_bytes(b"not ours")
    upstream = Upstream(Response(b"downloaded"), Response(b"downloaded"))
    with pytest.raises(failure, match="failed after 1 attempt"):
        tool.http_download(URL, destination, opener=upstream, sleep=pytest.fail)
    assert destination.read_bytes() == b"not ours"


def test_pypi_metadata_is_retried_and_a_missing_release_fails_at_once():
    release = {
        "urls": [
            {
                "filename": "demo-1.0-py3-none-any.whl",
                "packagetype": "bdist_wheel",
                "digests": {"sha256": "a" * 64},
            }
        ]
    }
    upstream = Upstream(http_error(502), Response(json.dumps(release).encode()))
    delays = []
    wheels = native_requirements.pypi_wheels("demo", "1.0", opener=upstream, sleep=delays.append)
    assert wheels == [{"filename": "demo-1.0-py3-none-any.whl", "sha256": "a" * 64}]
    assert delays == [10]
    assert upstream.calls[0]["url"] == "https://pypi.org/pypi/demo/1.0/json"

    missing = Upstream(http_error(404))
    with pytest.raises(native_requirements.LockError, match="failed after 1 attempt: HTTP Error"):
        native_requirements.pypi_wheels("demo", "9.9", opener=missing, sleep=pytest.fail)
    assert len(missing.calls) == 1

    down = Upstream(*[handshake() for _ in range(3)])
    with pytest.raises(native_requirements.LockError, match="failed after 3 attempts"):
        native_requirements.pypi_wheels("demo", "1.0", opener=down, sleep=lambda delay: None)
