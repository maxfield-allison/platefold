"""Bounded retries for the build and release tools' HTTP downloads.

A native build fetches reviewed, hash-pinned inputs from upstream hosts, and a
single timeout used to fail the whole job. Here a transient network failure
(a refused or reset connection, a TLS handshake or read timeout, a body cut
short, HTTP 429 or 5xx) removes any partial file and repeats the same request,
up to ATTEMPTS times with a growing pause. Anything else fails at once: a 4xx
answer, a body over the size limit, a certificate that does not verify.

A retry only repeats the request. The caller still checks the size and SHA-256
of what arrived, and a mismatch is never retried, so retrying cannot let in
bytes the manifest did not review.
"""

from __future__ import annotations

import errno
import hashlib
import http.client
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

ATTEMPTS = 3
BACKOFF_SECONDS = 10
CHUNK = 1024 * 1024
# Connection failures that are not ConnectionError subclasses.
NETWORK_ERRNOS = frozenset({errno.ENETDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH})
# Failures of a connection or a transfer, as distinct from what the server said.
TRANSIENT = (
    TimeoutError,  # connect, TLS handshake or read timeout
    ConnectionError,  # refused, reset, aborted, or closed before the answer
    http.client.IncompleteRead,
    ssl.SSLEOFError,
    ssl.SSLZeroReturnError,
)

Opener = Callable[..., Any]


class TooLarge(Exception):
    """The response body exceeds the caller's limit. Never retried."""


class Failed(Exception):
    """The download did not succeed; error is the last failure.

    Its text, "failed after N attempts", completes the caller's own message.
    """

    def __init__(self, error: BaseException, attempts: int):
        self.error = error
        self.attempts = attempts
        super().__init__(f"failed after {attempts} attempt{'' if attempts == 1 else 's'}")


def transient(error: BaseException) -> bool:
    """Whether repeating the same request could succeed."""
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or 500 <= error.code <= 599
    if isinstance(error, urllib.error.URLError):
        error = error.reason if isinstance(error.reason, BaseException) else error
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    return isinstance(error, TRANSIENT) or (
        isinstance(error, OSError) and error.errno in NETWORK_ERRNOS
    )


def describe(error: BaseException) -> str:
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return str(error) or type(error).__name__


def _stream(response, limit: int | None, write: Callable[[bytes], object]) -> None:
    size = 0
    while chunk := response.read(CHUNK):
        size += len(chunk)
        if limit is not None and size > limit:
            raise TooLarge(f"The response exceeds {limit} bytes")
        write(chunk)
    # http.client ends a Content-Length body early at EOF without raising.
    remaining = getattr(response, "length", None)
    if remaining:
        raise ConnectionError(
            f"The connection closed after {size} bytes, {remaining} short of the declared length"
        )


def _save(opener: Opener, request, destination: Path, limit: int | None, timeout: float) -> str:
    digest = hashlib.sha256()
    with opener(request, timeout=timeout) as response:
        handle = destination.open("xb")

        def write(chunk: bytes) -> None:
            digest.update(chunk)
            handle.write(chunk)

        try:
            with handle:
                _stream(response, limit, write)
        except BaseException:
            # Closed first, so Windows can remove it; a retry opens it again with "xb".
            destination.unlink(missing_ok=True)
            raise
    return digest.hexdigest()


def _read(opener: Opener, request, limit: int | None, timeout: float) -> bytes:
    data = bytearray()
    with opener(request, timeout=timeout) as response:
        _stream(response, limit, data.extend)
    return bytes(data)


def _retry(attempt_once: Callable[[], Any], name: str, sleep: Callable[[float], object]):
    attempt = 1
    while True:
        try:
            return attempt_once()
        except (OSError, http.client.HTTPException) as error:
            if not transient(error) or attempt == ATTEMPTS:
                raise Failed(error, attempt) from error
            delay = BACKOFF_SECONDS * attempt
            print(
                f"Download of {name} failed ({describe(error)}), attempt {attempt} of "
                f"{ATTEMPTS}; retrying in {delay} s",
                file=sys.stderr,
                flush=True,
            )
            sleep(delay)
            attempt += 1


def save(
    request: urllib.request.Request,
    destination: Path,
    *,
    name: str,
    limit: int | None,
    timeout: float,
    opener: Opener = urllib.request.urlopen,
    sleep: Callable[[float], object] = time.sleep,
) -> str:
    """Stream request's body to the new file destination; return its SHA-256.

    name labels the download in progress messages. Raises TooLarge or Failed,
    and leaves no partial file behind; an existing destination is left alone.
    """
    return _retry(lambda: _save(opener, request, destination, limit, timeout), name, sleep)


def read(
    request: urllib.request.Request,
    *,
    name: str,
    limit: int | None,
    timeout: float,
    opener: Opener = urllib.request.urlopen,
    sleep: Callable[[float], object] = time.sleep,
) -> bytes:
    """Return request's body, with the same retries as save."""
    return _retry(lambda: _read(opener, request, limit, timeout), name, sleep)
