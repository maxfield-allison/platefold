"""Detect pages that contain nothing but the running header and footer.

Blank pages are this project's most likely regression: a figure that overflows
its page pushes an empty one ahead of it, and nothing else in the pipeline
notices. Kept in the package rather than in `tools/` so the test suite can
assert on it directly.

pdfplumber is a development dependency, so it is imported lazily — the export
path never needs it.
"""

from __future__ import annotations

import glob
import os

# Fraction of page height trimmed top and bottom before looking for content,
# so the running header and footer don't count as "something on the page".
_HEADER_BAND = 0.045

# A page with less than this much text and fewer than this many drawn objects
# is considered blank.
_MIN_TEXT = 5
_MIN_DRAWN = 3


def blank_pages(pdf_path: str) -> list[int]:
    """1-based page numbers that appear blank in `pdf_path`."""
    import pdfplumber

    empties: list[int] = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            h = page.height
            band = page.crop((0, h * _HEADER_BAND, page.width, h * (1 - _HEADER_BAND)))
            text = (band.extract_text() or "").strip()
            drawn = len(band.images) + len(band.curves) + len(band.rects)
            if len(text) < _MIN_TEXT and drawn < _MIN_DRAWN:
                empties.append(i + 1)
    return empties


def scan(root: str) -> dict[str, list[int]]:
    """Blank pages for every PDF under `root`, keyed by relative path."""
    found: dict[str, list[int]] = {}
    for path in sorted(glob.glob(os.path.join(root, "**", "*.pdf"), recursive=True)):
        empties = blank_pages(path)
        if empties:
            found[os.path.relpath(path, root)] = empties
    return found
