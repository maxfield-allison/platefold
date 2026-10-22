"""Placement ladder.

This is the decision the whole project exists to make, and it is pure
geometry with no I/O, so it gets the deepest coverage in the suite.

The tests assert *properties of the outcome* — that the figure genuinely fits
the page it was assigned, at the scale that was claimed, and that no smaller
page in the ladder would also have held it — rather than pinning the exact
numbers the current implementation happens to produce. A rewrite of
`plan_figure` that still places figures correctly should keep these passing.
"""

from __future__ import annotations

import pytest

from platefold.config import PX_PER_MM, PageSpec, Settings, make_settings
from platefold.layout import IMAGE_FRAME_PX, plan_figure

# Sub-pixel tolerance. Page dimensions are emitted to CSS at 0.1mm, so exact
# float equality is not a meaningful thing to assert on.
EPS = 1e-3


@pytest.fixture
def s() -> Settings:
    return make_settings("letter")


def spec_by_name(settings: Settings, name: str) -> PageSpec:
    return next(p for p in settings.plates if p.name == name)


# ---------------------------------------------------------------------------
# The core contract: whatever is returned must actually fit.
# ---------------------------------------------------------------------------


def assert_fits(w: float, h: float, plan: dict, s: Settings, frame: float = 0.0) -> None:
    """The planned placement must hold the figure, and any frame drawn outside
    it, at the scale it claims."""
    scale = plan["scale"]
    assert 0 < scale <= 1.0 + EPS
    framed_w, framed_h = w * scale + 2 * frame, h * scale + 2 * frame

    if plan["mode"] == "inline":
        assert framed_w <= s.content_w_px + EPS
        assert framed_h <= s.content_h_px * 0.92 + EPS
        return

    if plan.get("page"):
        page = spec_by_name(s, plan["page"])
        assert framed_w <= page.content_w_px + EPS
        assert framed_h <= page.content_h_px - s.caption_px + EPS
        return

    # Bespoke page: the generated page must contain the figure plus margins
    # and caption room, and must respect the hard width cap.
    page_w, page_h, margin = plan["custom"]
    assert page_w <= s.max_custom_width + EPS
    assert framed_w <= (page_w - 2 * margin) * PX_PER_MM + EPS
    caption_mm = s.caption_px / PX_PER_MM
    assert framed_h / PX_PER_MM <= page_h - 2 * margin - caption_mm + EPS


@pytest.mark.parametrize(
    "w,h",
    [
        (100, 80),  # tiny
        (700, 400),  # comfortably inline
        (1400, 500),  # too wide for the column
        (2600, 900),  # needs A3
        (4700, 1200),  # the pathological mermaid flowchart
        (900, 3000),  # very tall
        (60, 4000),  # absurd aspect ratio
    ],
)
def test_every_placement_actually_fits(w, h, s):
    assert_fits(w, h, plan_figure(w, h, s), s)


@pytest.mark.parametrize("preset", ["letter", "a4", "uniform-letter"])
@pytest.mark.parametrize("w,h", [(300, 200), (1600, 700), (5000, 1500)])
def test_placement_fits_under_every_preset(preset, w, h):
    settings = make_settings(preset)
    assert_fits(w, h, plan_figure(w, h, settings), settings)


@pytest.mark.parametrize(
    "w,h", [(100, 80), (700, 400), (1400, 500), (2600, 900), (4700, 1200), (60, 4000), (20000, 900)]
)
def test_framed_placement_fits_with_its_frame(w, h, s):
    """An image's hairline frame sits outside the picture, so the planner
    leaves room for it: the picture at its scale plus the frame must fit."""
    assert_fits(w, h, plan_figure(w, h, s, frame=IMAGE_FRAME_PX), s, frame=IMAGE_FRAME_PX)


def test_framed_figure_exactly_as_wide_as_the_column_shrinks_by_its_frame(s):
    plan = plan_figure(s.content_w_px, 300, s, frame=IMAGE_FRAME_PX)
    assert plan["mode"] == "inline"
    assert s.content_w_px * plan["scale"] == pytest.approx(s.content_w_px - 2 * IMAGE_FRAME_PX)


def test_framed_figure_with_room_to_spare_keeps_full_scale(s):
    plan = plan_figure(s.content_w_px - 2 * IMAGE_FRAME_PX, 300, s, frame=IMAGE_FRAME_PX)
    assert plan == {"mode": "inline", "scale": 1.0}


