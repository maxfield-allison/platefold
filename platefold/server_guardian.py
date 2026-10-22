"""Linux export guardian, independent of the HTTP event loop.

stdin is a liveness lease from the server: EOF/cancel stops the render tree.
The inherited store lock is held until cleanup finishes, including after a
server crash. No PID from a previous server incarnation is ever signalled.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

from platefold.server_models import JobLimits
from platefold.server_sandbox import SandboxObserver

POLL_SECONDS = 0.1


def prctl(option: int, value: int) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(option, value, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "Linux process containment unavailable")


def emit(**event) -> None:
    with contextlib.suppress(BrokenPipeError):
        print(json.dumps(event), flush=True)


def tree_usage(children: list[psutil.Process]) -> tuple[int, int]:
    memory = count = 0
    for child in children:
        with contextlib.suppress(psutil.NoSuchProcess):
            memory += child.memory_info().rss
            count += 1
    return memory, count


def disk_usage(path: Path, limits: JobLimits) -> tuple[int, int]:
    size = count = 0
    for directory, _, files in os.walk(path, followlinks=False):
        for name in files:
            with contextlib.suppress(FileNotFoundError):
                info = os.lstat(Path(directory, name))
                size += info.st_size
                count += 1
            if size > limits.scratch_bytes or count > limits.scratch_files:
                return size, count
    return size, count


def terminate_tree(process: subprocess.Popen, parent: psutil.Process) -> None:
    """Reap both the process group and adopted/detached descendants."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    terminate_descendants(parent)
    with contextlib.suppress(ChildProcessError):
        process.wait(timeout=2)


def terminate_descendants(parent: psutil.Process) -> None:
    # PR_SET_CHILD_SUBREAPER makes orphaned Chromium grandchildren ours to reap.
    # Repeat after killing parents so detached, newly adopted children are included.
    for _ in range(3):
        children = parent.children(recursive=True)
        for child in children:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
        psutil.wait_procs(children, timeout=1)
        with contextlib.suppress(ChildProcessError):
            while os.waitpid(-1, os.WNOHANG)[0] > 0:
                pass


def monitor(
    process,
    root: Path,
    limits: JobLimits,
    parent,
    selector,
    sandbox: SandboxObserver,
    deadline: float,
) -> str | None:
    """Watch the render tree until it exits, breaks a limit or loses its lease.

    ``deadline`` is a ``time.monotonic()`` value set by the server when the job
    started running.
    """
    while process.poll() is None:
        if selector.select(POLL_SECONDS):
            # Both explicit cancellation and EOF revoke the lease.
            os.read(sys.stdin.fileno(), 1)
            return "cancelled"
        if time.monotonic() >= deadline:
            return "deadline_exceeded"
        children = parent.children(recursive=True)
        sandbox.observe(children)
        memory, count = tree_usage(children)
        size, files = disk_usage(root, limits)
        if (
            memory > limits.memory_bytes
            or count > limits.processes
            or size > limits.scratch_bytes
            or files > limits.scratch_files
        ):
            return "resource_limit"
    return None if process.returncode == 0 else "worker_failed"


def main() -> int:
    root = Path(sys.argv[1])
    config = json.loads((root / "request.json").read_text())
    limits = JobLimits(**config["limits"])
    # The server's deadline, never later than a clock started now would allow.
    deadline = min(float(sys.argv[3]), time.monotonic() + limits.export_seconds)
    if time.monotonic() >= deadline:
        # Start-up used the whole budget; do not start a renderer at all.
        emit(event="exit", code="deadline_exceeded", sandbox=SandboxObserver().state)
        return 0
    prctl(36, 1)  # PR_SET_CHILD_SUBREAPER
    parent = psutil.Process()
    with selectors.DefaultSelector() as selector:
        selector.register(sys.stdin, selectors.EVENT_READ)
        # Child output goes straight to the server's bounded NDJSON reader.
        process = subprocess.Popen(
            [sys.executable, "-m", "platefold.server_render", str(root), str(os.getpid())],
            stdin=subprocess.DEVNULL,
            pass_fds=(int(sys.argv[2]),),
            start_new_session=True,
            close_fds=True,
        )
        sandbox = SandboxObserver()
        try:
            code = monitor(process, root, limits, parent, selector, sandbox, deadline)
        finally:
            terminate_tree(process, parent)
        emit(event="exit", code=code, sandbox=sandbox.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
