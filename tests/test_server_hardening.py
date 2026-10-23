"""Server hardening: origins, credentials, slow clients, job cleanup and sandbox reporting."""

from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import secrets
import socket
import string
import subprocess
import sys
import threading
import time
import types
import uuid
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")
pytest.importorskip("uvicorn")
pytest.importorskip("psutil")

import h11
import psutil
import uvicorn
from starlette.testclient import TestClient

from platefold import server_jobs, server_sandbox
from platefold.server import (
    SecurityBoundary,
    canonical_origins,
    create_app,
    main,
    token_hashes,
    weak_token,
)
from platefold.server_http import HEAD_BYTES, DeadlineH11Protocol, HeadLimitedConnection
from platefold.server_jobs import JobStore
from platefold.server_models import JobLimits, JobRequest
from platefold.server_sandbox import SandboxObserver, boot_clock, isolated, started

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux server contract")
ORIGIN = "https://testserver"


def credential_file(directory: Path, tokens: dict[str, str]) -> Path:
    path = directory / "tokens.json"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    os.write(fd, json.dumps(tokens).encode())
    os.close(fd)
    return path


@pytest.fixture
def serve(tmp_path, monkeypatch):
    """Run main() with uvicorn replaced; nothing binds a port."""
    captured: dict = {}
    fake = types.ModuleType("uvicorn")
    fake.run = lambda app, **options: captured.update(options, app=app)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "uvicorn", fake)
    tokens = credential_file(tmp_path, {"alice": secrets.token_urlsafe(32)})

    def run(*arguments: str) -> dict | None:
        captured.clear()
        argv = ["--data-dir", str(tmp_path / "jobs"), "--tokens-file", str(tokens), *arguments]
        try:
            main(argv)
        except SystemExit as exc:
            assert exc.code == 2
            return None
        return dict(captured)

    return run


# F6: plain-http origins off loopback and weak credentials.


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.0.2.10:8787",
        "http://platefold.example",
        "http://0.0.0.0:8787",
        "http://[fe80::1]",
    ],
)
def test_plain_http_origin_off_loopback_is_refused_unless_explicit(origin):
    with pytest.raises(ValueError, match="loopback"):
        canonical_origins((origin,))
    assert canonical_origins((origin,), allow_http=True)


@pytest.mark.parametrize(
    "origin",
    ["http://127.0.0.1:8787", "http://127.1.2.3", "http://[::1]:8787", "http://localhost:8787"],
)
def test_plain_http_origin_on_loopback_is_accepted(origin):
    assert canonical_origins((origin,)) == (origin,)


def test_origins_are_compared_as_browsers_serialize_them():
    assert canonical_origins(("HTTPS://PlateFold.Example:443", "http://LOCALHOST:80")) == (
        "https://platefold.example",
        "http://localhost",
    )
    assert canonical_origins(("https://a.example:8443", "https://a.example:8443")) == (
        "https://a.example:8443",
    )
    for invalid in ("https://a.example:99999", "https://a.example/", "https://*.example"):
        with pytest.raises(ValueError):
            canonical_origins((invalid,))


def test_configured_origin_case_and_default_port_still_match_requests(tmp_path):
    credential = secrets.token_urlsafe(32)
    app = create_app(
        tmp_path / "jobs", {"alice": credential}, ("HTTPS://TestServer:443",), browser=""
    )
    with TestClient(app, base_url=ORIGIN) as client:
        client.headers["Authorization"] = "Bearer " + credential
        assert client.get("/api/v1/capabilities", headers={"Origin": ORIGIN}).status_code == 200
        assert client.get("/health/live", headers={"Host": "testserver:443"}).status_code == 400


def test_create_app_refuses_cleartext_origin_unless_explicit(tmp_path):
    tokens = {"alice": secrets.token_urlsafe(32)}
    with pytest.raises(ValueError):
        create_app(tmp_path / "a", tokens, ("http://testserver",), browser="")
    app = create_app(
        tmp_path / "b", tokens, ("http://testserver",), browser="", allow_http_origins=True
    )
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200


def test_serve_requires_https_origin_off_loopback(serve):
    assert serve("--host", "0.0.0.0", "--origin", "http://192.0.2.10:8787") is None
    assert serve("--host", "0.0.0.0", "--origin", "https://platefold.example")
    # The Compose example: container-wide bind, published to host loopback only.
    assert serve("--host", "0.0.0.0", "--origin", "http://127.0.0.1:8787")
    explicit = serve(
        "--host", "0.0.0.0", "--origin", "http://192.0.2.10:8787", "--allow-http-origin"
    )
    assert explicit and explicit["host"] == "0.0.0.0"
    assert serve()  # Loopback default origin.


