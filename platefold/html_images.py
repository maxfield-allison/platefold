"""Rewrite image tags without changing the surrounding authored HTML."""

from __future__ import annotations

from collections.abc import Callable
from html.parser import HTMLParser


class ImageTags(HTMLParser):
    """Use parsed attributes while preserving untouched source bytes and text."""

    def __init__(self, source: str, replace: Callable[[dict[str, str | None]], str]):
        super().__init__(convert_charrefs=True)
        self.source = source
        self.replace = replace
        self.offsets = [0]
        for line in source.split("\n"):
            self.offsets.append(self.offsets[-1] + len(line) + 1)
        self.parts: list[str] = []
        self.cursor = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "img":
            return
        # HTML uses the first occurrence of duplicate attributes.
        attributes: dict[str, str | None] = {}
        for name, value in attrs:
            attributes.setdefault(name, value)
        if not attributes.get("src"):
            return
        line, column = self.getpos()
        start = self.offsets[line - 1] + column
        raw = self.get_starttag_text()
        assert raw is not None  # called only while processing an actual start tag
        self.parts.extend((self.source[self.cursor : start], self.replace(attributes)))
        self.cursor = start + len(raw)

    def rewrite(self) -> str:
        self.feed(self.source)
        self.close()
        return "".join(self.parts) + self.source[self.cursor :]
