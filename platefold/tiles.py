"""Sheet furniture for tiled print: trim marks, overlap marks and tile labels.

Each tile sheet is a zero-margin page. The tile window sits where the page
margins would put the content area (`Settings.tile_frame`), with a label strip
below it. Marks live outside the window, in the margins and the label strip, so
they never cover the figure:

- trim marks continue each window edge outward from its corners; cutting along
  them removes the margin from a sheet;
- overlap marks show where a neighbouring sheet's trimmed edge belongs. The
  figure strip between that mark and the window edge is printed on both sheets.

Every coordinate is a whole CSS pixel so Chromium paints the window, the figure
offset and the marks on one grid.
"""

from __future__ import annotations

import html
import math

from .config import PX_PER_MM, TILE_LABEL_PX, Settings

MARK_GAP = 6  # px between the window edge and the start of a mark (1.6 mm)
MARK_LENGTH = 19  # px (5 mm)
MARK_RADIUS = 7  # px, the overlap mark's circle (1.9 mm)
# A margin narrower than this (10 mm) gets no marks: printers commonly cannot
# print the outer 4-6 mm of a sheet, and a clipped mark is worse than none.
MARK_MIN_MARGIN = 38
MARK_STROKE = 0.75  # px; about 0.2 mm


def sheet_size(s: Settings) -> tuple[int, int]:
    """Whole-pixel sheet box that always fits inside the physical page."""
    return (
        math.floor(s.page_width * PX_PER_MM + 1e-6),
        math.floor(s.page_height * PX_PER_MM + 1e-6),
    )


def _circle(cx: float, cy: float) -> str:
    r = MARK_RADIUS
    return f"M{cx - r} {cy}a{r} {r} 0 1 0 {2 * r} 0a{r} {r} 0 1 0 {-2 * r} 0"


def _mark_bands(plan: dict, s: Settings) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Where marks may go: spans above/below the window, then left/right of it.

    Below the window is the label strip, which is always printable. The other
    three sides need a margin wide enough to print in.
    """
    sheet_w, _ = sheet_size(s)
    x0, y0 = plan["left"], plan["top"]
    x1, y1 = x0 + plan["width"], y0 + plan["height"]
    near, far = MARK_GAP, MARK_GAP + MARK_LENGTH
    across = [(y1 + near, y1 + far)]
    if y0 >= MARK_MIN_MARGIN:
        across.append((y0 - far, y0 - near))
    beside = []
    if x0 >= MARK_MIN_MARGIN:
        beside.append((x0 - far, x0 - near))
    if sheet_w - x1 >= MARK_MIN_MARGIN:
        beside.append((x1 + near, x1 + far))
    return across, beside


def mark_paths(plan: dict, s: Settings, row: int, column: int) -> tuple[list[str], list[str]]:
    """SVG path data for trim marks and overlap marks on one sheet."""
    x0, y0 = plan["left"], plan["top"]
    x1, y1 = x0 + plan["width"], y0 + plan["height"]
    overlap = plan["overlap"]
    right = column + 1 < plan["columns"] and overlap > 0
    below = row + 1 < plan["rows"] and overlap > 0
    across, beside = _mark_bands(plan, s)
    trims = [f"M{x} {start}V{end}" for start, end in across for x in (x0, x1)]
    trims += [f"M{start} {y}H{end}" for start, end in beside for y in (y0, y1)]
    overlaps: list[str] = []
    if right:
        x = x1 - overlap
        for start, end in across:
            overlaps += [f"M{x} {start}V{end}", _circle(x, (start + end) / 2)]
    if below:
        y = y1 - overlap
        for start, end in beside:
            overlaps += [f"M{start} {y}H{end}", _circle((start + end) / 2, y)]
    return trims, overlaps


def marks_svg(plan: dict, s: Settings, row: int, column: int) -> str:
    sheet_w, sheet_h = sheet_size(s)
    trims, overlaps = mark_paths(plan, s, row, column)
    color = html.escape(s.theme.ink, quote=True)
    paths = "".join(f'<path d="{"".join(group)}"/>' for group in (trims, overlaps) if group)
    return (
        f'<svg class="tile-marks" aria-hidden="true" width="{sheet_w}" height="{sheet_h}" '
        f'viewBox="0 0 {sheet_w} {sheet_h}" fill="none" stroke="{color}" '
        f'stroke-width="{MARK_STROKE}">{paths}</svg>'
    )


def label_html(plan: dict, row: int, column: int, figure: str, name: str) -> str:
    """One-line label under the window: figure first, sheet position last.

    `figure` is the figure-number placeholder; `name` is already escaped. The
    label is inset past the overlap marks so it can't collide with them.
    """
    inset = plan["overlap"] + MARK_GAP + MARK_RADIUS
    total = plan["columns"] * plan["rows"]
    number = row * plan["columns"] + column + 1
    title = f"Figure {figure}" + (f" &middot; {name}" if name else "")
    where = (
        f"Tile {number} of {total}: row {row + 1} of {plan['rows']}, "
        f"column {column + 1} of {plan['columns']}"
    )
    return (
        f'<p class="tile-label" aria-hidden="true" style="left:{plan["left"] + inset}px;'
        f"top:{plan['top'] + plan['height']}px;width:{plan['width'] - 2 * inset}px;"
        f'height:{TILE_LABEL_PX}px"><span class="tile-name">{title}</span>'
        f'<span class="tile-where">{where}</span></p>'
    )


def assembly_note(plan: dict, overlap_mm: float) -> str:
    total = plan["columns"] * plan["rows"]
    joins = (
        f" Neighbouring sheets share a {overlap_mm:g} mm strip of the figure."
        if plan["overlap"] and total > 1
        else ""
    )
    return (
        f'<p aria-hidden="true">{plan["columns"]} columns by {plan["rows"]} rows, '
        f"{total} sheets.{joins} Print at actual size (100%). Assemble left to right, then "
        "top to bottom; each sheet is labelled with its row and column.</p>"
    )