def test_serve_names_the_refused_setting_without_echoing_values(tmp_path, capsys):
    weak = "changeme" * 4
    tokens = credential_file(tmp_path, {"alice": weak})
    base = ["--data-dir", str(tmp_path / "jobs"), "--tokens-file", str(tokens)]
    with pytest.raises(SystemExit):
        main(base)
    message = capsys.readouterr().err
    assert "repetitive" in message and weak not in message and "changeme" not in message
    tokens.unlink()
    credential_file(tmp_path, {"alice": secrets.token_urlsafe(32)})
    with pytest.raises(SystemExit):
        main([*base, "--host", "0.0.0.0", "--origin", "http://192.0.2.10:8787"])
    assert "--allow-http-origin" in capsys.readouterr().err


def test_serve_pins_bounded_protocol_and_disables_websockets(serve):
    options = serve()
    assert options
    assert options["http"] is DeadlineH11Protocol
    assert options["ws"] == "none"
    assert options["h11_max_incomplete_event_size"] == 16 * 1024
    assert DeadlineH11Protocol.head_bytes == HEAD_BYTES == 16 * 1024
    assert options["limit_concurrency"] == 64


GENERATORS = {
    "hex-128": lambda: secrets.token_hex(16),
    "hex-256": lambda: secrets.token_hex(32),
    "uuid4": lambda: str(uuid.uuid4()),
    "urlsafe-192": lambda: secrets.token_urlsafe(24),
    "urlsafe-256": lambda: secrets.token_urlsafe(32),
    "printable": lambda: "".join(
        secrets.choice(string.ascii_letters + string.digits + string.punctuation) for _ in range(32)
    ),
}


@pytest.mark.parametrize("generator", GENERATORS)
def test_generated_tokens_pass_the_strength_floor(generator):
    # Calibrated on 100,000 samples each; this guards against a regression
    # that would reject ordinary generator output.
    assert not any(weak_token(GENERATORS[generator]()) for _ in range(3000))


@pytest.mark.parametrize(
    "token",
    [
        "a" * 32,
        "x" * 31 + "y",
        "password" * 4,
        "changeme" * 4,
        "Password1234" * 3,
        "0123456789abcdef" * 2,
        "01" * 16,
        "abcdefghijklmnopqrstuvwxyz012345",
        "zyxwvutsrqponmlkjihgfedcba987654",
        secrets.token_urlsafe(12) * 2,
    ],
)
def test_weak_tokens_are_refused(token):
    assert weak_token(token)
    with pytest.raises(ValueError):
        token_hashes({"alice": token})


def test_every_configured_digest_is_compared(monkeypatch):
    from platefold import server

    tokens = {f"user{i}": secrets.token_urlsafe(32) for i in range(5)}
    boundary = SecurityBoundary(None, token_hashes(tokens), (ORIGIN,), frozenset())
    calls = []
    real = server.hmac.compare_digest
    monkeypatch.setattr(server.hmac, "compare_digest", lambda a, b: calls.append(1) or real(a, b))
    assert boundary.owner_for(tokens["user0"]) == "user0"
    assert len(calls) == len(tokens)
    assert boundary.owner_for(secrets.token_urlsafe(32)) is None


def test_non_http_scopes_are_refused_before_routing():
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["type"])

    boundary = SecurityBoundary(app, {}, (ORIGIN,), frozenset())
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "websocket.connect"}

    asyncio.run(boundary({"type": "websocket", "headers": []}, receive, send))
    assert not reached
    assert sent == [{"type": "websocket.close", "code": 1008}]
    asyncio.run(boundary({"type": "lifespan"}, receive, send))
    assert reached == ["lifespan"]


# F4: slow request heads.


class QuickDeadline(DeadlineH11Protocol):
    head_seconds = 0.5


async def slow_app(scope, receive, send):
    if scope["type"] != "http":
        return
    if scope["path"] == "/slow":
        await asyncio.sleep(1.2)  # Longer than the head deadline.
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


