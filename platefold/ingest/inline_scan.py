"""Linear-time paths through Python-Markdown's inline parsing, and Cancel within it.

Python-Markdown applies each inline pattern to a paragraph's text by searching
for a match and then asking the pattern's processor whether it accepts it. The
two processors below examine the rest of the paragraph every time they are
asked, so a paragraph with many rejected candidates costs time proportional to
the square of its length:

- A link, image or reference (`[`) looks for its closing `]` one character at
  a time, to the end of the text when there is none. 20,000 unclosed `[a `
  openers (60 KB) took more than five minutes.
- A code span looks for a closing run of backticks after its opening run. In
  the text's last run of backticks there is none, and every position in that
  run is tried again. 20,000 backticks in a row took almost six seconds.

`install` gives the processors of one parser instance equivalent methods that
answer from tables: the position of each `[`'s matching `]`, and where the
text's final run of backticks begins. A table is built once and reused for the
later versions of the same text that Python-Markdown creates as it replaces
each element (see `SuffixCache`), so a long paragraph is not re-indexed after
every link. The results, and so
the HTML, are unchanged. The same parser's processors also check for
cancellation every `CANCEL_STRIDE` candidates, because Python-Markdown handles
a whole paragraph inside one call.

Other parsers in the process, and Python-Markdown's classes, are not changed.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import markdown
from markdown.inlinepatterns import BacktickInlineProcessor, LinkInlineProcessor

from ..cancellation import CancelCheck, Throttle

# Python-Markdown 3.11 added this method; earlier releases match code spans
# with a regular expression instead.
_STOCK_FIND_CODE_SPANS = getattr(BacktickInlineProcessor, "find_code_spans", None)

# Candidates between cancel checks. Rejecting one usually takes microseconds;
# the slowest remaining cases take a few milliseconds each on a large paragraph.
CANCEL_STRIDE = 16


class SuffixCache:
    """Tables built for one text and reused for later texts with the same ending.

    Python-Markdown replaces each accepted inline element with a placeholder,
    producing a new string, and continues after it. Every question asked here
    depends only on the text from the queried position to the end, which
    those replacements leave unchanged. A table built for one text therefore
    answers for a later text too, once the later text's ending is confirmed to
    be the same; that check is one string comparison rather than a rebuild.
    `get` returns the tables and the `shift` that maps a position in the text
    asked about to the same character in the text the tables were built from.
    """

    __slots__ = ("_base", "_build", "_current", "_shift", "_tables", "_verified", "built")

    def __init__(self, build: Callable[[str], Any]):
        self._build = build
        self._base: str | None = None
        self._tables: Any = None
        self._current: str | None = None
        self._verified = -1
        self._shift = 0
        # Characters indexed so far, for tests that bound the work.
        self.built = 0

    def get(self, text: str, position: int) -> tuple[Any, int]:
        suffix = len(text) - position
        if text is not self._current:
            self._current = text
            self._verified = len(text) if text is self._base else -1
            self._shift = len(self._base) - len(text) if self._base is not None else 0
        if suffix <= self._verified:
            return self._tables, self._shift
        base = self._base
        if base is not None and suffix <= len(base) and base.endswith(text[position:]):
            self._verified = suffix
            return self._tables, self._shift
        self._base = self._current = text
        self._tables = self._build(text)
        self._verified = len(text)
        self._shift = 0
        self.built += len(text)
        return self._tables, 0


_BRACKETS = re.compile(r"[\[\]]")


def _match_brackets(text: str) -> dict[int, int]:
    closers: dict[int, int] = {}
    open_positions: list[int] = []
    for match in _BRACKETS.finditer(text):
        if match.group() == "[":
            open_positions.append(match.start())
        elif open_positions:
            closers[open_positions.pop()] = match.start()
    return closers


def _get_text(stock: Callable[[str, int], tuple[str, int, bool]], table: SuffixCache):
    """`LinkInlineProcessor.getText`, answered from the bracket table.

    The stock method counts brackets from `index` until the count returns to
    zero, which is where the `[` just before `index` finds its match. Without
    one it returns everything to the end of the text and `False`; every caller
    discards the text in that case, so the table returns it empty rather than
    copying the rest of the paragraph for each rejected `[`.
    """

    def get_text(data: str, index: int) -> tuple[str, int, bool]:
        if not 0 < index <= len(data) or data[index - 1] != "[":
            return stock(data, index)
        # The match of a `[` depends only on the text from it to the end.
        closers, shift = table.get(data, index - 1)
        close = closers.get(index - 1 + shift)
        if close is None:
            return "", len(data), False
        close -= shift
        return data[index:close], close + 1, True

    return get_text


class _FinalTicks:
    """Where the most recent text's last run of backticks starts and ends."""

    __slots__ = ("_run", "_text")

    def __init__(self) -> None:
        self._text: str | None = None
        self._run = (-1, -1)

    def run(self, text: str) -> tuple[int, int]:
        if text is not self._text:
            self._text = text
            last = text.rfind("`")
            first = len(text[: last + 1].rstrip("`")) if last >= 0 else -1
            self._run = (first, last)
        return self._run


def _find_code_spans(stock: Callable[[int, str], Any], final: _FinalTicks):
    """`BacktickInlineProcessor.find_code_spans`, without rescanning the last run.

    An opening run that starts inside the text's final run of backticks has no
    later run to close it, so the stock method returns `None` after counting
    to the end of the run. Every other opening run is left to the stock method,
    which finds a closing run and consumes the text up to it.
    """

    def find_code_spans(start: int, text: str):
        first, last = final.run(text)
        if 0 <= first <= start <= last:
            return None
        return stock(start, text)

    return find_code_spans


def _checked(handle_match: Callable[..., Any], tick: Callable[[], None]):
    def handle(*args: Any) -> Any:
        tick()
        return handle_match(*args)

    return handle


class InlineScan:
    """What `install` added to one parser, kept for tests that bound the work."""

    def __init__(self) -> None:
        self.brackets = SuffixCache(_match_brackets)
        self.final_ticks = _FinalTicks()


def install(parser: markdown.Markdown, cancel_check: CancelCheck | None = None) -> InlineScan:
    """Give `parser`'s inline processors linear lookups and a cancel check."""
    scan = InlineScan()
    tick = Throttle(cancel_check, CANCEL_STRIDE) if cancel_check else None
    for processor in parser.inlinePatterns:
        target: Any = processor
        # Only the library's own methods are replaced; a subclass that
        # overrides one keeps its override.
        if getattr(type(processor), "getText", None) is LinkInlineProcessor.getText:
            target.getText = _get_text(target.getText, scan.brackets)
        if (
            _STOCK_FIND_CODE_SPANS is not None
            and getattr(type(processor), "find_code_spans", None) is _STOCK_FIND_CODE_SPANS
        ):
            target.find_code_spans = _find_code_spans(target.find_code_spans, scan.final_ticks)
        if tick is not None:
            target.handleMatch = _checked(target.handleMatch, tick)
    return scan
