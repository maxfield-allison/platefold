"""Generated placeholders that authored text cannot spell.

Rendering leaves markers in its HTML for later steps to fill in: where each
diagram goes, figure numbers, the figure a tile sheet belongs to, and contents
page numbers. A fixed spelling such as ``@@FIGNUM@@`` would also match a
document that happens to contain it, whether a page explaining the renderer,
a code sample or an imported cell, and turn that text into renderer state.

Every render therefore spells its placeholders with a key derived from a
digest of the text being rendered and checked to occur nowhere in it. A
document cannot contain a digest of its own contents, and copying one export's
spelling into a document changes the digest, so authored text can contain any
spelling of the markers without being read as one. The same input still
produces the same output.
"""

from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Iterable


def new_key(texts: Iterable[str] = ()) -> str:
    """A key derived from ``texts`` that occurs in none of them."""
    corpus = list(texts)
    digest = hashlib.sha256()
    for text in corpus:
        data = text.encode("utf-8", "surrogatepass")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    seed = digest.digest()
    for attempt in itertools.count():
        key = "pf" + hashlib.sha256(seed + attempt.to_bytes(8, "big")).hexdigest()[:16]
        if not any(key in text for text in corpus):
            return key
    raise AssertionError("unreachable")


class Placeholders:
    """One render's placeholder spellings, all carrying the same key."""

    def __init__(self, key: str):
        if not re.fullmatch(r"[0-9A-Za-z]+", key):
            raise ValueError("Placeholder keys are alphanumeric")
        self.key = key
        self.fignum = f"@@{key}:FIGNUM@@"
        self.figref = f"@@{key}:FIGREF@@"
        # A diagram placeholder normally stands alone in its own paragraph.
        self.figures = re.compile(rf"<p>@@{key}:FIGURE(\d+)@@</p>|@@{key}:FIGURE(\d+)@@")
        self.numbers = re.compile(rf"@@{key}:(FIGNUM|FIGREF|TILESET(\d+))@@")
        self.pages = re.compile(rf"@@{key}:PG:(.*?)@@", re.S)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Placeholders) and other.key == self.key

    def __hash__(self) -> int:
        return hash(self.key)

    def __repr__(self) -> str:
        return f"Placeholders({self.key!r})"

    @classmethod
    def unused_in(cls, texts: Iterable[str]) -> Placeholders:
        return cls(new_key(texts))

    def figure(self, index: int) -> str:
        return f"@@{self.key}:FIGURE{index}@@"

    def tileset(self, index: int) -> str:
        return f"@@{self.key}:TILESET{index}@@"

    def page(self, anchor: str) -> str:
        return f"@@{self.key}:PG:{anchor}@@"


# The text half of an injected heading marker. Only the renderer's own marker
# link carries it, so authored link text with the same spelling is kept.
HEADING_MARKER_TEXT = re.compile(
    r'(<a class="pgmark" aria-hidden="true" tabindex="-1" href="#[^"]*">)Zq[0-9a-f]{8}</a>'
)


def strip_marker_text(html: str) -> str:
    """Remove the text half of every injected heading marker."""
    return HEADING_MARKER_TEXT.sub(r"\1</a>", html)