@contextmanager
def running_server(limit_concurrency=8):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        slow_app,
        http=QuickDeadline,
        ws="none",
        lifespan="off",
        log_level="critical",
        access_log=False,
        limit_concurrency=limit_concurrency,
        timeout_keep_alive=5,
        h11_max_incomplete_event_size=HEAD_BYTES,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def connect(port):
    connection = socket.create_connection(("127.0.0.1", port), timeout=5)
    connection.settimeout(5)
    return connection


def closed_within(connection, seconds) -> bool:
    started = time.monotonic()
    try:
        while connection.recv(4096):
            pass
    except (ConnectionResetError, TimeoutError):
        return False
    return time.monotonic() - started <= seconds


def request(port, path="/"):
    with connect(port) as connection:
        connection.sendall(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n".encode())
        response = b""
        while chunk := connection.recv(4096):
            response += chunk
    return response.split(b"\r\n", 1)[0]


def test_partial_request_head_is_closed_at_its_deadline():
    with running_server() as port, connect(port) as connection:
        connection.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
        assert closed_within(connection, 3)


def test_trickled_head_after_a_kept_alive_response_is_closed():
    with running_server() as port, connect(port) as connection:
        connection.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
        assert b"200" in connection.recv(4096)
        connection.setblocking(False)
        started = time.monotonic()
        # Each byte cancels uvicorn's keep-alive timer; the head deadline holds.
        while time.monotonic() - started < 4:
            try:
                if connection.recv(1) == b"":
                    break
            except BlockingIOError:
                pass
            except ConnectionResetError:
                break
            try:
                connection.sendall(b"X")
            except (BrokenPipeError, ConnectionResetError):
                break
            time.sleep(0.1)
        assert time.monotonic() - started < 3


def test_unread_body_after_response_is_closed():
    with running_server() as port, connect(port) as connection:
        connection.sendall(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 1000000\r\n\r\nabc")
        assert b"200" in connection.recv(4096)
        assert closed_within(connection, 3)


def test_request_in_progress_outlives_the_head_deadline():
    with running_server() as port:
        assert b"200" in request(port, "/slow")


def test_slow_heads_no_longer_hold_every_slot():
    with running_server(limit_concurrency=8) as port:
        holders = [connect(port) for _ in range(12)]
        try:
            for holder in holders:
                holder.sendall(b"GET / HTTP/1.1\r\nHost: x\r\n")
            time.sleep(0.2)
            assert b"503" in request(port)  # Every slot is held, as before the fix.
            time.sleep(1.0)
            assert b"200" in request(port)  # The deadline released them.
        finally:
            for holder in holders:
                holder.close()


# Request head size: the documented limit is 16 KiB for the complete head.


def padded_head(
    total: int, fields: int = 1, *, close: bool = True, method: bytes = b"GET"
) -> bytes:
    """A request head of exactly ``total`` bytes, padded across ``fields`` headers."""
    start = method + b" / HTTP/1.1\r\nHost: x\r\n" + (b"Connection: close\r\n" if close else b"")
    if method == b"POST":
        start += b"Content-Length: 3\r\n"
    names = [b"X-Pad-%03d: " % index for index in range(fields)]
    room = total - len(start) - 2 - sum(len(name) + 2 for name in names)
    assert room >= fields
    sizes = [room // fields + (index < room % fields) for index in range(fields)]
    head = (
        start
        + b"".join(n + b"a" * size + b"\r\n" for n, size in zip(names, sizes, strict=True))
        + b"\r\n"
    )
    assert len(head) == total
    return head


def exchange(port, *writes: bytes) -> bytes:
    """Send each write separately, then read until the server closes."""
    with connect(port) as connection:
        for index, data in enumerate(writes):
            if index:
                time.sleep(0.2)  # Let the server read the previous write on its own.
            connection.sendall(data)
        received = b""
        try:
            while chunk := connection.recv(65536):
                received += chunk
        except ConnectionResetError:
            # Rejecting an oversized head can close with unread request bytes.
            # Accept a reset only after the complete expected rejection arrived.
            if (
                statuses(received)[-1:] != [431]
                or b"\r\n\r\n" not in received
                or not received.endswith(b"Request head exceeds 16 KiB")
            ):
                raise
    return received


def statuses(received: bytes) -> list[int]:
    return [int(code) for code in re.findall(rb"^HTTP/1\.1 (\d{3}) ", received, re.MULTILINE)]


@pytest.mark.parametrize(
    "response,complete",
    [
        (
            b"HTTP/1.1 431 Request Header Fields Too Large\r\nConnection: close\r\n\r\n"
            b"Request head exceeds 16 KiB",
            True,
        ),
        (b"", False),
        (b"HTTP/1.1 431 Request Header Fields Too Large\r\n", False),
        (b"HTTP/1.1 431 Request Header Fields Too Large\r\nRequest head exceeds 16 KiB", False),
        (b"HTTP/1.1 431 Request Header Fields Too Large\r\n\r\nRequest head exceeds", False),
        (b"HTTP/1.1 200 OK\r\n\r\nRequest head exceeds 16 KiB", False),
    ],
)
def test_exchange_reset_requires_complete_head_rejection(monkeypatch, response, complete):
    connection = MagicMock()
    connection.__enter__.return_value = connection
    connection.recv.side_effect = (
        [response, ConnectionResetError()] if response else [ConnectionResetError()]
    )
    monkeypatch.setattr(sys.modules[__name__], "connect", lambda _port: connection)
    if complete:
        received = exchange(0, b"request")
        assert received == response
    else:
        with pytest.raises(ConnectionResetError):
            exchange(0, b"request")


@pytest.mark.parametrize("fields", [1, 160], ids=["one-header", "many-headers"])
@pytest.mark.parametrize(
    "size,status",
    [(HEAD_BYTES - 1, 200), (HEAD_BYTES, 200), (HEAD_BYTES + 1, 431), (60_000, 431)],
)
def test_request_head_limit_applies_to_a_head_sent_in_one_write(fields, size, status):
    with running_server() as port:
        received = exchange(port, padded_head(size, fields))
    assert statuses(received) == [status]
    if status == 431:
        assert b"connection: close" in received.lower()
        assert received.endswith(b"Request head exceeds 16 KiB")


@pytest.mark.parametrize("size,status", [(HEAD_BYTES, 200), (HEAD_BYTES + 1, 431), (40_000, 431)])
def test_request_head_limit_applies_to_a_head_split_across_writes(size, status):
    head = padded_head(size, 40)
    with running_server() as port:
        assert statuses(exchange(port, head[:10_000], head[10_000:])) == [status]


def test_incomplete_request_head_over_the_limit_is_refused_without_waiting():
    head = padded_head(40_000, 40)
    with running_server() as port:
        assert statuses(exchange(port, head[: HEAD_BYTES + 100])) == [431]


def test_request_head_limit_applies_to_each_kept_alive_request():
    first = padded_head(HEAD_BYTES, 40, close=False)
    with running_server() as port:
        pipelined = exchange(port, first + padded_head(HEAD_BYTES + 1, 40))
        kept_alive = exchange(port, first, padded_head(HEAD_BYTES + 1, 40))
        allowed = exchange(port, first, padded_head(HEAD_BYTES, 40))
    assert statuses(pipelined) == [200, 431]
    assert statuses(kept_alive) == [200, 431]
    assert statuses(allowed) == [200, 200]


def test_request_head_limit_applies_after_an_early_response_and_unread_body():
    # The handler answers without reading the body; the next head then parses in
    # the same pass that finishes the body.
    post = padded_head(1000, close=False, method=b"POST") + b"abc"
    with running_server() as port:
        assert statuses(exchange(port, post + padded_head(HEAD_BYTES + 1, 40))) == [200, 431]
        assert statuses(exchange(port, post + padded_head(HEAD_BYTES, 40))) == [200, 200]


def test_large_bodies_are_not_counted_as_request_head():
    body = b"b" * 200_000
    head = b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n" % len(body)
    with running_server() as port:
        received = exchange(port, head + body + padded_head(1000))
    assert statuses(received) == [200, 200]


@pytest.mark.parametrize("ending", [b"\r\n", b"\n"], ids=["crlf", "lf"])
def test_head_limit_counts_every_byte_through_the_closing_blank_line(ending):
    def head(total):
        start = b"GET / HTTP/1.1" + ending + b"Host: x" + ending + b"X-Pad: "
        return start + b"a" * (total - len(start) - 2 * len(ending)) + ending + ending

    for size, fits in ((HEAD_BYTES, True), (HEAD_BYTES + 1, False)):
        connection = HeadLimitedConnection(HEAD_BYTES, HEAD_BYTES)
        data = head(size)
        assert len(data) == size
        connection.receive_data(data + b"GET / HTTP/1.1\r\n")
        if fits:
            assert isinstance(connection.next_event(), h11.Request)
        else:
            with pytest.raises(h11.RemoteProtocolError) as refused:
                connection.next_event()
            assert refused.value.error_status_hint == 431
            assert connection.head_too_large


# U2: retention cleanup.


@pytest.fixture
def store(tmp_path):
    store = JobStore(tmp_path / "jobs", JobLimits(), "", {})
    store.open()
    yield store
    if store.lock_fd is not None:
        os.close(store.lock_fd)


def finished_job(store):
    job = store.admit(
        "alice", JobRequest.model_validate({"source": {"kind": "upload", "format": "markdown"}})
    )
    store.transition(job, "cancelled")
    job.finished_at = time.time() - store.limits.retention_seconds - 1
    return job


def test_failed_expiry_keeps_the_job_and_reports_unhealthy(store, monkeypatch):
    job = finished_job(store)

    def denied(*_args, **_kwargs):
        raise OSError(errno.EACCES, "synthetic cleanup failure")

    monkeypatch.setattr(server_jobs.shutil, "rmtree", denied)
    store.expire()  # Must not raise into admission or the sweeper.
    assert job.id in store.jobs
    assert not store.storage_healthy
    monkeypatch.undo()
    store.expire()
    assert job.id not in store.jobs
    assert not store.path(job).exists()


def test_expiry_does_not_follow_links_out_of_the_job_root(store, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("must survive")
    job = finished_job(store)
    (store.path(job) / "results").symlink_to(outside, target_is_directory=True)
    store.expire()
    assert not store.path(job).exists()
    assert (outside / "keep.txt").read_text() == "must survive"

    swapped = finished_job(store)
    directory = store.path(swapped)
    directory.rename(tmp_path / "moved")
    directory.symlink_to(outside, target_is_directory=True)
    store.expire()
    assert not directory.exists() and not directory.is_symlink()
    assert swapped.id not in store.jobs
    assert (outside / "keep.txt").read_text() == "must survive"
    assert store.storage_healthy


def test_remove_tree_refuses_paths_outside_a_job(store, tmp_path):
    for path in (
        store.root,
        tmp_path,
        store.root / "not-a-job",
        store.root / ("a" * 32) / "x" / "y",
    ):
        with pytest.raises(ValueError):
            store.remove_tree(path)
    assert store.remove_tree(store.root / ("b" * 32))  # Already absent is success.


def test_admission_survives_an_expiry_failure(tmp_path, monkeypatch):
    credential = secrets.token_urlsafe(32)
    app = create_app(tmp_path / "jobs", {"alice": credential}, (ORIGIN,), browser=sys.executable)
    with TestClient(app, base_url=ORIGIN) as client:
        client.headers["Authorization"] = "Bearer " + credential
        body = {"source": {"kind": "upload", "format": "markdown"}}
        url = client.post("/api/v1/jobs", json=body).json()["links"]["status"]
        client.post(url + "/cancel")
        job = next(iter(app.state.jobs.jobs.values()))
        job.finished_at = time.time() - app.state.jobs.limits.retention_seconds - 1

        def denied(*_args, **_kwargs):
            raise OSError(errno.EACCES, "synthetic cleanup failure")

        monkeypatch.setattr(server_jobs.shutil, "rmtree", denied)
        response = client.post("/api/v1/jobs", json=body)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "storage_unavailable"


# U4: the renderer sandbox is observed, not asserted.

SANDBOXED = "Name:\tchrome\nNSpid:\t2044997\t12\t1\nNoNewPrivs:\t1\nSeccomp:\t2\n"


def test_status_parser_requires_both_sandbox_layers():
    assert isolated(SANDBOXED)
    assert not isolated(SANDBOXED.replace("Seccomp:\t2", "Seccomp:\t0"))
    assert not isolated(SANDBOXED.replace("2044997\t12\t1", "2044997"))
    assert not isolated("")


@contextmanager
def fake_renderer():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)", "--type=renderer"]
    )
    try:
        renderer = psutil.Process(process.pid)
        # Until the child has exec'd, its command line is still the parent's;
        # the tests below want it seen as a renderer on the first look.
        deadline = time.monotonic() + 10
        while "--type=renderer" not in renderer.cmdline():
            assert time.monotonic() < deadline, "fake renderer did not start"
            time.sleep(0.01)
        yield renderer
    finally:
        process.kill()
        process.wait()


def test_observer_reports_an_unsandboxed_renderer_once_settled():
    with fake_renderer() as renderer:
        observer = SandboxObserver()
        observer.observe([renderer], now=started(renderer.pid))
        assert observer.state == "unknown"  # Too young to judge.
        observer.observe([renderer], now=started(renderer.pid) + 2)
        assert observer.state == "disabled"


def test_observer_reports_enabled_and_ignores_other_processes(monkeypatch):
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        with fake_renderer() as renderer:
            observer = SandboxObserver()
            observer.observe([psutil.Process(other.pid)], now=boot_clock() + 5)
            assert observer.state == "unknown"
            observer.observe([renderer], now=started(renderer.pid) + 2)
            assert observer.state == "disabled"
            # A filter applied late still counts; it cannot be removed afterwards.
            monkeypatch.setattr(server_sandbox, "isolated", lambda _status: True)
            observer.observe([renderer], now=started(renderer.pid) + 3)
            assert observer.state == "enabled"
    finally:
        other.kill()
        other.wait()


def test_capabilities_report_the_observed_sandbox_state(tmp_path):
    credential = secrets.token_urlsafe(32)
    app = create_app(tmp_path / "jobs", {"alice": credential}, (ORIGIN,), browser=sys.executable)
    store = app.state.jobs
    with TestClient(app, base_url=ORIGIN) as client:
        client.headers["Authorization"] = "Bearer " + credential

        def engine():
            return client.get("/api/v1/capabilities").json()["pdf_engine"]

        assert engine()["sandbox"] == "unverified" and engine()["sandbox_disabled"] is None
        url = client.post(
            "/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}}
        ).json()["links"]["status"]
        job = store.jobs[url.rsplit("/", 1)[1]]

        class Stream:
            def __init__(self, *events):
                self.lines = [json.dumps(e).encode() + b"\n" for e in events]

            async def readline(self):
                return self.lines.pop(0) if self.lines else b""

        def read(*events):
            process = types.SimpleNamespace(stdout=Stream(*events))
            return asyncio.run(store.read_events(job, process))

        read({"event": "exit", "code": None, "sandbox": "disabled"})
        assert engine()["sandbox"] == "disabled" and engine()["sandbox_disabled"] is True
        read({"event": "exit", "code": None, "sandbox": "unknown"})
        assert engine()["sandbox"] == "disabled"  # No evidence does not clear evidence.
        read({"event": "exit", "code": None, "sandbox": "claimed-safe"})
        assert job.sandbox == "unknown"
        read({"event": "exit", "code": None, "sandbox": "enabled"})
        assert engine()["sandbox"] == "enabled" and engine()["sandbox_disabled"] is False
        store.transition(job, "cancelled")

    resumed = JobStore(tmp_path / "jobs", JobLimits(), "", {})
    resumed.open()
    try:
        assert resumed.sandbox == "enabled"
    finally:
        os.close(resumed.lock_fd)


@pytest.mark.integration
def test_real_export_through_a_no_sandbox_wrapper_is_reported(tmp_path):
    from platefold.browser import find_bundled

    chromium = find_bundled()
    if not chromium:
        pytest.skip("bundled Chromium is unavailable")
    # A distribution wrapper or administrator policy can add flags that
    # Plate|Fold never passes itself.
    wrapper = tmp_path / "chromium-wrapper"
    wrapper.write_text(f'#!/bin/sh\nexec "{chromium}" --no-sandbox "$@"\n')
    wrapper.chmod(0o700)
    credential = secrets.token_urlsafe(32)
    app = create_app(tmp_path / "jobs", {"alice": credential}, (ORIGIN,), browser=str(wrapper))
    with TestClient(app, base_url=ORIGIN) as client:
        client.headers["Authorization"] = "Bearer " + credential
        url = client.post(
            "/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}}
        ).json()["links"]["status"]
        response = client.put(
            url + "/source",
            content=b"# Wrapper\n\n" + b"Text. " * 2000,
            headers={"Content-Type": "text/markdown"},
        )
        assert response.status_code == 202
        deadline = time.monotonic() + 60
        while client.get(url).json()["state"] not in {
            "completed",
            "completed_with_warnings",
            "failed",
        }:
            assert time.monotonic() < deadline
            time.sleep(0.1)
        engine = client.get("/api/v1/capabilities").json()["pdf_engine"]
        assert engine["sandbox"] == "disabled" and engine["sandbox_disabled"] is True
