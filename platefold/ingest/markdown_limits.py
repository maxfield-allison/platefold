"""Hostile-input limits for the Markdown work that cannot be made linear here.

Two parts of converting Markdown still take time proportional to the square of
the input in the worst case:

- HTML tags that never close. Python-Markdown hands the document to Python's
  HTML parser to find raw HTML, and that parser reads `<` followed by a letter
  as the start of a tag, then searches every later character for its `>`.
  A tag without one searches the rest of the document, so each such tag costs
  the document's length: about 1.8 s per unclosed tag in a 10 MB document,
  inside one call that Cancel cannot interrupt.
- Many inline elements in one paragraph. Python-Markdown copies the whole
  paragraph each time it replaces one link, code span, HTML tag or emphasis,
  so the work is the number of elements times the paragraph's length. 20,000
  raw `<b>x</b>` tags in one 160 KB paragraph took nine seconds.

Rather than reimplement those parts, conversion refuses input whose searching
or copying would exceed `limits.max_html_tag_search_bytes` or
`limits.max_paragraph_rebuild_bytes`, with an error that names the paragraph.
Both totals are counted in bytes because that is what the time depends on; a
count of tags or elements alone would not bound it. The defaults are far above
the largest totals in several thousand real documents.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from typing import Any

import markdown
from markdown.inlinepatterns import InlineProcessor
from markdown.preprocessors import Preprocessor

from ..limits import ResourceLimitError, ResourceLimits

# What Python's HTML parser reads as the start of a tag.
_TAG_START = re.compile(r"<[A-Za-z]")
_PLACEHOLDER = re.compile("\x02[^\x03]*\x03")
_BLANK_LINE = re.compile(r"\n[ \t]*\n")
_SNIPPET = 60


def _snippet(text: str) -> str:
    plain = " ".join(_PLACEHOLDER.sub(" ", text).split())
    return plain if len(plain) <= _SNIPPET else plain[: _SNIPPET - 1] + "…"


def html_tag_searches(text: str) -> list[tuple[int, int]]:
    """(tag position, bytes searched) for each `<` that starts an HTML tag.

    The search for a tag's end is estimated as the distance to the next `>`,
    or to the end of the text when there is none.
    """
    ends = [match.start() for match in re.finditer(">", text)]
    found = []
    for match in _TAG_START.finditer(text):
        index = bisect_left(ends, match.end())
        end = ends[index] if index < len(ends) else len(text)
        found.append((match.start(), end - match.start()))
    return found


def _paragraph_at(text: str, position: int) -> str:
    start = 0
    for match in _BLANK_LINE.finditer(text, 0, position):
        start = match.end()
    end = _BLANK_LINE.search(text, position)
    return text[start : end.start() if end else len(text)]


class _TagSearch(Preprocessor):
    """Checks the document just before the raw HTML parser reads it."""

    def __init__(self, md: markdown.Markdown, limit: int):
        super().__init__(md)
        self.limit = limit

    def run(self, lines: list[str]) -> list[str]:
        text = "\n".join(lines)
        total = 0
        for position, searched in html_tag_searches(text):
            total += searched
            if total > self.limit:
                excerpt = _snippet(_paragraph_at(text, position))
                raise ResourceLimitError(
                    "Markdown has HTML tags that never close with '>': reading them would "
                    f"search more than limits.max_html_tag_search_bytes ({self.limit} bytes), "
                    f"reaching it in the paragraph beginning “{excerpt}”. Each unclosed tag "
                    "makes the HTML reader search the rest of the document. Write a literal "
                    "'<' as &lt; or inside `code`, close the tags, or raise the limit for a "
                    "trusted document."
                )
        return lines


class _InlineBudget:
    """Counts the work of one paragraph's inline elements."""

    __slots__ = ("elements", "limit", "paragraph", "work")

    def __init__(self, limit: int):
        self.limit = limit
        self.paragraph = ""
        self.elements = 0
        self.work = 0

    def start(self, text: str) -> None:
        self.paragraph = text
        self.elements = 0
        self.work = 0

    def add(self, text: str) -> None:
        # Replacing one element rebuilds the current text once.
        self.elements += 1
        self.work += len(text)
        if self.work > self.limit:
            raise ResourceLimitError(
                f"The paragraph beginning “{_snippet(self.paragraph)}” has too many inline "
                f"elements for its length: replacing its {self.elements} links, code spans, "
                f"HTML tags or emphasis in {len(self.paragraph) // 1024 + 1} KiB would copy "
                f"more than limits.max_paragraph_rebuild_bytes ({self.limit} bytes). Split the "
                "paragraph with blank lines, or raise the limit for a trusted document."
            )


class _ParagraphStart(InlineProcessor):
    """Tried first on every paragraph; records it and never matches."""

    def __init__(self, budget: _InlineBudget):
        # \A matches only where a text run begins.
        super().__init__(r"\A")
        self.budget = budget

    def handleMatch(self, m, data):  # type: ignore[override]
        self.budget.start(data)
        return None, None, None


def _counted(handle_match, budget: _InlineBudget):
    def handle(m, data, *rest):
        result = handle_match(m, data, *rest)
        if result[1] is not None and result[2] is not None and result[0] is not None:
            budget.add(data)
        return result

    return handle


def install(parser: markdown.Markdown, limits: ResourceLimits) -> None:
    """Refuse hostile input in `parser`'s conversions before it becomes slow."""
    # After fenced code is set aside (25) and before raw HTML is read (20).
    parser.preprocessors.register(
        _TagSearch(parser, limits.max_html_tag_search_bytes), "platefold_tag_search", 21
    )
    budget = _InlineBudget(limits.max_paragraph_rebuild_bytes)
    for processor in parser.inlinePatterns:
        if isinstance(processor, InlineProcessor):
            target: Any = processor
            target.handleMatch = _counted(target.handleMatch, budget)
    parser.inlinePatterns.register(_ParagraphStart(budget), "platefold_paragraph_start", 10_000)
