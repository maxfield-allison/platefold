"""Cooperative cancellation for work that runs in Plate|Fold's own Python code.

The export service owns one cancel check per job. Calling it returns normally
while the job may continue and raises `CancelledError` once the user has asked
to stop (or `ResourceLimitError` once the job's deadline has passed). Long
loops call it between units of work so that Cancel takes effect within a
fraction of a second rather than after the loop finishes.

A check costs a function call and a clock read. Loops whose units are tiny,
such as XML elements, call it through `checked` or `Throttle`, which consult
the real check once every `stride` units.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import TypeVar

CancelCheck = Callable[[], None]
T = TypeVar("T")

# Enough units to keep the overhead negligible, few enough that even slow
# units (a spreadsheet row, a markdown block) reach a check within milliseconds.
DEFAULT_STRIDE = 256


class CancelledError(RuntimeError):
    """The caller requested cancellation while work was in progress."""


def never_cancelled() -> None:
    """The check used when a caller supplies none."""


class Throttle:
    """Call a cancel check on every `stride`-th tick."""

    __slots__ = ("_check", "_countdown", "_stride")

    def __init__(self, check: CancelCheck | None, stride: int = DEFAULT_STRIDE):
        self._check = check or never_cancelled
        self._stride = max(1, stride)
        self._countdown = self._stride

    def __call__(self) -> None:
        self._countdown -= 1
        if self._countdown <= 0:
            self._countdown = self._stride
            self._check()


def checked(
    items: Iterable[T], check: CancelCheck | None, stride: int = DEFAULT_STRIDE
) -> Iterator[T]:
    """Yield `items`, calling `check` before the first and every `stride`-th item."""
    check = check or never_cancelled
    check()
    tick = Throttle(check, stride)
    for item in items:
        tick()
        yield item
