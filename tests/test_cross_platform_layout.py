"""The same document paginates the same way on Linux, Windows and macOS.

The fixture uses only the bundled typefaces, so every platform's browser shapes
the same font files. The expected layout below was recorded from a real export
and is asserted unchanged on each operating system the suite runs on. Agreement
between platforms follows from each one matching the same record.

If an assertion here fails on one platform only, the export depends on
something that platform supplies, such as a system font or a rounding
difference in its text rasteriser. That is a defect to explain, not a number
to update. Update the record only for an intended layout change, and then on
every platform at once.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pypdf import PdfReader

pytestmark = pytest.mark.integration

PT_PER_MM = 72.0 / 25.4

LETTER = (216, 279)

# (width, height) of each page in whole millimetres. Page 4 is the plate the
# wide diagram is promoted onto; its size follows the measured label widths.
EXPECTED_PAGES = [LETTER, LETTER, LETTER, (686, 90), LETTER]

# Each heading and the page it starts on.
EXPECTED_OUTLINE = [
    ("1. Running text", 2),
    ("2. Lists and a table", 2),
    ("3. Code", 3),
    ("4. A diagram that fits the column", 3),
    ("5. A diagram that needs its own page", 3),
    ("6. Closing text", 5),
    ("7. Appendix", 5),
    ("7.1 Repeated measure", 5),
    ("7.2 Numbers", 5),
]

BUNDLED_FACES = {"Sora-Regular", "Sora-Bold", "JetBrainsMono-Regular"}


def _page_sizes(reader: PdfReader) -> list[tuple[int, int]]:
    return [
        (round(float(p.mediabox.width) / PT_PER_MM), round(float(p.mediabox.height) / PT_PER_MM))
        for p in reader.pages
    ]


def _outline(reader: PdfReader) -> list[tuple[str, int]]:
    found: list[tuple[str, int]] = []

    def walk(items):
        for item in items:
            if isinstance(item, list):
                walk(item)
            else:
                found.append((item.title, reader.get_destination_page_number(item) + 1))

    walk(reader.outline)
    return found


def _faces_by_page(pdf: Path) -> dict[int, set[str]]:
    import pdfplumber

    faces: dict[int, set[str]] = {}
    with pdfplumber.open(pdf) as document:
        for number, page in enumerate(document.pages, 1):
            # Drop the six-letter subset tag: "ABCDEF+Sora-Regular" is Sora-Regular.
            faces[number] = {re.sub(r"^[A-Z]{6}\+", "", c["fontname"]) for c in page.chars}
    return faces


@pytest.fixture(scope="module")
def exported(tmp_path_factory) -> Path:
    from platefold.cli import main

    source = Path(__file__).parent / "fixtures" / "cross_platform_layout.md"
    out = tmp_path_factory.mktemp("cross-platform")
    assert main([str(source), "-o", str(out), "--quiet"]) == 0
    return out / "cross_platform_layout.pdf"


def test_page_count_and_sizes_match_the_record(exported):
    sizes = _page_sizes(PdfReader(str(exported)))
    assert len(sizes) == len(EXPECTED_PAGES), f"page count {len(sizes)}: {sizes}"
    assert sizes == EXPECTED_PAGES


def test_every_heading_starts_on_the_recorded_page(exported):
    assert _outline(PdfReader(str(exported))) == EXPECTED_OUTLINE


def test_only_the_bundled_faces_reach_the_pdf(exported):
    """A system font anywhere, including the running header and footer, would
    make the export look different on each operating system."""
    faces = _faces_by_page(exported)
    stray = {page: found - BUNDLED_FACES for page, found in faces.items() if found - BUNDLED_FACES}
    assert not stray, f"faces from outside the package, by page: {stray}"
    assert set().union(*faces.values()) == BUNDLED_FACES
