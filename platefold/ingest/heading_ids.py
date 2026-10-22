"""Unique heading ids in linear time, identical to Python-Markdown's.

Python-Markdown's table-of-contents extension gives repeated headings the ids
`name`, `name_1`, `name_2` and so on through `markdown.extensions.toc.unique`.
That function finds each free suffix by trying every smaller one first, so a
document that repeats one heading n times costs about n²/2 set lookups: 8,000
copies of one heading took over a minute. The same extension nests its contents
entries with `list.pop(0)`, which is quadratic in the number of headings once
they reach the hundreds of thousands.

`HeadingIds` returns exactly the ids `unique` would, but remembers, for each
base name, how far the run of taken suffixes already reaches, so every heading
costs a small constant number of lookups. `install()` puts it and a
queue-backed nesting step in place of the library's functions. Both are module
globals the extension looks up on every call, the same kind of seam the DOCX
reader uses for Mammoth's parser. Outside a `scope()`, the replacement `unique`
defers to the original function, so other users of Python-Markdown in the same
process see no change.

The scope also carries the export's cancel check, consulted every few headings,
because the extension assigns every id inside one call that offers no other
point to stop.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, MutableSet
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from markdown.extensions import toc

from ..cancellation import CancelCheck, Throttle, never_cancelled

# The library's own functions, kept before `install()` replaces them.
STOCK_UNIQUE = toc.unique
# The nesting step only needs `len`, truth and `pop(0)` from its argument.
STOCK_NEST_TOC_TOKENS: Callable[[Any], list] = toc.nest_toc_tokens

# Headings between cancel checks. Assigning an id costs microseconds and
# rendering its heading well under a millisecond, so a check is reached many
# times a second.
CANCEL_STRIDE = 64


class HeadingIds:
    """Allocate ids as `markdown.extensions.toc.unique` does, in linear time.

    `unique` keeps a taken id's trailing `_<number>` and counts upward from
    it, and otherwise appends `_1`, `_2`, and so on, returning the first
    candidate not already in `ids`. The candidates for one call are
    therefore `stem_n`, `stem_n+1`, and so on for a stem and starting
    number read from the id. `_reach` records, for each stem, runs of
    consecutive suffixes known to be taken, and path compression keeps the
    walk across a run short, so a thousand copies of one heading no longer
    walk a thousand candidates each.

    `ids` may be shared with other code that adds to it. Ids are never
    removed, so a suffix recorded as taken stays taken and each recorded
    run stays valid.
    """

    __slots__ = ("_reach", "ids", "probes")

    def __init__(self, ids: MutableSet[str] | None = None):
        self.ids: MutableSet[str] = set() if ids is None else ids
        # stem -> {n: m}: every `stem_j` with n <= j < m is taken.
        self._reach: dict[str, dict[int, int]] = {}
        # Membership tests made so far, for tests that bound the work.
        self.probes = 0

    def __call__(self, id: str) -> str:
        ids = self.ids
        self.probes += 1
        if id and id not in ids:
            ids.add(id)
            return id
        if "\n" in id:
            # `unique` matches its counter pattern with `.` and `$`, which
            # treat a newline specially. Slugs never contain one; take the
            # original path rather than reproduce that corner.
            return STOCK_UNIQUE(id, ids)
        stem, start = _counter(id)
        number = self._free(stem, start)
        found = "%s_%d" % (stem, number)
        ids.add(found)
        self._reach[stem][number] = number + 1
        return found

    def _free(self, stem: str, number: int) -> int:
        """The smallest suffix at or above `number` whose id is not taken."""
        reach = self._reach.setdefault(stem, {})
        ids = self.ids
        visited = []
        while True:
            following = reach.get(number)
            if following is None:
                self.probes += 1
                if "%s_%d" % (stem, number) not in ids:
                    break
                following = number + 1
            visited.append(number)
            number = following
        for passed in visited:
            reach[passed] = number
        return number


def _counter(id: str) -> tuple[str, int]:
    """The stem and first suffix `unique` would try for a taken id."""
    match = toc.IDCOUNT_RE.match(id)
    if match:
        try:
            return match.group(1), int(match.group(2)) + 1
        except ValueError:
            # Too many digits for Python's integer conversion limit, where
            # `unique` itself raises. Counting on from the whole id still
            # gives a unique result.
            pass
    return id, 1


class _Scope:
    __slots__ = ("_allocator", "_tick")

    def __init__(self, cancel_check: CancelCheck):
        self._allocator: HeadingIds | None = None
        self._tick = Throttle(cancel_check, CANCEL_STRIDE)

    def unique(self, id: str, ids: MutableSet[str]) -> str:
        self._tick()
        allocator = self._allocator
        if allocator is None or allocator.ids is not ids:
            # One conversion builds one set; a new set starts a fresh record.
            allocator = self._allocator = HeadingIds(ids)
        return allocator(id)


_ACTIVE: ContextVar[_Scope | None] = ContextVar("platefold_heading_ids", default=None)


def unique(id: str, ids: MutableSet[str]) -> str:
    """Drop-in replacement for `markdown.extensions.toc.unique`."""
    scope = _ACTIVE.get()
    if scope is None:
        return STOCK_UNIQUE(id, ids)
    return scope.unique(id, ids)


class _FrontQueue(deque):
    """The list operations `nest_toc_tokens` uses, with a constant-time `pop(0)`."""

    def pop(self, index: int = -1):  # type: ignore[override]
        if index == 0:
            return self.popleft()
        if index == -1:
            return super().pop()
        raise IndexError("only the first and last entries can be popped")


def nest_toc_tokens(toc_list: list) -> list:
    """Drop-in replacement for `markdown.extensions.toc.nest_toc_tokens`.

    The original pops the front of a list once per heading. Running it over
    a queue gives the same result in linear time; the input is then
    emptied, as the original leaves it.
    """
    nested = STOCK_NEST_TOC_TOKENS(_FrontQueue(toc_list))
    toc_list.clear()
    return nested


def install() -> None:
    """Point Python-Markdown's contents extension at the linear functions."""
    toc.unique = unique
    toc.nest_toc_tokens = nest_toc_tokens


@contextmanager
def scope(cancel_check: CancelCheck | None = None) -> Iterator[None]:
    """Allocate ids linearly, and check `cancel_check`, within this context."""
    install()
    token = _ACTIVE.set(_Scope(cancel_check or never_cancelled))
    try:
        yield
    finally:
        _ACTIVE.reset(token)
