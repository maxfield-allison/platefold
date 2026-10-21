"""Linear-time answers for link destinations and emphasis in Python-Markdown.

Two more of Python-Markdown's inline processors search the rest of a
paragraph for every candidate they reject, so many rejected candidates cost
time proportional to the square of the paragraph's length:

- A link or image destination, `[text](`, is scanned one character at a time
  for its closing `)`. Without one the scan reaches the end of the paragraph,
  and 5,000 `[a](` took almost ten seconds.
- An emphasis opener such as `_` or `**` is matched by a regular expression
  that looks for a closer anywhere later in the paragraph. Without one it
  fails only at the end, and 10,000 `_a ` took more than five seconds.

Both are answered here by predicting, from tables, whether the stock method or
expression would succeed. A candidate predicted to fail gets the stock "no
match" answer at once. Every other candidate is handed to the stock code,
which consumes the text it scans, so the output is unchanged and the total
work stays linear. Each prediction reads only the text from the candidate to
the end, so the tables are shared across Python-Markdown's successive strings
through `inline_scan.SuffixCache`. The tests compare the predictions with the
stock code on generated text.

`install` changes only the processors of one parser instance, and only while
they still use the library's own method and expressions.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Callable
from typing import Any, cast

import markdown
from markdown import inlinepatterns
from markdown.inlinepatterns import AsteriskProcessor, EmStrongItem, LinkInlineProcessor

from .inline_scan import SuffixCache

_WORD = re.compile(r"\w")
_LINK_CHARS = re.compile(r"[()'\"]")


def _count(positions: list[int], start: int, end: int) -> int:
    """How many of the sorted `positions` lie in `[start, end)`."""
    return bisect_left(positions, end) - bisect_left(positions, start)


def _first(positions: list[int], start: int) -> int:
    """The first of the sorted `positions` at or after `start`, or -1."""
    index = bisect_left(positions, start)
    return positions[index] if index < len(positions) else -1


# -- link destinations -------------------------------------------------------


class _LinkTables:
    __slots__ = ("closes", "opens", "paren_match", "quote_closers", "quotes", "size")

    def __init__(self, text: str):
        self.size = len(text)
        self.paren_match: dict[int, int] = {}
        self.opens: list[int] = []
        self.closes: list[int] = []
        self.quotes: dict[str, list[int]] = {"'": [], '"': [], "any": []}
        # For each quote character, the `)` positions whose previous
        # non-space character is that quote: (quote positions, `)` positions).
        self.quote_closers: dict[str, tuple[list[int], list[int]]] = {
            "'": ([], []),
            '"': ([], []),
        }
        stack: list[int] = []
        for match in _LINK_CHARS.finditer(text):
            position = match.start()
            char = match.group()
            if char == "(":
                self.opens.append(position)
                stack.append(position)
            elif char == ")":
                self.closes.append(position)
                if stack:
                    self.paren_match[stack.pop()] = position
                last = position - 1
                while last >= 0 and text[last] == " ":
                    last -= 1
                if last >= 0 and text[last] in self.quote_closers:
                    before, closers = self.quote_closers[text[last]]
                    before.append(last)
                    closers.append(position)
            else:
                self.quotes[char].append(position)
                self.quotes["any"].append(position)

    def first_closer(self, quote: str, after: int) -> int:
        """The first `)` whose previous non-space character is a `quote` after `after`."""
        before, closers = self.quote_closers[quote]
        index = bisect_right(before, after)
        return closers[index] if index < len(before) else -1

    def parens(self, start: int, end: int) -> int:
        return _count(self.opens, start, end) + _count(self.closes, start, end)


def _link_handled(t: _LinkTables, opener: int, start: int) -> bool:
    """Whether `LinkInlineProcessor.getLink` accepts a destination.

    `opener` is the `(` and `start` the first character after it and any
    whitespace, both as positions in the text the tables were built from.
    """
    close = t.paren_match.get(opener, -1)
    quote_at = _first(t.quotes["any"], start)
    if close >= 0 and (quote_at < 0 or close < quote_at):
        return True  # The parentheses balance before any quote is seen.
    if quote_at < 0:
        return False
    # From the first quote on, the stock scan ends at a `)` that follows a
    # later copy of that quote, or a later copy of the other quote character
    # after that one's first appearance.
    char = "'" if _first(t.quotes["'"], quote_at) == quote_at else '"'
    other = '"' if char == "'" else "'"
    if t.first_closer(char, quote_at) >= 0:
        return True
    other_at = _first(t.quotes[other], quote_at + 1)
    if other_at >= 0 and t.first_closer(other, other_at) >= 0:
        return True
    # Otherwise it is accepted once enough parentheses follow the quote to use
    # up the nesting depth reached before it.
    depth = 1 + _count(t.opens, start, quote_at) - _count(t.closes, start, quote_at)
    return t.parens(quote_at + 1, t.size) >= depth


def _get_link(stock: Callable[[str, int], Any], pattern: re.Pattern[str], cache: SuffixCache):
    def get_link(data: str, index: int):
        match = pattern.match(data, pos=index)
        if match is None or match.group(1):
            return stock(data, index)
        tables, shift = cache.get(data, match.start())
        if _link_handled(tables, match.start() + shift, match.end() + shift):
            return stock(data, index)
        # Every caller discards a destination it does not accept.
        return "", None, index, False

    return get_link


# -- emphasis ------------------------------------------------------------------


class _EmphasisTables:
    """Positions of one delimiter character, and of the closers built from it."""

    __slots__ = ("_valid", "char", "positions", "size", "text")

    def __init__(self, text: str, char: str):
        self.text = text
        self.char = char
        self.size = len(text)
        self.positions = [match.start() for match in re.finditer(re.escape(char), text)]
        self._valid: dict[str, list[int]] = {}

    def at(self, position: int, run: str) -> bool:
        return self.text.startswith(run, position)

    def word(self, position: int) -> bool:
        return 0 <= position < self.size and _WORD.match(self.text, position) is not None

    def valid(self, name: str) -> list[int]:
        """The delimiter positions that satisfy the closer test called `name`."""
        found = self._valid.get(name)
        if found is None:
            test = _CLOSERS[name]
            found = self._valid[name] = [p for p in self.positions if test(self, p)]
        return found

    def first(self, name: str, start: int) -> int:
        return _first(self.valid(name), start)

    def any_at_or_after(self, name: str, start: int) -> bool:
        found = self.valid(name)
        return bool(found) and found[-1] >= start


def _not_after_same(t: _EmphasisTables, p: int) -> bool:
    return p == 0 or t.text[p - 1] != t.char


_CLOSERS: dict[str, Callable[[_EmphasisTables, int], bool]] = {
    "one": lambda t, p: True,
    "two": lambda t, p: t.at(p, t.char * 2),
    "three": lambda t, p: t.at(p, t.char * 3),
    # (?<!\w)\1(?!\1) in the middle of SMART_STRONG_EM_RE
    "smart-middle": lambda t, p: not t.word(p - 1) and not t.at(p + 1, t.char),
    # \1{3}(?!\w) at the end of SMART_STRONG_EM_RE
    "smart-three": lambda t, p: t.at(p, t.char * 3) and not t.word(p + 3),
    # (?<!_)\1(?!\w) with \1 = __
    "smart-two": lambda t, p: t.at(p, t.char * 2) and _not_after_same(t, p) and not t.word(p + 2),
    # (?<!_)\1(?!\w) with \1 = _
    "smart-one": lambda t, p: _not_after_same(t, p) and not t.word(p + 1),
}

# Each prediction receives the current text and candidate position (for the
# opener, which may sit just before the shared ending) and the tables with the
# shift that maps a position into them (for the closers, which lie within it).
Predict = Callable[[str, int, _EmphasisTables, int], bool]


def _opener_word(text: str, pos: int) -> bool:
    return pos > 0 and _WORD.match(text, pos - 1) is not None


def _em_strong(text: str, pos: int, t: _EmphasisTables, shift: int) -> bool:
    # (c)\1{2}(.+?)\1(.*?)\1{2}
    if not text.startswith(t.char * 3, pos):
        return False
    single = t.first("one", pos + 4 + shift)
    return single >= 0 and t.any_at_or_after("two", single + 1)


def _strong_em(text: str, pos: int, t: _EmphasisTables, shift: int) -> bool:
    # (c)\1{2}(.+?)\1{2}(.*?)\1
    if not text.startswith(t.char * 3, pos):
        return False
    double = t.first("two", pos + 4 + shift)
    return double >= 0 and t.any_at_or_after("one", double + 2)


def _strong_em3(text: str, pos: int, t: _EmphasisTables, shift: int) -> bool:
    # (\*)\1(?!\1)([^*]+?)\1(?!\1)(.+?)\1{3}
    c = t.char
    if not text.startswith(c * 2, pos) or pos + 2 >= len(text) or text[pos + 2] == c:
        return False
    middle = t.first("one", pos + 3 + shift)
    if middle < 0 or t.at(middle + 1, c):
        return False
    return t.any_at_or_after("three", middle + 2)


def _strong(text: str, pos: int, t: _EmphasisTables, shift: int) -> bool:
    # (\*{2})(.+?)\1
    return text.startswith(t.char * 2, pos) and t.any_at_or_after("two", pos + 3 + shift)


def _emphasis(text: str, pos: int, t: _EmphasisTables, shift: int) -> bool:
    # (\*)([^\*]+)\1
    c = t.char
    if not text.startswith(c, pos) or pos + 1 >= len(text) or text[pos + 1] == c:
        return False
    return t.first("one", pos + 1 + shift) >= 0


def _smart_strong_em(text: str, pos: int, t: _EmphasisTables, shift: int) -> bool:
    # (?<!\w)(\_)\1(?!\1)(.+?)(?<!\w)\1(?!\1)(.+?)\1{3}(?!\w)
    c = t.char
    if _opener_word(text, pos) or not text.startswith(c * 2, pos) or text.startswith(c, pos + 2):
        return False
    middle = t.first("smart-middle", pos + 3 + shift)
    return middle >= 0 and t.any_at_or_after("smart-three", middle + 2)


def _smart_strong(text: str, pos: int, t: _EmphasisTables, shift: int) -> bool:
    # (?<!\w)(_{2})(?!_)(.+?)(?<!_)\1(?!\w)
    c = t.char
    if _opener_word(text, pos) or not text.startswith(c * 2, pos) or text.startswith(c, pos + 2):
        return False
    return t.any_at_or_after("smart-two", pos + 3 + shift)


def _smart_emphasis(text: str, pos: int, t: _EmphasisTables, shift: int) -> bool:
    # (?<!\w)(_)(?!_)(.+?)(?<!_)\1(?!\w)
    c = t.char
    if _opener_word(text, pos) or not text.startswith(c, pos) or text.startswith(c, pos + 1):
        return False
    return t.any_at_or_after("smart-one", pos + 2 + shift)


# Each prediction is paired with the expression it was derived from, and is
# used only for a processor that still carries exactly that expression.
PREDICTIONS: dict[str, tuple[str, Predict]] = {
    inlinepatterns.EM_STRONG_RE: ("*", _em_strong),
    inlinepatterns.STRONG_EM_RE: ("*", _strong_em),
    inlinepatterns.STRONG_EM3_RE: ("*", _strong_em3),
    inlinepatterns.STRONG_RE: ("*", _strong),
    inlinepatterns.EMPHASIS_RE: ("*", _emphasis),
    inlinepatterns.EM_STRONG2_RE: ("_", _em_strong),
    inlinepatterns.STRONG_EM2_RE: ("_", _strong_em),
    inlinepatterns.SMART_STRONG_EM_RE: ("_", _smart_strong_em),
    inlinepatterns.SMART_STRONG_RE: ("_", _smart_strong),
    inlinepatterns.SMART_EMPHASIS_RE: ("_", _smart_emphasis),
}


class Predicted:
    """A compiled expression that skips a match predicted to fail."""

    __slots__ = ("_cache", "_compiled", "_predict")

    def __init__(self, compiled: re.Pattern[str], predict: Predict, cache: SuffixCache):
        self._compiled = compiled
        self._predict = predict
        self._cache = cache

    def match(self, string: str, pos: int = 0, endpos: int | None = None):
        if endpos is not None:
            return self._compiled.match(string, pos, endpos)
        tables, shift = self._cache.get(string, pos)
        if not self._predict(string, pos, tables, shift):
            return None
        return self._compiled.match(string, pos)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._compiled, name)


class InlineLookups:
    """The tables `install` attached to one parser, kept for tests that bound the work."""

    def __init__(self) -> None:
        self.links = SuffixCache(_LinkTables)
        self.emphasis = {
            char: SuffixCache(lambda text, char=char: _EmphasisTables(text, char)) for char in "*_"
        }


def install(parser: markdown.Markdown) -> InlineLookups:
    """Give `parser`'s link and emphasis processors linear-time predictions."""
    lookups = InlineLookups()
    for processor in parser.inlinePatterns:
        target: Any = processor
        if getattr(type(processor), "getLink", None) is LinkInlineProcessor.getLink:
            target.getLink = _get_link(target.getLink, target.RE_LINK, lookups.links)
        if isinstance(processor, AsteriskProcessor):
            items = []
            for item in processor.PATTERNS:
                known = PREDICTIONS.get(item.pattern.pattern)
                if known is None:
                    items.append(item)
                    continue
                char, predict = known
                cache = lookups.emphasis[char]
                items.append(
                    EmStrongItem(
                        cast(Any, Predicted(item.pattern, predict, cache)), item.builder, item.tags
                    )
                )
            target.PATTERNS = items
    return lookups
