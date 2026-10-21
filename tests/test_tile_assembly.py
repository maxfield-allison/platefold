"""Digital assembly of tiled print output.

Physical printing can't run in CI, so this proves the geometry the paper would
rely on: every tile is rasterised at 300 dpi, located on its sheet by its
printed trim marks alone, and joined to its neighbours through the overlap
marks. The joined result is compared with a single-sheet print of the same
figure at the same scale. Alignment must be better than one 300 dpi pixel.

PDFium snaps axis-aligned filled rectangles to whole pixels, so edges between
filled cells can't be located more finely than a pixel. Thin stroked guide
lines are anti-aliased, and their ink-weighted centres give sub-pixel
positions; the fills are used for the whole-picture comparison.
"""

from __future__ import annotations

import itertools
import json
import statistics
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from platefold.cli import main
from platefold.config import make_settings
from platefold.layout import plan_figure
from platefold.tiles import MARK_GAP, MARK_LENGTH

pytestmark = pytest.mark.integration

DPI = 300
K = DPI / 96  # device pixels per CSS pixel
CELL = 100  # CSS px
COLUMNS, ROWS = 14, 16  # a 1400 x 1600 CSS px figure
LINE = 50  # guide lines run through every cell centre
LINE_INK = (16, 16, 16)
SEARCH = 8  # device px either side of an expected guide line


