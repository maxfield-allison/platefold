"""Single-replica job admission, durable state, process leases and retention."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
import stat
import sys
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from platefold.limits import MIB
from platefold.server_models import TERMINAL, APIError, JobLimits, JobRequest, Mount, MountSource
from platefold.server_sandbox import STATES as SANDBOX_STATES
from platefold.server_sources import parts_for

MAX_EVENT_BYTES = 64 * 1024
MAX_EVENTS = 128


def atomic_json(path: Path, value) -> None:
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class Job:
    id: str
    owner: str
    state: str
    created_at: float
    updated_at: float
    request: dict
    finished_at: float | None = None
    error: str | None = None
    results: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    sequence: int = 0
    # Renderer sandbox state the export guardian observed; None before a render.
    sandbox: str | None = None

    def public(self, limits: JobLimits) -> dict:
        return {
            "schema_version": 1,
            "id": self.id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": (
                self.finished_at + limits.retention_seconds if self.finished_at else None
            ),
            "error": {"code": self.error} if self.error else None,
            "last_event": self.sequence,
            "links": {
                key: f"/api/v1/jobs/{self.id}{suffix}"
                for key, suffix in {
                    "status": "",
                    "source": "/source",
                    "events": "/events",
                    "results": "/results",
                    "cancel": "/cancel",
                }.items()
            },
        }


class JobStore:
    def __init__(self, root: Path, limits: JobLimits, browser: str, mounts: dict[str, Mount]):
        self.root, self.limits, self.browser, self.mounts = root, limits, browser, mounts
        self.jobs: dict[str, Job] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.processes: dict[str, asyncio.subprocess.Process] = {}
        self.uploads: set[str] = set()
        self.downloads: dict[str, int] = {}
        self.lock_fd: int | None = None
        self.sweeper: asyncio.Task | None = None
        self.closing = False
        self.storage_healthy = True
        # Renderer sandbox state from the most recent export that observed one.
        self.sandbox: str | None = None

    def path(self, job: Job) -> Path:
        return self.root / job.id

    def record(self, job: Job, event: str, **payload) -> None:
        job.sequence += 1
        job.updated_at = time.time()
        job.events.append(
            {
                "sequence": job.sequence,
                "event": event,
                "time": job.updated_at,
                "state": job.state,
                **payload,
            }
        )
        job.events = job.events[-MAX_EVENTS:]
        atomic_json(self.path(job) / "state.json", asdict(job))

    def transition(self, job: Job, state: str, error: str | None = None) -> None:
        job.state, job.error = state, error
        if state in TERMINAL:
            job.finished_at = time.time()
        self.record(job, "state")

    def open(self) -> None:
        if not sys.platform.startswith("linux"):
            raise ValueError("The HTTP server requires Linux")
        if not shutil.rmtree.avoids_symlink_attacks:
            raise ValueError("Job cleanup requires descriptor-based directory removal")
        import fcntl

        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.geteuid()
            or info.st_mode & 0o077
            or self.root.resolve() != self.root
        ):
            raise ValueError("Job storage must be an owned, private, unlinked absolute directory")
        fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ValueError(
                "Job storage is already in use; run exactly one server replica"
            ) from None
        self.lock_fd = fd
        try:
            self.recover()
        except BaseException:
            os.close(fd)
            self.lock_fd = None
            raise

    def recover(self) -> None:
        for path in self.root.iterdir():
            if not re.fullmatch(r"[0-9a-f]{32}", path.name):
                continue
            if path.is_symlink() or not path.is_dir():
                raise ValueError("Invalid job storage entry")
            state = path / "state.json"
            if not state.exists():
                # An admission interrupted before its first durable state.
                if not self.remove_tree(path):
                    raise ValueError("Job storage entry could not be removed")
                continue
            if state.stat().st_size > MIB:
                raise ValueError("Invalid job state size")
            job = Job(**json.loads(state.read_text()))
            if job.id != path.name:
                raise ValueError("Invalid job state identity")
            self.jobs[job.id] = job
            if job.state not in TERMINAL:
                self.transition(job, "interrupted", "server_restarted")
            self.clean_work(job)
        observed = [j for j in self.jobs.values() if j.sandbox in {"enabled", "disabled"}]
        if observed:
            self.sandbox = max(observed, key=lambda j: j.updated_at).sandbox
        self.expire()

    async def start(self) -> None:
        self.open()
        self.sweeper = asyncio.create_task(self.sweep())

    def clean_work(self, job: Job) -> None:
        self.remove_tree(self.path(job) / "work")
        if job.state not in {"completed", "completed_with_warnings"}:
            self.remove_tree(self.path(job) / "results")
            job.results = []

    def remove_tree(self, path: Path) -> bool:
        """Remove a directory inside the job root without following links.

        Only a job directory or one of its direct children may be removed. A
        symlink or other non-directory entry there is unlinked itself, never
        followed. Directory trees are removed by descriptor
        (``rmtree.avoids_symlink_attacks``), so links swapped in underneath
        cannot redirect the removal either. Returns False after marking
        storage unhealthy when the entry could not be removed.
        """
        relative = path.relative_to(self.root).parts
        if not 1 <= len(relative) <= 2 or not re.fullmatch(r"[0-9a-f]{32}", relative[0]):
            raise ValueError("Refusing to remove a path outside a job directory")
        try:
            info = path.lstat()
        except FileNotFoundError:
            return True
        except OSError:
            self.storage_healthy = False
            return False
        try:
            if stat.S_ISDIR(info.st_mode):
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            return True
        except OSError:
            # Failed cleanup must not silently release capacity for more data.
            self.storage_healthy = False
            return False
        return True

    def active(self, job: Job) -> bool:
        """Still holding a full scratch reservation: unfinished, uploading or rendering."""
        return job.state not in TERMINAL or job.id in self.uploads or job.id in self.tasks

    def owner_active_limit(self) -> int:
        slots = self.limits.store_bytes // (self.limits.scratch_bytes + MIB)
        return max(1, slots - 1)

    def reserved_bytes(self) -> int:
        return sum(
            self.limits.scratch_bytes + MIB
            if self.active(j)
            else MIB + sum(r["bytes"] for r in j.results)
            for j in self.jobs.values()
        )

    def admit(self, owner: str, request: JobRequest) -> Job:
        self.expire()
        if not self.storage_healthy:
            raise APIError(503, "storage_unavailable", "Job storage is unavailable")
        owned = [j for j in self.jobs.values() if j.owner == owner]
        if (
            self.closing
            or len(self.jobs) >= self.limits.max_jobs
            # One principal may hold every scratch reservation but one, so
            # another principal can always start a job while storage allows.
            or sum(self.active(j) for j in owned) >= self.owner_active_limit()
            # Retained results count; cancelled and failed jobs hold nothing.
            or sum(self.active(j) or bool(j.results) for j in owned) >= self.limits.owner_jobs
            or self.reserved_bytes() + self.limits.scratch_bytes + MIB > self.limits.store_bytes
        ):
            raise APIError(429, "capacity_exhausted", "Job capacity is full; retry after retention")
        if isinstance(request.source, MountSource):
            mount = self.mounts.get(request.source.mount)
            if not mount or owner not in mount.owners:
                raise APIError(404, "source_not_found", "Source not found")
            parts_for(request.source.path, empty=True)
        now = time.time()
        # Keep client omission meaningful across the disposable-renderer boundary.
        # ExportOptions supplies ordinary defaults when it is reconstructed, while
        # the source-theme path can still distinguish an omitted font from an
        # explicit portable override.
        payload = {
            "source": request.source.model_dump(),
            "settings": request.settings.model_dump(exclude_unset=True),
        }
        job = Job(uuid.uuid4().hex, owner, "awaiting_upload", now, now, payload)
        try:
            self.prepare(job, request)
        except OSError:
            self.jobs.pop(job.id, None)
            self.remove_tree(self.path(job))
            self.storage_healthy = False
            raise APIError(507, "storage_unavailable", "Job storage is unavailable") from None
        return job

    def prepare(self, job: Job, request: JobRequest) -> None:
        directory = self.path(job)
        directory.mkdir(mode=0o700)
        work = directory / "work"
        work.mkdir(mode=0o700)
        for name in ("tmp", "config", "cache"):
            (work / name).mkdir()
        config = {"request": job.request, "limits": asdict(self.limits), "browser": self.browser}
        if isinstance(request.source, MountSource):
            config["mount_path"] = str(self.mounts[request.source.mount].path)
        atomic_json(work / "request.json", config)
        self.jobs[job.id] = job
        self.record(job, "state")
        if isinstance(request.source, MountSource):
            self.enqueue(job)

    def get(self, owner: str, job_id: str) -> Job:
        job = self.jobs.get(job_id)
        if not job or job.owner != owner:
            raise APIError(404, "job_not_found", "Job not found")
        return job

    def enqueue(self, job: Job) -> None:
        self.transition(job, "queued")
        self.schedule()

    def schedule(self) -> None:
        if self.closing:
            return
        for job in self.jobs.values():
            if len(self.tasks) >= self.limits.concurrent_jobs:
                break
            if job.state == "queued" and job.id not in self.tasks:
                self.tasks[job.id] = asyncio.create_task(self.run(job))

    async def run(self, job: Job) -> None:
        try:
            if job.state != "queued":
                return
            self.transition(job, "running")
            # export_seconds counts from here, so guardian start-up is inside it.
            # CLOCK_MONOTONIC is shared by every process on the host.
            deadline = time.monotonic() + self.limits.export_seconds
            process = await self.launch(job, deadline)
            self.processes[job.id] = process
            # Cancellation may arrive during the await in process creation.
            if job.state == "cancelling":
                self.revoke(process)
            result, error = await self.read_events(job, process)
            await process.wait()
            if process.returncode != 0:
                error = error or "worker_failed"
            self.finish(job, result, error)
        except Exception:
            process = self.processes.get(job.id)
            if process:
                self.revoke(process)
                await process.wait()
            self.transition(job, "failed", "worker_failed")
        finally:
            self.clean_work(job)
            self.processes.pop(job.id, None)
            self.tasks.pop(job.id, None)
            self.schedule()

    async def launch(self, job: Job, deadline: float) -> asyncio.subprocess.Process:
        work = self.path(job) / "work"
        # Do not inherit API credentials, proxy credentials, user Python hooks,
        # library preload variables or desktop application state.
        environment = {
            "PATH": os.defpath,
            "LANG": "C.UTF-8",
            "PYTHONUNBUFFERED": "1",
            "TMPDIR": str(work / "tmp"),
            "XDG_CACHE_HOME": str(work / "cache"),
            "XDG_CONFIG_HOME": str(work / "config"),
        }
        assert self.lock_fd is not None
        return await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "platefold.server_guardian",
            str(work),
            str(self.lock_fd),
            repr(deadline),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            # Package discovery is from installation; never from uploaded content.
            cwd=Path(__file__).resolve().parent.parent,
            pass_fds=(self.lock_fd,),
            start_new_session=True,
            limit=MAX_EVENT_BYTES,
        )

    async def read_events(self, job: Job, process) -> tuple[dict | None, str | None]:
        result, error, exited = None, None, False
        while line := await process.stdout.readline():
            if len(line) > MAX_EVENT_BYTES:
                raise ValueError("Worker event exceeds bound")
            event = json.loads(line)
            kind = event.get("event")
            if kind == "progress":
                self.record(
                    job, "progress", done=event["done"], total=event["total"], file=event["file"]
                )
            elif kind == "result":
                result = event
            elif kind == "error":
                error = event["code"]
            elif kind == "exit":
                exited = True
                error = error or event["code"]
                if event.get("sandbox") in SANDBOX_STATES:
                    job.sandbox = event["sandbox"]
                    if job.sandbox != "unknown":
                        self.sandbox = job.sandbox
            else:
                raise ValueError("Invalid worker event")
        return result, error if exited else "worker_failed"

    def finish(self, job: Job, result: dict | None, error: str | None) -> None:
        if job.state == "cancelling":
            self.transition(job, "cancelled")
        elif error or not result:
            self.transition(job, "failed", error or "worker_failed")
        else:
            job.results = result["results"]
            os.replace(self.path(job) / "work" / "output", self.path(job) / "results")
            self.transition(job, result["status"])

    @staticmethod
    def revoke(process) -> None:
        if process.stdin:
            process.stdin.close()

    def cancel(self, job: Job) -> None:
        if job.state in TERMINAL or job.state == "cancelling":
            return
        if job.state == "running":
            self.transition(job, "cancelling")
            if process := self.processes.get(job.id):
                self.revoke(process)
        else:
            self.transition(job, "cancelled")
            # The upload handler retains its directory until its active write ends.
            if job.id not in self.uploads:
                self.clean_work(job)

    def expire(self) -> None:
        now = time.time()
        for job in list(self.jobs.values()):
            if job.id in self.uploads or job.id in self.tasks or self.downloads.get(job.id):
                continue
            if (
                job.finished_at is not None
                and now >= job.finished_at + self.limits.retention_seconds
            ):
                # Keep the record, and its reservation, until the files are gone.
                if self.remove_tree(self.path(job)):
                    del self.jobs[job.id]
            elif job.state in {
                "awaiting_upload",
                "queued",
            } and now >= job.created_at + self.limits.upload_seconds + (
                self.limits.export_seconds if job.state == "queued" else 0
            ):
                self.transition(job, "failed", "admission_expired")
                self.clean_work(job)

    async def sweep(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                self.expire()
            except OSError:
                self.storage_healthy = False
            self.schedule()

    async def close(self) -> None:
        self.closing = True
        if self.sweeper:
            self.sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.sweeper
        active = [j for j in self.jobs.values() if j.state not in TERMINAL]
        for job in active:
            self.cancel(job)
        if self.tasks:
            await asyncio.gather(*list(self.tasks.values()), return_exceptions=True)
        for job in active:
            self.transition(job, "interrupted", "server_stopped")
            self.clean_work(job)
        if self.lock_fd is not None:
            os.close(self.lock_fd)
            self.lock_fd = None
