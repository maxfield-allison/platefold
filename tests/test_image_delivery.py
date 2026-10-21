"""Images reach the printed PDF as the author sees them, or the export says so.

The loader's output is not the evidence here: each test reads the image
objects Chromium actually placed in the delivered PDF, with the placement
matrix that decides which way up they print.
"""

from __future__ import annotations

import base64
import os
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader
from pypdf.generic import ContentStream
from test_docx_ingest import make_docx
from test_images import UPRIGHT, encoded_bytes, oriented_bytes, quadrant_colours

from platefold.config import make_settings
from platefold.service import ExportRequest, export

pytestmark = pytest.mark.integration

RED = (220, 20, 20)
HEADER = "# Images\n\nLanguage: en\nAuthor: Test Suite\n\n"


def _multiply(first: tuple[float, ...], second: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f = first
    g, h, i, j, k, m = second
    return (
        a * g + b * i,
        a * h + b * j,
        c * g + d * i,
        c * h + d * j,
        e * g + f * i + k,
        e * h + f * j + m,
    )


def placed_images(pdf: Path) -> list[tuple[tuple[float, ...], Image.Image]]:
    """Every image the PDF draws, in drawing order, with its placement matrix."""
    reader = PdfReader(str(pdf))
    found: list[tuple[tuple[float, ...], Image.Image]] = []

    def walk(content, resources, matrix: tuple[float, ...]) -> None:
        saved = []
        for operands, operator in ContentStream(content, reader).operations:
            if operator == b"q":
                saved.append(matrix)
            elif operator == b"Q":
                matrix = saved.pop()
            elif operator == b"cm":
                matrix = _multiply(tuple(float(v) for v in operands), matrix)
            elif operator == b"Do":
                target = resources["/XObject"][operands[0]].get_object()
                if target["/Subtype"] == "/Form":
                    own = tuple(float(v) for v in target.get("/Matrix", [1, 0, 0, 1, 0, 0]))
                    walk(target, target.get("/Resources", resources), _multiply(own, matrix))
                elif target["/Subtype"] == "/Image":
                    found.append((matrix, target.decode_as_image()))

    for page in reader.pages:
        walk(page.get_contents(), page["/Resources"], (1, 0, 0, 1, 0, 0))
    return found


def as_printed(matrix: tuple[float, ...], image: Image.Image) -> Image.Image:
    """The image turned and mirrored as its placement matrix prints it.

    Image space has columns running along (a, b) and the first row at the top
    of the unit square; the page's y axis points up.
    """
    a, b, c, d = matrix[:4]
    flip = Image.Transpose
    if abs(a) >= abs(b):
        if a < 0:
            image = image.transpose(flip.FLIP_LEFT_RIGHT)
        if d < 0:
            image = image.transpose(flip.FLIP_TOP_BOTTOM)
        return image
    image = image.transpose(flip.TRANSPOSE)
    if c > 0:
        image = image.transpose(flip.FLIP_LEFT_RIGHT)
    if b > 0:
        image = image.transpose(flip.FLIP_TOP_BOTTOM)
    return image


def printed_box(matrix: tuple[float, ...]) -> tuple[float, float]:
    """Width and height in points of the area an image covers on the page."""
    a, b, c, d = matrix[:4]
    return abs(a) + abs(c), abs(b) + abs(d)


def run(source: Path, out: Path, dpi: int = 300) -> dict:
    settings = make_settings(document_language="en", image_dpi=dpi)
    result = export(ExportRequest(str(source), str(out), settings))
    assert result.exit_code == 0, result.report
    document = result.report["documents"][0]
    document["status"] = result.status
    return document


def assert_red_figure(pdf: Path, size: tuple[int, int]) -> None:
    [(_matrix, image)] = placed_images(pdf)
    assert image.size == size  # not Chromium's 14 x 16 broken-image icon
    width, height = size
    assert image.convert("RGB").getpixel((width // 2, height // 2)) == RED


def red_tiff() -> bytes:
    # LZW keeps it inside the Word reader's archive compression-ratio limit.
    return encoded_bytes("TIFF", Image.new("RGB", (240, 160), RED), compression="tiff_lzw")


# ---------------------------------------------------------------------------
# Formats Chromium cannot draw are converted, on both sides of the threshold.
# ---------------------------------------------------------------------------

# At 300 DPI the 240 px image is under print resolution and was once passed
# through as TIFF bytes. At 24 DPI it is downsampled to 60 px.
THRESHOLD = pytest.mark.parametrize(("dpi", "size"), [(300, (240, 160)), (24, (60, 40))])


@THRESHOLD
def test_a_tiff_file_prints_on_either_side_of_the_downsampling_threshold(tmp_path, dpi, size):
    source = tmp_path / "src" / "images.md"
    source.parent.mkdir()
    (source.parent / "scan.tiff").write_bytes(red_tiff())
    source.write_text(HEADER + "![A red scan](scan.tiff)\n", encoding="utf-8")

    document = run(source, tmp_path / "out", dpi)

    assert document["status"] == "completed"
    assert document["missing_images"] == [] and document["warnings"] == []
    assert_red_figure(tmp_path / "out" / "images.pdf", size)


@THRESHOLD
def test_a_tiff_carried_by_a_word_file_prints_on_either_side_of_the_threshold(tmp_path, dpi, size):
    body = (
        '<w:p><w:r><w:drawing><wp:inline><wp:docPr id="1" name="Scan" descr="A red scan"/>'
        "<a:graphic><a:graphicData><pic:pic><pic:blipFill><a:blip r:embed="
        '"rIdImage1"/></pic:blipFill></pic:pic></a:graphicData></a:graphic></wp:inline>'
        "</w:drawing></w:r></w:p>"
    )
    source = make_docx(
        tmp_path / "scan.docx",
        body=body,
        image_name="scan.tiff",
        image_content_type="image/tiff",
        image_bytes=red_tiff(),
    )

    document = run(source, tmp_path / "out", dpi)

    assert document["missing_images"] == []
    assert not [w for w in document["warnings"] if w.startswith("IMAGE_")]
    assert_red_figure(tmp_path / "out" / "scan.pdf", size)


def test_an_inline_tiff_data_image_prints(tmp_path):
    uri = "data:image/tiff;base64," + base64.b64encode(red_tiff()).decode("ascii")
    source = tmp_path / "inline.md"
    source.write_text(HEADER + f'<img src="{uri}" alt="A red scan">\n', encoding="utf-8")

    document = run(source, tmp_path / "out")

    assert document["status"] == "completed", document["warnings"]
    assert_red_figure(tmp_path / "out" / "inline.pdf", (240, 160))


# ---------------------------------------------------------------------------
# Whatever Chromium still cannot decode is reported, never silently printed.
# ---------------------------------------------------------------------------


def _all_text(pdf: Path) -> str:
    return "\n".join(page.extract_text() or "" for page in PdfReader(str(pdf)).pages)


@pytest.mark.parametrize("headings", [1, 12], ids=["one-pass", "contents-two-passes"])
def test_an_image_the_browser_cannot_decode_is_a_warning_box_and_reported(tmp_path, headings):
    source = tmp_path / "src" / "broken.md"
    source.parent.mkdir()
    # Pillow cannot read it either, so the loader embeds the bytes as they are.
    (source.parent / "mystery.png").write_bytes(b"\x89PNG but not really " + os.urandom(64))
    sections = "".join(f"## Section {n}\n\nText.\n\n" for n in range(headings))
    source.write_text(HEADER + "![A mystery chart](mystery.png)\n\n" + sections, encoding="utf-8")

    document = run(source, tmp_path / "out")

    assert document["status"] == "completed_with_warnings"
    assert document["missing_images"] == ["mystery.png"]
    [warning] = [w for w in document["warnings"] if w.startswith("IMAGE_UNDECODABLE:")]
    assert "mystery.png" in warning
    pdf = tmp_path / "out" / "broken.pdf"
    assert placed_images(pdf) == []  # no broken-image icon
    text = _all_text(pdf)
    assert "Image could not be displayed" in text and "mystery.png" in text


def test_a_document_printed_in_parts_reports_an_undecodable_image(tmp_path):
    from dataclasses import replace

    from platefold.limits import ResourceLimits

    rows = "".join(f"| {n} | value {n} |\n" for n in range(400))
    source = tmp_path / "src" / "parts.md"
    source.parent.mkdir()
    (source.parent / "mystery.png").write_bytes(b"not an image at all")
    source.write_text(
        HEADER + "![A mystery chart](mystery.png)\n\n| N | Value |\n|---|---|\n" + rows,
        encoding="utf-8",
    )
    limits = replace(ResourceLimits(), max_cells_per_pass=300)
    settings = make_settings(document_language="en", limits=limits)
    result = export(ExportRequest(str(source), str(tmp_path / "out"), settings))

    document = result.report["documents"][0]
    assert document["missing_images"] == ["mystery.png"]
    assert [w for w in document["warnings"] if w.startswith("IMAGE_UNDECODABLE:")]
    assert "Image could not be displayed" in _all_text(tmp_path / "out" / "parts.pdf")


def test_a_tiff_nested_in_an_svg_file_is_reported(tmp_path):
    nested = base64.b64encode(red_tiff()).decode("ascii")
    source = tmp_path / "src" / "nested.md"
    source.parent.mkdir()
    (source.parent / "drawing.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="240" height="160">'
        f'<image href="data:image/tiff;base64,{nested}" width="240" height="160"/></svg>',
        encoding="utf-8",
    )
    source.write_text(HEADER + "![A drawing with a scan](drawing.svg)\n", encoding="utf-8")

    document = run(source, tmp_path / "out")

    assert document["status"] == "completed_with_warnings"
    assert any(w.startswith("IMAGE_UNDECODABLE:") and "TIFF" in w for w in document["warnings"])


# ---------------------------------------------------------------------------
# EXIF orientation: the same upright picture whichever path the image takes.
# ---------------------------------------------------------------------------

ORIENTED = [(fmt, n) for fmt in ("JPEG", "PNG") for n in range(1, 9)]
# Chromium ignores a WebP's EXIF orientation, and so does the loader.
IGNORED = [("WEBP", 3), ("WEBP", 6)]


def test_every_exif_orientation_prints_upright_whether_passed_through_or_resampled(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    lines = []
    for fmt, orientation in ORIENTED + IGNORED:
        name = f"photo-{orientation}.{fmt.lower()}"
        (root / name).write_bytes(oriented_bytes(fmt, orientation))
        lines.append(f"![{fmt} photo tagged {orientation}]({name})\n")
    source = root / "photos.md"
    source.write_text(HEADER + "\n".join(lines), encoding="utf-8")

    # 160 x 240 displayed and placed at natural size: 300 DPI wants 500 px, so
    # the original bytes print; 24 DPI wants 40 px, so each is re-encoded.
    passed = run(source, tmp_path / "passed", 300)
    resampled = run(source, tmp_path / "resampled", 24)
    assert passed["image_bytes_saved"] == 0 and resampled["image_bytes_saved"] > 0
    assert passed["warnings"] == [] and resampled["warnings"] == []

    kept = placed_images(tmp_path / "passed" / "photos.pdf")
    shrunk = placed_images(tmp_path / "resampled" / "photos.pdf")
    assert len(kept) == len(shrunk) == len(ORIENTED + IGNORED)
    for (fmt, orientation), original, small in zip(ORIENTED + IGNORED, kept, shrunk, strict=True):
        label = f"{fmt} orientation {orientation}"
        assert printed_box(original[0]) == pytest.approx(printed_box(small[0]), abs=0.5), label
        big, little = as_printed(*original), as_printed(*small)
        assert little.size[0] < big.size[0], label  # this copy really was resampled
        if (fmt, orientation) in IGNORED:
            stored = Image.open(BytesIO(oriented_bytes(fmt, orientation)))
            portrait = stored.size[1] > stored.size[0]
            assert quadrant_colours(big) == quadrant_colours(little), label
        else:
            portrait = True
            assert quadrant_colours(big) == quadrant_colours(little) == UPRIGHT, label
        width, height = printed_box(original[0])
        assert (height > width) == portrait, label
        assert (big.size[1] > big.size[0]) == (little.size[1] > little.size[0]) == portrait