@pytest.mark.parametrize("mode", ["online", "print", "tiled"])
@pytest.mark.parametrize(
    "w,h", [(720, 100), (702.614, 300), (1200.5, 700), (2400, 900), (6000, 900), (700, 2000)]
)
def test_whole_pixel_plans_place_whole_pixels_and_still_fit(mode, w, h):
    """Chromium paints an image box on whole CSS px, so image plans round the
    placed width down to a whole pixel; tiles keep their full size."""
    s = make_settings("letter", output_mode=mode)
    exact = plan_figure(w, h, s, frame=IMAGE_FRAME_PX)
    plan = plan_figure(w, h, s, frame=IMAGE_FRAME_PX, whole_px=True)
    if plan["mode"] == "tiled":
        assert plan == exact
        return
    placed = w * plan["scale"]
    assert placed == pytest.approx(round(placed), abs=1e-6)
    assert 0 <= w * exact["scale"] - placed < 1
    if mode == "online":
        assert_fits(w, h, plan, s, frame=IMAGE_FRAME_PX)


@pytest.mark.parametrize("mode", ["print", "tiled"])
def test_print_modes_leave_room_for_the_frame(mode):
    s = make_settings("letter", output_mode=mode)
    room_w = s.content_w_px - 2 * IMAGE_FRAME_PX
    room_h = s.content_h_px - s.caption_px - 2 * IMAGE_FRAME_PX
    for w, h in ((room_w, 300), (300, room_h), (room_w + 0.5, 300), (1400, 2000)):
        plan = plan_figure(w, h, s, frame=IMAGE_FRAME_PX)
        if plan["mode"] == "tiled":
            # Tiles are drawn without a frame and at full size.
            assert plan["scale"] == 1.0
            assert w + 2 * IMAGE_FRAME_PX > s.content_w_px or h > room_h
            continue
        assert w * plan["scale"] <= room_w + EPS
        assert h * plan["scale"] <= room_h + EPS
        if w <= room_w and h <= room_h:
            assert plan["scale"] == 1.0


# ---------------------------------------------------------------------------
# Ladder ordering: never skip a rung that would have worked.
# ---------------------------------------------------------------------------


def test_column_is_preferred_when_it_qualifies(s):
    # Exactly the column width: scale 1.0, no reason to promote it.
    plan = plan_figure(s.content_w_px, 300, s)
    assert plan["mode"] == "inline"
    assert plan["scale"] == pytest.approx(1.0)


def test_promoted_figure_did_not_qualify_for_the_column(s):
    plan = plan_figure(1500, 600, s)
    assert plan["mode"] == "plate"
    column_scale = min(s.content_w_px / 1500, (s.content_h_px * 0.92) / 600, 1.0)
    assert column_scale < s.inline_fit


def test_a3_is_only_used_when_letter_landscape_will_not_do(s):
    """Any figure that lands on A3 must genuinely not fit the smaller plate."""
    plan = plan_figure(1400, 600, s)
    assert plan.get("page") == "plate-a3-l"

    letter = spec_by_name(s, "plate-letter-l")
    letter_scale = min(letter.content_w_px / 1400, (letter.content_h_px - s.caption_px) / 600, 1.0)
    assert letter_scale < s.plate_fit


def test_a_figure_too_wide_for_a3_gets_a_bespoke_page(s):
    """A3 landscape is ~1481px of content; beyond that, cut a page to fit."""
    plan = plan_figure(2600, 900, s)
    assert plan["mode"] == "plate"
    assert plan["page"] is None


def test_bespoke_page_only_after_both_standard_plates_fail(s):
    plan = plan_figure(4700, 1100, s)
    assert plan["mode"] == "plate"
    assert plan["page"] is None
    for spec in s.plates:
        scale = min(spec.content_w_px / 4700, (spec.content_h_px - s.caption_px) / 1100, 1.0)
        assert scale < s.plate_fit


def test_ladder_never_returns_a_larger_page_than_needed(s):
    """Sweep widths; each result must be the first rung that qualifies."""
    rungs = ["inline", "plate-letter-l", "plate-a3-l", None]
    for w in range(200, 5200, 100):
        plan = plan_figure(w, 700, s)
        chosen = "inline" if plan["mode"] == "inline" else plan.get("page")
        chosen_idx = rungs.index(chosen)
        # Every earlier rung must have been rejected.
        if chosen_idx > 0:
            column_scale = min(s.content_w_px / w, (s.content_h_px * 0.92) / 700, 1.0)
            assert column_scale < s.inline_fit
        for spec in s.plates[: max(0, chosen_idx - 1)]:
            scale = min(spec.content_w_px / w, (spec.content_h_px - s.caption_px) / 700, 1.0)
            assert scale < s.plate_fit