def cell_colour(column: int, row: int) -> tuple[int, int, int]:
    """A distinct flat colour for every cell, so no shift can alias onto a match."""
    index = row * COLUMNS + column
    return (40 + index % 8 * 25, 40 + index // 8 % 7 * 28, 40 + index // 56 * 40)


def write_figure(root: Path) -> Path:
    cells = "".join(
        f'<rect x="{c * CELL}" y="{r * CELL}" width="{CELL}" height="{CELL}" '
        'fill="#%02x%02x%02x"/>' % cell_colour(c, r)
        for r in range(ROWS)
        for c in range(COLUMNS)
    )
    width, height = COLUMNS * CELL, ROWS * CELL
    lines = "".join(f"M{c * CELL + LINE} 0V{height}" for c in range(COLUMNS)) + "".join(
        f"M0 {r * CELL + LINE}H{width}" for r in range(ROWS)
    )
    root.mkdir(parents=True, exist_ok=True)
    (root / "grid.svg").write_text(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">{cells}'
        f'<path d="{lines}" stroke="#%02x%02x%02x" stroke-width="2" fill="none"/></svg>' % LINE_INK
    )
    # The reference sheet keeps the hairline frame ordinary image figures get.
    # It is drawn outside the picture, so both prints show it at 1400 x 1600 px.
    source = root / "grid.md"
    source.write_text("![Grid of distinct cells](grid.svg)\n")
    return source


def render(pdf_path: Path) -> list[Image.Image]:
    import pypdfium2 as pdfium

    images = []
    with pdfium.PdfDocument(pdf_path) as pdf:
        for index in range(len(pdf)):
            page = pdf[index]
            # PDFium stretches the page to fill a bitmap whose size is rounded
            # up. Sheets here are whole 300 dpi pixels, and the tiny scale
            # reduction keeps float error from adding a stretched extra row.
            for points in page.get_size():
                assert abs(points * DPI / 72 - round(points * DPI / 72)) < 1e-3, points
            bitmap = page.render(scale=DPI / 72 * (1 - 1e-9))
            images.append(bitmap.to_pil().convert("RGB"))
            bitmap.close()
            page.close()
    return images


def mark_centre(image: Image.Image, box: tuple[int, int, int, int], axis: int) -> float:
    """Ink-weighted centre of a printed mark inside `box`, along x (0) or y (1)."""
    crop = image.crop(box).convert("L")
    pixels = crop.load()
    total = moment = 0.0
    for y in range(crop.height):
        for x in range(crop.width):
            ink = 255 - pixels[x, y]
            if ink > 16:
                total += ink
                moment += ink * ((x, y)[axis] + 0.5)
    assert total, f"no mark found in {box}"
    return box[axis] + moment / total


def line_centre(image: Image.Image, near: float, fixed: int, axis: int) -> float:
    """Sub-pixel centre of the guide line crossing `fixed` close to `near`.

    The line crosses the middle of a flat cell, so the pixels at both ends of the
    search window give its background; each pixel's departure from that colour
    is its ink.
    """
    pixels = image.load()

    def pixel(position: int) -> tuple[int, int, int]:
        return pixels[(position, fixed) if axis == 0 else (fixed, position)]

    # Re-centre on the darkest nearby pixel, so an approximate guess still puts
    # clean background at both ends of the window.
    darkest = min(
        range(round(near) - SEARCH, round(near) + SEARCH + 1),
        key=lambda position: sum(
            abs(value - ink) for value, ink in zip(pixel(position), LINE_INK, strict=True)
        ),
    )
    start = darkest - SEARCH

    def at(offset: int) -> tuple[int, int, int]:
        return pixel(start + offset)

    background = [(a + b) / 2 for a, b in zip(at(0), at(2 * SEARCH), strict=True)]
    total = moment = 0.0
    for offset in range(2 * SEARCH + 1):
        ink = sum(abs(value - base) for value, base in zip(at(offset), background, strict=True))
        if ink > 12:
            total += ink
            moment += ink * (start + offset + 0.5)
    assert total, f"no guide line near {near} on axis {axis}"
    return moment / total


def reference_origin(reference: Image.Image) -> tuple[float, float]:
    """Where the single-sheet print put the figure, measured from its guide lines."""
    target = cell_colour(0, 0)
    pixels = reference.load()
    coarse = next(
        (x, y)
        for y in range(reference.height)
        for x in range(0, reference.width, 2)
        if pixels[x, y] == target
    )
    origin = []
    for axis in (0, 1):
        cross = round(coarse[1 - axis] + (CELL / 4) * K)
        offsets = [
            line_centre(reference, coarse[axis] + (i * CELL + LINE) * K, cross, axis)
            - (i * CELL + LINE) * K
            for i in range(COLUMNS if axis == 0 else ROWS)
        ]
        # Same scale: every line sits a whole number of cells from the first.
        assert max(offsets) - min(offsets) < 0.1, offsets
        origin.append(statistics.fmean(offsets))
    return origin[0], origin[1]


def export_pair(tmp_path: Path, capsys, overlap: float) -> tuple[dict, Path, Path]:
    """The tiled export under test, and a single-sheet print of the same figure."""
    source = write_figure(tmp_path / "tiled-source")
    reference_source = write_figure(tmp_path / "reference-source")
    tiled_out, single_out = tmp_path / "tiled", tmp_path / "single"
    flags = ["--quiet", "--document-language", "en"]
    tiled = ["--json", "--output-mode", "tiled", "--preset", "letter"]
    code = main([str(source), "-o", str(tiled_out), *flags, *tiled, "--tile-overlap", str(overlap)])
    assert code == 0
    result = json.loads(capsys.readouterr().out)
    # One sheet big enough for the whole figure at full size is the reference:
    # 17 x 19 inches, so it too is a whole number of 300 dpi pixels.
    single = ["--output-mode", "print", "--page-width", "431.8", "--page-height", "482.6"]
    code = main([str(reference_source), "-o", str(single_out), *flags, *single])
    assert code == 0
    return result, tiled_out / "grid.pdf", single_out / "grid.pdf"


def vertical_mark(image: Image.Image, x: float, window_top: float) -> float:
    """Centre of a vertical mark in the margin above the window, near `x`."""
    near, far = MARK_GAP * K, (MARK_GAP + MARK_LENGTH) * K
    box = (int(x - 12), int(window_top - far + 2), int(x + 12), int(window_top - near - 2))
    return mark_centre(image, box, 0)


def horizontal_mark(image: Image.Image, y: float, window_left: float) -> float:
    """Centre of a horizontal mark in the margin left of the window, near `y`."""
    near, far = MARK_GAP * K, (MARK_GAP + MARK_LENGTH) * K
    box = (int(window_left - far + 2), int(y - 12), int(window_left - near - 2), int(y + 12))
    return mark_centre(image, box, 1)


def locate_windows(tiles: list[Image.Image], plan: dict) -> dict:
    """Each sheet's window corner, found from its printed trim marks only."""
    origins = {}
    guess_x, guess_y = plan["left"] * K, plan["top"] * K
    for index, image in enumerate(tiles):
        x = vertical_mark(image, guess_x, guess_y)
        y = horizontal_mark(image, guess_y, guess_x)
        # Marks sit on the window edge; the window starts at the planned corner.
        assert abs(x - guess_x) < 1 and abs(y - guess_y) < 1, (index, x, y)
        origins[divmod(index, plan["columns"])] = (x, y)
    return origins


def place_sheets(tiles: list[Image.Image], origins: dict, plan: dict) -> dict:
    """Lay each sheet where its left and upper neighbours' marks say it goes.

    A sheet's trimmed edge goes on its neighbour's overlap mark, or on the
    neighbour's trim mark when sheets butt with no overlap. Positions are in
    figure CSS px and are chained from sheet to sheet, as they would be on a
    table, so an error in any mark would accumulate.
    """
    columns, overlap = plan["columns"], plan["overlap"]
    placed = {(0, 0): (0.0, 0.0)}
    for row in range(plan["rows"]):
        for column in range(columns):
            if (row, column) in placed:
                continue
            if column:
                x0, y0 = origins[row, column - 1]
                image = tiles[row * columns + column - 1]
                edge = vertical_mark(image, x0 + (plan["width"] - overlap) * K, y0)
                left, top = placed[row, column - 1]
                placed[row, column] = (left + (edge - x0) / K, top)
            else:
                x0, y0 = origins[row - 1, 0]
                image = tiles[(row - 1) * columns]
                edge = horizontal_mark(image, y0 + (plan["height"] - overlap) * K, x0)
                left, top = placed[row - 1, 0]
                placed[row, 0] = (left, top + (edge - y0) / K)
    return placed


def worst_misalignment(
    tiles: list[Image.Image], origins: dict, placed: dict, plan: dict, reference: Image.Image
) -> tuple[float, int]:
    """Largest distance, in 300 dpi pixels, between a guide line on a placed
    sheet and the same line on the reference print."""
    ref_origin = reference_origin(reference)
    windows = (plan["width"], plan["height"])
    worst, measured = 0.0, 0
    for (row, column), origin in origins.items():
        image = tiles[row * plan["columns"] + column]
        offsets = placed[row, column]
        for axis, count, crossings in ((0, COLUMNS, ROWS), (1, ROWS, COLUMNS)):
            other = 1 - axis
            for line, cell in itertools.product(range(count), range(crossings)):
                local = line * CELL + LINE - offsets[axis]
                # A quarter-cell in: between a cell edge and the crossing line.
                cross = cell * CELL + CELL / 4 - offsets[other]
                if not (4 <= local <= windows[axis] - 4 and 0 <= cross <= windows[other]):
                    continue
                fixed = round(origin[other] + cross * K)
                tile_at = line_centre(image, origin[axis] + local * K, fixed, axis)
                figure_at = (tile_at - origin[axis]) / K + offsets[axis]
                fixed = round(ref_origin[other] + (cell * CELL + CELL / 4) * K)
                expected = ref_origin[axis] + (line * CELL + LINE) * K
                ref_at = line_centre(reference, expected, fixed, axis)
                worst = max(worst, abs(figure_at - (ref_at - ref_origin[axis]) / K) * K)
                measured += 1
    return worst, measured


def assembled_difference(
    tiles: list[Image.Image], origins: dict, placed: dict, plan: dict, reference: Image.Image
) -> float:
    """Paste every sheet's window where it was placed; return the share of
    pixels that differ from the reference print."""
    width, height = round(COLUMNS * CELL * K), round(ROWS * CELL * K)
    assembled = Image.new("RGB", (width, height), "white")
    for (row, column), (x0, y0) in origins.items():
        image = tiles[row * plan["columns"] + column]
        left, top = (round(value * K) for value in placed[row, column])
        crop_w = min(round(plan["width"] * K), width - left)
        crop_h = min(round(plan["height"] * K), height - top)
        piece = image.crop((round(x0), round(y0), round(x0) + crop_w, round(y0) + crop_h))
        assembled.paste(piece, (left, top))
    ref_x, ref_y = (round(value) for value in reference_origin(reference))
    expected = reference.crop((ref_x, ref_y, ref_x + width, ref_y + height))
    difference = (
        ImageChops.difference(assembled, expected)
        .convert("L")
        .point(lambda value: 255 if value > 48 else 0)
    )
    return difference.histogram()[255] / (width * height)


# Letter portrait, full margins: a 702 x 902 px window. A 10 mm (38 px) overlap
# needs a third column for 1400 px; butted sheets (0 mm) cover it in two.
@pytest.mark.parametrize("overlap,grid", [(10, (3, 2)), (0, (2, 2))])
def test_tiles_reassemble_to_the_single_sheet_print_within_a_pixel(tmp_path, capsys, overlap, grid):
    result, tiled_pdf, single_pdf = export_pair(tmp_path, capsys, overlap)
    settings = make_settings("letter", output_mode="tiled", tile_overlap=overlap)
    plan = plan_figure(COLUMNS * CELL, ROWS * CELL, settings)
    columns, rows = plan["columns"], plan["rows"]
    assert (columns, rows) == grid

    # The report and the --json result describe the same tiling.
    tiled = [
        {"figure": 1, "columns": columns, "rows": rows, "sheets": columns * rows, "scale": 1.0}
    ]
    assert result["report"]["documents"][0]["tiled_figures"] == tiled
    assert result["report"]["settings"]["tile_overlap_mm"] == overlap
    report = json.loads((tiled_pdf.parent / "_export-report.json").read_text(encoding="utf-8"))
    assert report["documents"][0]["tiled_figures"] == tiled

    pages = render(tiled_pdf)
    (reference,) = render(single_pdf)
    # The page before the tiles carries the caption and assembly note.
    assert len(pages) == 1 + columns * rows
    tiles = pages[1:]

    origins = locate_windows(tiles, plan)
    placed = place_sheets(tiles, origins, plan)
    # The marks put every sheet where the plan says it starts in the figure.
    step_x, step_y = plan["width"] - plan["overlap"], plan["height"] - plan["overlap"]
    for (row, column), (left, top) in placed.items():
        assert abs(left - column * step_x) * K < 1 and abs(top - row * step_y) * K < 1

    worst, measured = worst_misalignment(tiles, origins, placed, plan, reference)
    print(f"overlap {overlap} mm: {measured} line crossings, worst {worst:.3f} px at {DPI} dpi")
    assert measured > 200
    # The bar is one pixel; hold it to half so a regression shows early.
    assert worst < 0.5, f"worst tile-to-reference misalignment {worst:.3f} px at {DPI} dpi"

    wrong = assembled_difference(tiles, origins, placed, plan, reference)
    print(f"assembled pixels differing from the reference: {wrong:.4%}")
    # Only anti-aliased boundaries may differ after whole-pixel pasting.
    assert wrong < 0.02
