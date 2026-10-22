"""Observe whether Chromium's renderers actually run inside its Linux sandbox.

Plate|Fold never passes ``--no-sandbox``, but that does not prove the sandbox
is on: a distribution wrapper script or administrator policy can add flags, and
``--disable-seccomp-filter-sandbox`` removes one layer only. The export
guardian therefore inspects the renderer processes it already supervises.

A sandboxed Linux renderer has both layers: it runs in its own PID namespace
(layer 1, from the namespace or setuid sandbox) and under a seccomp-BPF filter
(layer 2). Both are visible in ``/proc/<pid>/status`` to a parent process, with
no cooperation from the renderer. A renderer applies its seccomp filter shortly
after it starts, so a renderer younger than ``SETTLE_SECONDS`` without one is
not yet evidence. A filter cannot be removed once applied. Chromium also copies
``--no-sandbox`` and ``--disable-seccomp-filter-sandbox`` onto every renderer's
command line, which is evidence at any age.

Two start-up windows need care. A renderer forked from Chromium's zygote shows
the zygote's command line, or an empty one, until it sets its own; so a process
is only filed away as "not a renderer" once it has settled. And ages come from
the kernel's boot clock: psutil's ``create_time()`` adds the start tick to the
boot time in ``/proc/stat``, which is whole seconds, so it can make a process
look up to a second older than it is.
"""

from __future__ import annotations

import contextlib
import os
import time
from collections.abc import Iterable
from pathlib import Path

import psutil

SETTLE_SECONDS = 1.0
ENABLED, DISABLED, UNKNOWN = "enabled", "disabled", "unknown"
STATES = frozenset({ENABLED, DISABLED, UNKNOWN})
UNSANDBOXED_FLAGS = frozenset({"--no-sandbox", "--disable-seccomp-filter-sandbox"})


def boot_clock() -> float:
    """Seconds since boot, on the clock ``/proc/<pid>/stat`` start times use."""
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def started(pid: int) -> float:
    """When the process started, in ``boot_clock()`` seconds, to one clock tick."""
    stat = Path(f"/proc/{pid}/stat").read_text()
    # The command name in parentheses may contain spaces; field 22 is starttime.
    ticks = int(stat.rsplit(")", 1)[1].split()[19])
    return ticks / os.sysconf("SC_CLK_TCK")


def isolated(status: str) -> bool:
    """Whether a ``/proc/<pid>/status`` text shows both sandbox layers."""
    fields = dict(line.split(":", 1) for line in status.splitlines() if ":" in line)
    seccomp = fields.get("Seccomp", "").strip() == "2"
    # NSpid lists the PID in each namespace from the outermost; more than one
    # entry means the process lives in a nested PID namespace.
    namespaced = len(fields.get("NSpid", "").split()) > 1
    return seccomp and namespaced


class SandboxObserver:
    """Accumulate per-renderer sandbox evidence across guardian polls."""

    def __init__(self) -> None:
        self.renderers: dict[int, str] = {}
        self.others: set[int] = set()

    def observe(self, processes: Iterable[psutil.Process], now: float | None = None) -> None:
        """Classify each process; ``now`` is ``boot_clock()`` time."""
        now = boot_clock() if now is None else now
        for process in processes:
            if process.pid in self.others or self.renderers.get(process.pid) == ENABLED:
                continue
            with contextlib.suppress(psutil.Error, OSError, ValueError, IndexError):
                self.classify(process, now)

    def classify(self, process: psutil.Process, now: float) -> None:
        command = process.cmdline()
        settled = now - started(process.pid) >= SETTLE_SECONDS
        if "--type=renderer" not in command:
            # A young process may be a renderer still showing the zygote's
            # command line; look again on the next poll.
            if settled:
                self.others.add(process.pid)
            return
        if UNSANDBOXED_FLAGS.intersection(command):
            self.renderers[process.pid] = DISABLED
        elif isolated(Path(f"/proc/{process.pid}/status").read_text()):
            self.renderers[process.pid] = ENABLED
        elif settled:
            self.renderers[process.pid] = DISABLED
        else:
            self.renderers.setdefault(process.pid, UNKNOWN)

    @property
    def state(self) -> str:
        """Disabled if any settled renderer lacked a layer; unknown without evidence."""
        states = set(self.renderers.values())
        if DISABLED in states:
            return DISABLED
        return ENABLED if ENABLED in states else UNKNOWN
