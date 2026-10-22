"""Decide where a figure of a given natural size should live.

This is the piece that off-the-shelf exporters don't do. Rather than shrinking
every diagram to the text column, each figure is measured and placed on the
smallest page that keeps it near full size:

    text column  ->  landscape plate  ->  A3 landscape  ->  bespoke page

`plan_figure` returns a dict:
    {"mode": "inline", "scale": float}
    {"mode": "plate", "page": "<css page name>", "scale": float}
    {"mode": "plate", "page": None, "scale": float,
     "custom": (width_mm, height_mm, margin_mm)}
    {"mode": "tiled", "scale": 1.0, "left", "top", "width", "height",
     "overlap", "columns", "rows"}   # whole CSS px; see Settings.tile_frame

`scale` always applies to the picture itself. A figure drawn with a frame
around it passes the frame's width as `frame`; the frame sits outside the
picture, so the room for the picture shrinks by the frame on every side and the
framed figure still fits the page it is given. A figure Chromium paints on the
whole-pixel grid passes `whole_px`, and its scale is lowered just enough that
the placed width is a whole number of CSS px.
"""

from __future__ import annotations

import math

from .config import PX_PER_MM, Settings
from .limits import ResourceLimitError

# Bespoke page dimensions are emitted into CSS at one decimal place. Rounding
# them *up* matters: rounding to nearest can shave up to 0.05mm off the page,
# which clips a figure sized to the full content width.
_MM_DP = 1

# The hairline frame drawn around ordinary image figures, outside the picture.
# Chromium lays out and paints any border thinner than one CSS px at a whole
# pixel, so a 0.5 pt frame took 1 px a side while the planner reserved 0.67 px.
# The frame is specified as the 1 px it actually occupies.
IMAGE_FRAME_PX = 1.0


def _round_up_mm(value: float) -> float:
    factor = 10**_MM_DP
    return math.ceil(value * factor) / factor


def _room(available: float, frame: float) -> float:
    """Space left for the picture once a frame takes `frame` px on both sides."""
    return max(available - 2 * frame, 1.0)


def _whole_px(w: float, plan: dict) -> dict:
    """Lower the scale so the placed width is whole CSS px, never wider.

    Chromium paints an image's box on whole CSS px, so a fractional width prints
    rounded to the nearest pixel, and a box rounded up past the column makes
    Chromium shrink the whole page to fit.
    """
    placed = math.floor(w * plan["scale"] + 1e-6)
    if plan["mode"] != "tiled" and placed >= 1:
        plan["scale"] = min(plan["scale"], placed / w)
    return plan


def plan_figure(
    w: float, h: float, s: Settings, *, frame: float = 0.0, whole_px: bool = False
) -> dict:
    """Plan placement for a figure whose natural size is w x h CSS px.

    `frame` is the width in CSS px of a frame drawn outside the picture on every
    side, and `whole_px` rounds the placed width down to whole CSS px; see the
    module docstring.
    """
    if w <= 0 or h <= 0:
        return {"mode": "inline", "scale": 1.0}
    plan = _plan(w, h, s, frame)
    return _whole_px(w, plan) if whole_px else plan


def _plan(w: float, h: float, s: Settings, frame: float) -> dict:
    if s.output_mode != "online":
        return _print_figure(w, h, s, frame)

    # 1. Does it fit in the running text column at an acceptable size?
    inline_scale = min(
        _room(s.content_w_px, frame) / w, _room(s.content_h_px * 0.92, frame) / h, 1.0
    )
    if inline_scale >= s.inline_fit:
        return {"mode": "inline", "scale": inline_scale}

    # 2. Try each standard plate page in order.
    for spec in s.plates:
        scale = min(
            _room(spec.content_w_px, frame) / w,
            _room(spec.content_h_px - s.caption_px, frame) / h,
            1.0,
        )
        if scale >= s.plate_fit:
            return {"mode": "plate", "page": spec.name, "scale": scale}

    # 3. No plates configured (uniform preset) -> shrink into the column.
    if not s.plates or s.max_custom_width <= 0:
        return {"mode": "inline", "scale": inline_scale}

    # 4. Bespoke page cut to the diagram's own proportions.
    margin = 12.0
    top, right, bottom, left = s.margins_for(margin, margin)
    scale = 1.0
    need_w = (w + 2 * frame) / PX_PER_MM + right + left
    if need_w > s.max_custom_width:
        scale = _room((s.max_custom_width - right - left) * PX_PER_MM, frame) / w
        need_w = s.max_custom_width
    need_h = (h * scale + 2 * frame) / PX_PER_MM + top + bottom + s.caption_px / PX_PER_MM
    return {
        "mode": "plate",
        "page": None,
        "scale": scale,
        "custom": (
            _round_up_mm(min(need_w, s.max_custom_width)),
            _round_up_mm(max(need_h, 90.0)),
            margin,
        ),
    }


def _tile_count(size: float, window: int, overlap: int) -> int:
    """Sheets needed when each one after the first repeats `overlap` px of the last."""
    if size <= window:
        return 1
    return math.ceil((size - overlap) / (window - overlap))


def _print_figure(w: float, h: float, s: Settings, frame: float) -> dict:
    if s.output_mode == "tiled":
        # A figure small enough stays whole, with its frame; tiles have none.
        if w + 2 * frame <= s.content_w_px and h + 2 * frame <= s.content_h_px - s.caption_px:
            return {"mode": "inline", "scale": 1.0}
        left, top, width, height, overlap = s.tile_frame
        columns = _tile_count(w, width, overlap)
        rows = _tile_count(h, height, overlap)
        if columns * rows > 400:
            raise ResourceLimitError("Diagram needs more than 400 tiles; choose larger paper")
        return {
            "mode": "tiled",
            "scale": 1.0,
            "left": left,
            "top": top,
            "width": width,
            "height": height,
            "overlap": overlap,
            "columns": columns,
            "rows": rows,
        }

    scale = min(
        _room(s.content_w_px, frame) / w, _room(s.content_h_px - s.caption_px, frame) / h, 1.0
    )
    if scale >= s.inline_fit:
        return {"mode": "inline", "scale": scale}
    return {"mode": "plate", "page": "print-sheet", "scale": scale}
