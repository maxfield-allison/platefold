"""Phase timing for the export pipeline.

Deliberately tiny: a wall-clock stopwatch keyed by phase name, so `--timings`
can answer "where did the time actually go" without pulling in a profiler.
Optimising this pipeline without measuring it first is guesswork — the browser
dominates so thoroughly that most Python-side micro-optimisation is noise.
"""

from __future__ import annotations

import time
from collections import defaultdict
from collections.abc import Callable
from contextlib import contextmanager


class Timings:
    """Accumulates elapsed wall-clock time and call counts per phase."""

    def __init__(self, enabled: bool = False, on_phase: Callable[[str], None] | None = None):
        self.enabled = enabled
        self.on_phase = on_phase
        self.total: dict[str, float] = defaultdict(float)
        self.counts: dict[str, int] = defaultdict(int)
        self._started = time.perf_counter()

    @contextmanager
    def phase(self, name: str):
        if self.on_phase:
            self.on_phase(name)
        if not self.enabled:
            yield
            return
        start = time.perf_counter()
        try:
            yield
        finally:
            self.total[name] += time.perf_counter() - start
            self.counts[name] += 1

    def add(self, name: str, seconds: float) -> None:
        if self.enabled:
            self.total[name] += seconds
            self.counts[name] += 1

    @property
    def elapsed(self) -> float:
        return time.perf_counter() - self._started

    def report(self) -> str:
        """A table of phases, slowest first."""
        if not self.enabled or not self.total:
            return ""
        wall = self.elapsed
        rows = sorted(self.total.items(), key=lambda kv: kv[1], reverse=True)
        width = max(len(name) for name, _ in rows)
        lines = ["", "Phase timings (wall clock %.2fs)" % wall, "-" * (width + 26)]
        for name, seconds in rows:
            lines.append(
                "%-*s  %7.2fs  %5.1f%%  n=%d"
                % (width, name, seconds, 100 * seconds / wall if wall else 0, self.counts[name])
            )
        return "\n".join(lines)
