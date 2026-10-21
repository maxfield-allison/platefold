"""The hairline frame around image figures sits outside the picture.

An image placed at scale 1.0 must print at its natural size, with the frame
drawn around it rather than over its edges, and a framed figure that fits a
page must keep its whole frame on that page.

Widths are measured from 300 dpi PDFium renders. The test pictures are vertical
stripes; every stripe boundary is located to a fraction of a pixel, and a line
fitted through them gives the picture's printed scale without depending on its
outer edges, where the frame's anti-aliasing mixes in.
"""

from __future__ import annotations

import ctypes
import statistics
from pathlib import Path

import pytest
from PIL import Image

from platefold.cli import main
from platefold.config import make_settings
from platefold.layout import IMAGE_FRAME_PX, plan_figure

pytestmark = pytest.mark.integration

DPI = 300
K = DPI / 96  # device pixels per CSS pixel
STRIPE = 4  # CSS px per stripe at natural size
DARK, LIGHT = (40, 90, 160), (250, 200, 0)
FRAME = (0xD7, 0xDF, 0xE8)  # the default theme's rule colour


def write_png(path: Path, width: int, height: int) -> None:
    image = Image.new("RGB", (width, height))
    image.putdata(
        [DARK if x // STRIPE % 2 == 0 else LIGHT for _ in range(height) for x in range(width)]
    )
    image.save(path)


def write_svg(path: Path, width: float, height: float) -> None:
    stripes = "".join(
        f'<rect x="{x}" width="{STRIPE}" height="{height}"/>'
        for x in range(STRIPE, int(width) - STRIPE + 1, 2 * STRIPE)
    )
    path.write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect width="{width}" height="{height}" '
        'fill="#%02x%02x%02x"/>'
        % DARK
        + '<g fill="#%02x%02x%02x">' % LIGHT
        + f"{stripes}</g></svg>",
        encoding="utf-8",
    )


def export(
    tmp_path: Path, kind: str, width: float, height: float, mode: str, preset: str = "letter"
) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (write_png if kind == "png" else write_svg)(source / f"stripes.{kind}", width, height)
    (source / "stripes.md").write_text(f"![Vertical stripes](stripes.{kind})\n", encoding="utf-8")
    flags = ["--quiet", "--document-language", "en", "--preset", preset]
    assert (
        main(
            [str(source / "stripes.md"), "-o", str(tmp_path / "out"), *flags, "--output-mode", mode]
        )
        == 0
    )
    return tmp_path / "out" / "stripes.pdf"


def render(pdf_path: Path) -> list[Image.Image]:
    import pypdfium2 as pdfium

    pages = []
    with pdfium.PdfDocument(pdf_path) as pdf:
        for index in range(len(pdf)):
            page = pdf[index]
            # A little under 300 dpi keeps float error from adding a stretched row.
            bitmap = page.render(scale=DPI / 72 * (1 - 1e-9))
            pages.append(bitmap.to_pil().convert("RGB"))
            bitmap.close()
            page.close()
    return pages


def placed_picture_widths(pdf_path: Path) -> list[float]:
    """Widths in CSS px of the pictures as the PDF places them: each raster
    image, or the dark background rectangle of an SVG picture."""
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw

    widths = []
    with pdfium.PdfDocument(pdf_path) as pdf:
        for index in range(len(pdf)):
            for item in pdf[index].get_objects(max_depth=4):
                found = item.type == raw.FPDF_PAGEOBJ_IMAGE
                if item.type == raw.FPDF_PAGEOBJ_PATH:
                    rgba = [ctypes.c_uint() for _ in range(4)]
                    raw.FPDFPageObj_GetFillColor(item.raw, *rgba)
                    found = tuple(value.value for value in rgba[:3]) == DARK
                if found:
                    left, _, right, _ = item.get_bounds()
                    widths.append((right - left) / 72 * 96)
    return widths


