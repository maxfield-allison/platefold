"""Authored document semantics, independent of application locale."""

from __future__ import annotations

import re
from html.parser import HTMLParser

_LANGUAGE = re.compile(
    r"(?:[a-z]{2,3}(?:-[a-z]{3}){0,3}|[a-z]{4}|[a-z]{5,8})"
    r"(?:-[a-z]{4})?(?:-(?:[a-z]{2}|[0-9]{3}))?"
    r"(?:-(?:[a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*"
    r"(?:-[0-9a-wy-z](?:-[a-z0-9]{2,8})+)*"
    r"(?:-x(?:-[a-z0-9]{1,8})+)?|x(?:-[a-z0-9]{1,8})+",
    re.I,
)
_GRANDFATHERED = frozenset(
    [
        "en-gb-oed",
        "i-ami",
        "i-bnn",
        "i-default",
        "i-enochian",
        "i-hak",
        "i-klingon",
        "i-lux",
        "i-mingo",
        "i-navajo",
        "i-pwn",
        "i-tao",
        "i-tay",
        "i-tsu",
        "sgn-be-fr",
        "sgn-be-nl",
        "sgn-ch-de",
        "art-lojban",
        "cel-gaulish",
        "no-bok",
        "no-nyn",
        "zh-guoyu",
        "zh-hakka",
        "zh-min",
        "zh-min-nan",
        "zh-xiang",
    ]
)


def valid_language(value: object) -> bool:
    """Accept bounded BCP 47 syntax, without guessing or translating content.

    Registry membership is deliberately not inferred. Private-use tags and
    undetermined language are valid; underscores and markup are not.
    """
    if not isinstance(value, str) or len(value) > 255:
        return False
    if value.lower() in _GRANDFATHERED:
        return True
    if not _LANGUAGE.fullmatch(value):
        return False
    if value.lower().startswith("x-"):
        return True
    extensions = value.lower().split("-x-", 1)[0].split("-")[1:]
    singletons = [part for part in extensions if len(part) == 1]
    return len(singletons) == len(set(singletons))


def document_language(override: str, source: str | None) -> tuple[str, list[tuple[str, str]]]:
    language = override or source
    if isinstance(language, str) and valid_language(language):
        return language, []
    return "und", [
        (
            "DOCUMENT_LANGUAGE",
            # Surface-neutral: this text reaches reports, JSON results and API
            # clients. The desktop substitutes its own wording by code, and the
            # CLI adds the flag that sets it (cli.warning_hints).
            "Document language is not set or was not recognized. Set the document language to en for English, or to the language your document uses. This helps screen readers pronounce the PDF correctly.",
        )
    ]


class _SVGAlternative(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"title", "desc"}:
            self.depth += 1

    def handle_endtag(self, tag):
        if tag in {"title", "desc"}:
            self.depth = max(0, self.depth - 1)
            self.parts.append(" ")

    def handle_data(self, text):
        if self.depth:
            self.parts.append(text)


def diagram_alternative(svg: str) -> str:
    """Read Mermaid's authored accTitle/accDescr from its rendered SVG."""
    parser = _SVGAlternative()
    parser.feed(svg)
    return " ".join("".join(parser.parts).split())


# A table cell with no text: its tags and whitespace, until the closing tag.
# Images and SVG count as content. Each alternative starts with a different
# character, so the scan stays linear on very large tables.
_EMPTY_CELL = re.compile(
    r"(<t([dh])\b[^>]*>)((?:\s|<(?!/?t[dh]\b|img\b|svg\b|math\b|object\b)[^>]*>)*)(</t\2>)",
    re.I,
)


def fill_empty_cells(body: str) -> str:
    """Put a no-break space in every empty table cell.

    Chromium leaves a cell with no content out of the tagged PDF, so its row
    has fewer cells than the others and the column order is lost to a screen
    reader (PDF/UA-1 rule 7.2-43). A no-break space draws nothing visible but
    is painted as text, so the cell gets a tagged run. A zero-width space is
    not enough: Chromium dropped it from some tables. A row whose cells are
    all empty gets one line of height, like any other row, instead of
    collapsing to its padding.
    """
    return _EMPTY_CELL.sub(lambda m: f"{m[1]}{m[3]}&#160;{m[4]}", body)