# ---------------------------------------------------------------------------
# Threshold boundaries.
# ---------------------------------------------------------------------------


def test_inline_fit_boundary_is_inclusive(s):
    """A figure scaling to exactly inline_fit stays in the column."""
    w = s.content_w_px / s.inline_fit
    plan = plan_figure(w, 100, s)
    assert plan["mode"] == "inline"
    assert plan["scale"] == pytest.approx(s.inline_fit)


def test_just_past_the_inline_boundary_is_promoted(s):
    w = s.content_w_px / s.inline_fit * 1.01
    assert plan_figure(w, 100, s)["mode"] == "plate"


def test_plate_fit_boundary_is_inclusive(s):
    letter = spec_by_name(s, "plate-letter-l")
    w = letter.content_w_px / s.plate_fit
    plan = plan_figure(w, 100, s)
    assert plan.get("page") == "plate-letter-l"
    assert plan["scale"] == pytest.approx(s.plate_fit)


def test_height_alone_can_promote_a_figure(s):
    """A narrow but very tall figure must not be left in the column."""
    tall = s.content_h_px * 0.92 / s.inline_fit * 1.05
    plan = plan_figure(200, tall, s)
    assert plan["mode"] == "plate"


# ---------------------------------------------------------------------------
# The bespoke page.
# ---------------------------------------------------------------------------


def test_bespoke_width_is_capped_and_figure_is_scaled_to_match(s):
    plan = plan_figure(20000, 1000, s)
    page_w, _, margin = plan["custom"]
    assert page_w == pytest.approx(s.max_custom_width)
    # Scaled down to fit inside the capped page, not left overflowing.
    assert 20000 * plan["scale"] <= (page_w - 2 * margin) * PX_PER_MM + EPS


def test_bespoke_page_has_a_floor_height(s):
    """A very wide, very short figure still gets a usable page."""
    _, page_h, _ = plan_figure(5000, 40, s)["custom"]
    assert page_h >= 90.0


def test_bespoke_pages_are_not_wider_than_a_custom_cap_of_zero():
    """max_custom_width = 0 disables bespoke pages entirely."""
    s = make_settings("letter", max_custom_width=0.0)
    plan = plan_figure(9000, 1000, s)
    assert plan["mode"] == "inline"


# ---------------------------------------------------------------------------
# Degenerate and disabled configurations.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("w,h", [(0, 0), (0, 500), (500, 0), (-10, 100), (100, -10)])
def test_degenerate_dimensions_fall_back_to_inline(w, h, s):
    plan = plan_figure(w, h, s)
    assert plan == {"mode": "inline", "scale": 1.0}


def test_uniform_preset_never_leaves_the_text_column():
    s = make_settings("uniform-letter")
    assert s.plates == []
    for w in (500, 1500, 4700, 20000):
        plan = plan_figure(w, 800, s)
        assert plan["mode"] == "inline"
        assert w * plan["scale"] <= s.content_w_px + EPS


def test_empty_plate_list_still_allows_bespoke_pages_to_be_disabled():
    s = make_settings("letter", plates=[], max_custom_width=900.0)
    plan = plan_figure(4700, 900, s)
    # No ladder pages configured, so it shrinks into the column.
    assert plan["mode"] == "inline"


def test_a4_geometry_is_actually_narrower_than_letter():
    """Guards the preset wiring: a4 must change the column, not just the name."""
    letter, a4 = make_settings("letter"), make_settings("a4")
    assert a4.content_w_px < letter.content_w_px
    w = letter.content_w_px  # fits letter exactly
    assert plan_figure(w, 300, letter)["mode"] == "inline"
    assert plan_figure(w, 300, a4)["scale"] < 1.0


def test_raising_inline_fit_pushes_more_figures_onto_plates():
    """The documented tuning knob must behave as documented."""
    relaxed = make_settings("letter", inline_fit=0.5)
    strict = make_settings("letter", inline_fit=0.95)
    w = relaxed.content_w_px / 0.7  # scales to ~0.7
    assert plan_figure(w, 300, relaxed)["mode"] == "inline"
    assert plan_figure(w, 300, strict)["mode"] == "plate"
