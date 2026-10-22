"""Exercise actual process trees, disconnects, deadlines and server crashes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, replace

import pytest

pytest.importorskip("psutil")
pytest.importorskip("httpx")
pytest.importorskip("starlette")

import httpx
import psutil
from starlette.testclient import TestClient

from platefold.server import create_app
from platefold.server_models import TERMINAL, JobLimits

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux process containment")


# After the deadline the guardian notices within one 0.1 s poll, kills and reaps
# the tree, and the server records the failure. Measured from the job's
# "running" event, that took 0.25-0.33 s on an idle host and at most 1.0 s with
# one CPU shared among three busy processes.
DEADLINE_CLEANUP_SECONDS = 2


def eventually(check, *, seconds=12):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.05)
    pytest.fail("Timed out waiting for process state")


def fake_browser(tmp_path):
    """A real executable with a detached grandchild and no DevTools endpoint."""
    marker = tmp_path / "browser-started.json"
    # Readers use existence as the readiness signal; publish only complete JSON.
    pending = marker.with_suffix(".tmp")
    executable = tmp_path / "test-browser"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, resource, subprocess, sys, time\n"
        "from pathlib import Path\n"
        # The grandchild names the test directory so job_processes() can find it.
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)', "
        f"{str(tmp_path)!r}], start_new_session=True)\n"
        f"Path({str(pending)!r}).write_text(json.dumps({{"
        "'pids': [os.getpid(), child.pid], "
        "'cpu_limit': resource.getrlimit(resource.RLIMIT_CPU)[1], "
        "'file_limit': resource.getrlimit(resource.RLIMIT_FSIZE)[1], "
        "'core_limit': resource.getrlimit(resource.RLIMIT_CORE)[1], "
        "'fd_limit': resource.getrlimit(resource.RLIMIT_NOFILE)[1], "
        "'credential_inherited': 'PLATEFOLD_API_TOKENS' in os.environ, "
        "'preload_inherited': 'PYTHONSTARTUP' in os.environ}))\n"
        f"Path({str(pending)!r}).replace({str(marker)!r})\n"
        "time.sleep(300)\n"
    )
    executable.chmod(0o700)
    return executable, marker


def all_dead(pids):
    return all(not psutil.pid_exists(pid) for pid in pids)


def job_processes(tmp_path):
    """Live processes whose command line names this test's private directory.

    The guardian and renderer take the job's work path as an argument and the
    fake browser and its grandchild carry the test directory, so this finds every
    process an export started, including ones that never wrote the marker.
    """
    root = str(tmp_path)
    found = []
    for process in psutil.process_iter(["cmdline", "status"]):
        if process.info["status"] == psutil.STATUS_ZOMBIE:
            continue
        arguments = process.info["cmdline"] or ()
        if any(a == root or a.startswith(root + os.sep) for a in arguments):
            found.append(process.pid)
    return found


def terminal_state(client, url):
    state = client.get(url).json()
    return state if state["state"] in TERMINAL else None


def check_browser_evidence(marker, limits):
    evidence = json.loads(marker.read_text())
    assert not evidence["credential_inherited"]
    assert not evidence["preload_inherited"]
    assert evidence["cpu_limit"] <= limits.cpu_seconds
    assert evidence["file_limit"] <= limits.output_bytes
    assert evidence["fd_limit"] <= 512
    assert evidence["core_limit"] == 0
    return evidence["pids"]


def submit(client):
    response = client.post(
        "/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}}
    )
    assert response.status_code == 201
    url = response.json()["links"]["status"]
    assert (
        client.put(
            url + "/source", content=b"# Tree cleanup", headers={"Content-Type": "text/markdown"}
        ).status_code
        == 202
    )
    return url


@pytest.mark.parametrize("action", ["cancel", "deadline", "shutdown", "guardian_crash"])
def test_process_tree_termination(tmp_path, monkeypatch, action):
    executable, marker = fake_browser(tmp_path)
    tokens = {"alice": secrets.token_urlsafe(32)}
    monkeypatch.setenv("PLATEFOLD_API_TOKENS", "SYNTHETIC_SECRET_SENTINEL")
    monkeypatch.setenv("PYTHONSTARTUP", "/synthetic-not-a-file")
    limits = replace(JobLimits(), export_seconds=3 if action == "deadline" else 20)
    app = create_app(
        tmp_path / "jobs", tokens, ("https://testserver",), limits=limits, browser=str(executable)
    )
    pids = []
    try:
        with TestClient(app, base_url="https://testserver") as client:
            client.headers["Authorization"] = "Bearer " + tokens["alice"]
            url = submit(client)
            if action == "deadline":
                # The deadline bounds the whole export, renderer start-up included.
                # On a busy host it can expire before the browser has started, so
                # waiting for the browser first would race the behaviour under test.
                state = eventually(
                    lambda: terminal_state(client, url), seconds=limits.export_seconds + 12
                )
                assert state["state"] == "failed"
                assert state["error"]["code"] == "deadline_exceeded"
                times = {
                    event["state"]: event["time"]
                    for event in client.get(url + "/events").json()["events"]
                    if event["event"] == "state"
                }
                # export_seconds counts from the moment the job starts running:
                # the export was stopped by its deadline, not before and not
                # much after it.
                elapsed = times["failed"] - times["running"]
                assert limits.export_seconds <= elapsed
                assert elapsed <= limits.export_seconds + DEADLINE_CLEANUP_SECONDS
                if marker.exists():
                    pids = check_browser_evidence(marker, limits)
            else:
                eventually(marker.exists)
                pids = check_browser_evidence(marker, limits)
                if action == "cancel":
                    assert client.post(url + "/cancel").status_code in {200, 202}
                elif action == "guardian_crash":
                    process = next(iter(app.state.jobs.processes.values()))
                    # The renderer's parent-death signal must kill its process group.
                    os.kill(process.pid, signal.SIGKILL)
                if action != "shutdown":
                    state = eventually(lambda: terminal_state(client, url))
                    assert state["state"] == ("cancelled" if action == "cancel" else "failed")
        eventually(lambda: all_dead(pids))
        # Nothing the export started survives, whether or not the browser got going.
        eventually(lambda: not job_processes(tmp_path))
        assert not list((tmp_path / "jobs").glob("*/work"))
    finally:
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_deadline_stops_a_running_tree_including_detached_descendants(tmp_path):
    """The guardian's deadline and tree kill, with the browser already running.

    The server test above cannot choose whether its deadline lands before or
    after the browser starts. Here the clock starts only once the fake browser
    and its detached grandchild exist, so both are always what the deadline stops.
    """
    executable, marker = fake_browser(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    harness = tmp_path / "harness.py"
    harness.write_text(
        "import json, selectors, subprocess, sys, time\n"
        "from pathlib import Path\n"
        "import psutil\n"
        "from platefold.server_guardian import monitor, prctl, terminate_tree\n"
        "from platefold.server_models import JobLimits\n"
        "from platefold.server_sandbox import SandboxObserver\n"
        "prctl(36, 1)  # PR_SET_CHILD_SUBREAPER, as the guardian sets it\n"
        f"marker = Path({str(marker)!r})\n"
        # Keep the tree off our pipes so a survivor cannot hold the result back.
        f"process = subprocess.Popen([{str(executable)!r}], start_new_session=True, "
        "stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "ready = time.monotonic() + 60\n"
        "while not marker.exists() and time.monotonic() < ready:\n"
        "    time.sleep(0.05)\n"
        "limits = JobLimits(export_seconds=1)\n"
        "with selectors.DefaultSelector() as selector:\n"
        "    selector.register(sys.stdin, selectors.EVENT_READ)\n"
        "    started = time.monotonic()\n"
        "    code = monitor(\n"
        f"        process, Path({str(scratch)!r}), limits, psutil.Process(), selector,\n"
        "        SandboxObserver(), started + limits.export_seconds,\n"
        "    )\n"
        "    elapsed = time.monotonic() - started\n"
        "    terminate_tree(process, psutil.Process())\n"
        "    # As subreaper, anything the tree left behind is still our descendant.\n"
        "    left = [child.pid for child in psutil.Process().children(recursive=True)]\n"
        "print(json.dumps({'code': code, 'elapsed': elapsed, 'left': left}))\n"
    )
    # An open, silent stdin is the guardian's lease; EOF would mean "cancelled".
    lease, held = os.pipe()
    pids = []
    try:
        guardian = subprocess.run(
            [sys.executable, str(harness)],
            stdin=lease,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
        pids = json.loads(marker.read_text())["pids"]
        outcome = json.loads(guardian.stdout)
        assert outcome["code"] == "deadline_exceeded"
        assert outcome["elapsed"] >= 1
        # Nothing survived the kill, not even as an unreaped zombie.
        assert outcome["left"] == []
        eventually(lambda: all_dead(pids))
        assert not job_processes(tmp_path)
    finally:
        os.close(lease)
        os.close(held)
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)


def test_guardian_started_after_the_deadline_starts_no_renderer(tmp_path):
    """A guardian whose start-up used up the whole budget reports the deadline at once."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "request.json").write_text(json.dumps({"limits": asdict(JobLimits())}))
    # The lock descriptor named here is not open in the guardian, so starting a
    # renderer (which passes it on) would crash the guardian. A clean deadline
    # report therefore shows that no renderer was started.
    closed = os.open(tmp_path / "lock", os.O_CREAT | os.O_RDWR, 0o600)
    os.close(closed)
    guardian = subprocess.run(
        [
            sys.executable,
            "-m",
            "platefold.server_guardian",
            str(work),
            str(closed),
            repr(time.monotonic() - 1),
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    events = [json.loads(line) for line in guardian.stdout.splitlines()]
    assert [(e["event"], e["code"]) for e in events] == [("exit", "deadline_exceeded")]


@pytest.mark.parametrize(
    "limits",
    [
        replace(JobLimits(), memory_bytes=1),
        replace(JobLimits(), processes=1),
        replace(JobLimits(), scratch_bytes=1),
    ],
)
def test_process_resource_overflow_is_terminal_and_cleans(tmp_path, limits):
    executable, _marker = fake_browser(tmp_path)
    tokens = {"alice": secrets.token_urlsafe(32)}
    app = create_app(
        tmp_path / "jobs", tokens, ("https://testserver",), limits=limits, browser=str(executable)
    )
    with TestClient(app, base_url="https://testserver") as client:
        client.headers["Authorization"] = "Bearer " + tokens["alice"]
        url = submit(client)
        state = eventually(
            lambda: s if (s := client.get(url).json())["state"] in TERMINAL else None
        )
        assert state["state"] == "failed"
        assert state["error"]["code"] == "resource_limit"
        assert not list((tmp_path / "jobs").glob("*/work"))


def test_queue_limit_and_queued_cancellation_do_not_start_browser(tmp_path):
    executable, marker = fake_browser(tmp_path)
    tokens = {"alice": secrets.token_urlsafe(32)}
    app = create_app(
        tmp_path / "jobs",
        tokens,
        ("https://testserver",),
        limits=replace(JobLimits(), concurrent_jobs=1),
        browser=str(executable),
    )
    with TestClient(app, base_url="https://testserver") as client:
        client.headers["Authorization"] = "Bearer " + tokens["alice"]
        first = submit(client)
        eventually(marker.exists)
        second = submit(client)
        assert client.get(second).json()["state"] == "queued"
        assert len(app.state.jobs.processes) == 1
        assert client.post(second + "/cancel").json()["state"] == "cancelled"
        client.post(first + "/cancel")
        eventually(lambda: not app.state.jobs.tasks)
        assert client.get(second).json()["state"] == "cancelled"
        assert len(json.loads(marker.read_text())["pids"]) == 2


def test_upload_disconnect_and_slow_body_cleanup(tmp_path):
    async def scenario():
        tokens = {"alice": secrets.token_urlsafe(32)}
        app = create_app(
            tmp_path / "jobs",
            tokens,
            ("https://testserver",),
            limits=replace(JobLimits(), upload_seconds=1),
            browser=sys.executable,
        )
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
            async with httpx.AsyncClient(
                transport=transport,
                base_url="https://testserver",
                headers={"Authorization": "Bearer " + tokens["alice"]},
            ) as client:
                for slow in (False, True):
                    response = await client.post(
                        "/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}}
                    )
                    url = response.json()["links"]["status"]

                    async def chunks(slow=slow):
                        yield b"# Partial"
                        if slow:
                            await asyncio.sleep(3)
                        else:
                            raise ConnectionError("synthetic disconnect")

                    response = await client.put(
                        url + "/source", content=chunks(), headers={"Content-Type": "text/markdown"}
                    )
                    assert response.status_code == (408 if slow else 500)
                    assert (await client.get(url)).json()["state"] == "failed"
                    assert not list((tmp_path / "jobs").glob("*/work"))

    asyncio.run(scenario())


def test_abrupt_server_loss_kills_descendants_and_restarts_interrupted(tmp_path):
    executable, marker = fake_browser(tmp_path)
    credential = secrets.token_urlsafe(32)
    token_file = tmp_path / "tokens.json"
    token_file.write_text(json.dumps({"alice": credential}))
    token_file.chmod(0o600)
    root = tmp_path / "jobs"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    launcher = tmp_path / "launch.py"
    launcher.write_text(
        "from platefold import server\n"
        f"server.discover_browser = lambda: {str(executable)!r}\n"
        f"server.main(['--data-dir', {str(root)!r}, '--tokens-file', {str(token_file)!r}, '--port', '{port}'])\n"
    )
    server = subprocess.Popen(
        [sys.executable, str(launcher)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    pids = []
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            headers={"Authorization": "Bearer " + credential},
            timeout=2,
        ) as client:

            def ready():
                try:
                    return client.get("/health/live").status_code == 200
                except httpx.TransportError:
                    return False

            eventually(ready)
            url = submit(client)
            eventually(marker.exists)
            pids = json.loads(marker.read_text())["pids"]
            server.kill()
            server.wait(timeout=5)
            eventually(lambda: all_dead(pids))

        # Reopening is only possible once the guardian releases its inherited lock.
        def released():
            import fcntl

            with (root / ".lock").open("rb") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return True
                except BlockingIOError:
                    return False

        eventually(released)
        app = create_app(root, {"alice": credential}, ("https://testserver",))
        with TestClient(app, base_url="https://testserver") as client:
            client.headers["Authorization"] = "Bearer " + credential
            state = client.get(url).json()
            assert state["state"] == "interrupted"
            assert state["error"]["code"] == "server_restarted"
            assert not list(root.glob("*/work"))
    finally:
        if server.poll() is None:
            server.kill()
            server.wait(timeout=5)
        token_file.unlink(missing_ok=True)
        for pid in pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