def distance(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def is_picture(pixel: tuple[int, int, int]) -> bool:
    return distance(pixel, FRAME) > 60 and distance(pixel, (255, 255, 255)) > 60


def run(inside, start: int) -> tuple[int, int]:
    """First and last position of the unbroken run through `start` where `inside` holds."""
    first = last = start
    while inside(first - 1):
        first -= 1
    while inside(last + 1):
        last += 1
    return first, last


def picture_centre(image: Image.Image) -> tuple[int, int] | None:
    """A point in the middle of the picture, or None if the page has none."""
    pixels = image.load()
    hits = [
        (x, y)
        for y in range(0, image.height, 9)
        for x in range(0, image.width, 9)
        if pixels[x, y] == DARK
    ]
    if len(hits) < 20:
        return None
    xs, ys = zip(*hits, strict=True)
    return (min(xs) + max(xs)) // 2, (min(ys) + max(ys)) // 2


def printed_width(image: Image.Image, natural_width: float) -> float:
    """The picture's printed width in device px, from its stripe boundaries."""
    pixels = image.load()
    centre = picture_centre(image)
    assert centre, "no picture on the page"
    column, row = centre
    first, last = run(lambda x: is_picture(pixels[x, row]), column)
    per_px = (last + 1 - first) / natural_width
    boundaries = []
    for index in range(1, int(natural_width) // STRIPE):
        guess = round(first + index * STRIPE * per_px)
        start, end = guess - 5, guess + 6
        # Each pixel's share of the light stripe, from the red channel.
        light = sum(
            min(1.0, max(0.0, (pixels[x, row][0] - DARK[0]) / (LIGHT[0] - DARK[0])))
            for x in range(start, end)
        )
        # Dark to light: the light run ends the window. Light to dark: it starts it.
        boundaries.append((index * STRIPE, end - light if index % 2 else start + light))
    positions, measured = zip(*boundaries, strict=True)
    slope, _ = statistics.linear_regression(positions, measured)
    return slope * natural_width


def frame_on_every_side(image: Image.Image) -> None:
    """Frame-coloured pixels sit just outside the picture on all four sides."""
    pixels = image.load()
    centre = picture_centre(image)
    assert centre, "no picture on the page"
    cx, cy = centre
    left, right = run(lambda x: is_picture(pixels[x, cy]), cx)
    top, bottom = run(lambda y: is_picture(pixels[cx, y]), cy)
    edges = {
        "left": [pixels[left - step, cy] for step in range(1, 5)],
        "right": [pixels[right + step, cy] for step in range(1, 5)],
        "top": [pixels[cx, top - step] for step in range(1, 5)],
        "bottom": [pixels[cx, bottom + step] for step in range(1, 5)],
    }
    for side, outside in edges.items():
        assert any(distance(pixel, FRAME) < 16 for pixel in outside), (side, outside)


# A 600 px picture is 1875 device px at 300 dpi.
@pytest.mark.parametrize("kind", ["svg", "png"])
def test_image_at_scale_one_prints_at_its_natural_width(tmp_path, kind):
    width, height = 600, 240
    settings = make_settings("letter", output_mode="print")
    assert plan_figure(width, height, settings, frame=IMAGE_FRAME_PX)["scale"] == 1.0
    pdf = export(tmp_path, kind, width, height, "print")

    # The PDF places the picture at exactly its natural width.
    (placed,) = placed_picture_widths(pdf)
    assert abs(placed - width) < 0.01, placed

    (page,) = render(pdf)
    measured = printed_width(page, width)
    print(f"{kind}: {measured:.3f} px printed for {width * K:.3f} px natural at {DPI} dpi")
    # PDFium draws a raster image with its edges on whole pixels, which can
    # stretch it by up to a pixel; vector content is drawn where it is placed.
    tolerance = 0.5 if kind == "svg" else 1.0
    assert abs(measured - width * K) < tolerance, measured
    frame_on_every_side(page)


# Figures sized to fill the space a page gives them: the text column in Print,
# a Letter landscape plate in Online, and the full height of a Print plate.
@pytest.mark.parametrize(
    "kind,width,height,mode,page_mm",
    [
        ("svg", make_settings("letter").content_w_px, 300, "print", (215.9, 279.4)),
        ("png", 965, 673, "online", (279.4, 215.9)),
        ("svg", 700, 2000, "print", (215.9, 279.4)),
    ],
)
def test_figure_that_fills_its_page_keeps_its_whole_frame(
    tmp_path, kind, width, height, mode, page_mm
):
    import pypdfium2 as pdfium

    pdf = export(tmp_path, kind, width, height, mode)
    with pdfium.PdfDocument(pdf) as document:
        sizes = [
            tuple(round(points / 72 * 25.4, 1) for points in document[i].get_size())
            for i in range(len(document))
        ]
    # One page, of the planned size: the frame pushed nothing over the edge.
    assert sizes == [page_mm]
    (image,) = render(pdf)
    frame_on_every_side(image)


# Figures across the ladder. Filling the Print column on Letter and A4 is the
# case that used to go wrong: Chromium draws a hairline frame at a whole pixel,
# the framed box overran the column, and Chromium shrank the whole page. Also a
# fractional-width SVG, Online plates and bespoke pages, and a Print plate
# filled by height.
@pytest.mark.parametrize(
    "kind,width,height,mode,preset",
    [
        ("png", 720, 100, "print", "letter"),
        ("svg", 800, 300, "print", "letter"),
        ("svg", make_settings("letter").content_w_px, 300, "print", "letter"),
        ("png", 720, 100, "print", "a4"),
        ("png", 1200, 700, "online", "letter"),
        ("png", 2400, 900, "online", "letter"),
        ("png", 6000, 900, "online", "letter"),
        ("svg", 700, 2000, "print", "letter"),
    ],
)
def test_placed_width_matches_the_plan(tmp_path, kind, width, height, mode, preset):
    settings = make_settings(preset, output_mode=mode)
    plan = plan_figure(width, height, settings, frame=IMAGE_FRAME_PX, whole_px=True)
    planned = width * plan["scale"]
    assert abs(planned - round(planned)) < 1e-6, planned

    (placed,) = placed_picture_widths(export(tmp_path, kind, width, height, mode, preset))
    print(
        f"{kind} {width:.2f} x {height} {mode} {preset}: planned {planned:.4f}, placed {placed:.4f}"
    )
    assert abs(placed - planned) < 0.01, (planned, placed)
