"""Paper selection, precedence and the geometry that must agree with printing."""

import re

import pytest

from platefold.cli import build_settings, parse_args
from platefold.config import PX_PER_MM, TILE_LABEL_PX, make_settings, parse_settings_file
from platefold.layout import plan_figure
from platefold.limits import ResourceLimitError
from platefold.paper import PAPER_SIZES, local_paper
from platefold.server_models import ExportOptions, JobLimits
from platefold.worker import request_from


@pytest.mark.parametrize(
    "locale,paper",
    [
        ("en-US", "letter"),
        ("fr_CA.UTF-8", "letter"),
        ("es-MX", "letter"),
        ("en-GB", "a4"),
        ("de-DE", "a4"),
        ("en", "a4"),
        ("C", "a4"),
        ("", "a4"),
        ("en-u-ca-gregory", "a4"),
        ("zh-Hant-US", "letter"),
        ("English_United States.1252", "letter"),
        ("French_Canada.1252", "letter"),
        ("English_United Kingdom.1252", "a4"),
    ],
)
def test_local_paper_requires_region(locale, paper):
    assert local_paper(locale) == paper


def test_auto_keeps_online_default_but_uses_locale_for_print():
    assert make_settings(locale_hint="de-DE") == make_settings("letter")
    assert make_settings(output_mode=None, locale_hint="de-DE") == make_settings("letter")
    assert make_settings(output_mode="print", locale_hint="de-DE").page_width == 210
    assert make_settings("letter", output_mode="print", locale_hint="de-DE").page_width == 215.9


@pytest.mark.parametrize("paper", PAPER_SIZES)
@pytest.mark.parametrize("margin", ["full", "none", "bottom-right"])
def test_fixed_paper_keeps_oversized_figure_whole(paper, margin):
    s = make_settings(paper, output_mode="print", margin_mode=margin, orientation="landscape")
    assert (s.page_height, s.page_width) == PAPER_SIZES[paper]
    plan = plan_figure(18000, 22000, s)
    assert plan["mode"] == "plate" and plan["page"] == "print-sheet"
    assert 18000 * plan["scale"] <= s.content_w_px + 1e-6
    assert 22000 * plan["scale"] <= s.content_h_px - s.caption_px + 1e-6


@pytest.mark.parametrize(
    "margin,edges",
    [
        ("full", (15, 15, 18, 15)),
        ("none", (0, 0, 0, 0)),
        ("bottom-right", (0, 15, 18, 0)),
    ],
)
def test_tile_grid_covers_the_entire_figure_at_natural_scale(margin, edges):
    s = make_settings("letter", output_mode="tiled", margin_mode=margin)
    assert s.page_margins == edges
    plan = plan_figure(1200, 1400, s)
    assert plan["scale"] == 1
    overlap = round(10 * PX_PER_MM)
    assert plan["overlap"] == overlap
    # Each sheet after the first advances by the window less the overlap, and
    # the last one still reaches the far edge of the figure.
    for size, window, count in (
        (1200, plan["width"], plan["columns"]),
        (1400, plan["height"], plan["rows"]),
    ):
        step = window - overlap
        assert (count - 1) * step + window >= size > (count - 2) * step + window
    # The window stays inside the margins and leaves the label strip below it.
    assert plan["left"] >= edges[3] * PX_PER_MM and plan["top"] >= edges[0] * PX_PER_MM
    assert plan["left"] + plan["width"] <= (215.9 - edges[1]) * PX_PER_MM
    assert plan["top"] + plan["height"] + TILE_LABEL_PX <= (279.4 - edges[2]) * PX_PER_MM


def test_zero_overlap_butts_sheets_edge_to_edge():
    plan = plan_figure(1404, 900, make_settings("letter", output_mode="tiled", tile_overlap=0))
    assert (plan["overlap"], plan["width"], plan["columns"]) == (0, 702, 2)
    plan = plan_figure(1404, 900, make_settings("letter", output_mode="tiled"))
    assert plan["columns"] == 3


@pytest.mark.parametrize("value", [-1, 51, float("nan"), True, "10"])
def test_tile_overlap_is_bounded(value):
    with pytest.raises(ValueError, match="tile_overlap"):
        make_settings(output_mode="tiled", tile_overlap=value)


def test_overlap_must_leave_most_of_the_sheet_new():
    # A5 landscape with wide margins: a 50 mm overlap would repeat most of each sheet.
    with pytest.raises(ValueError, match="tile_overlap"):
        make_settings("a5", output_mode="tiled", margin=40, tile_overlap=50)
    # The same value is harmless outside tiled print.
    make_settings("a5", output_mode="print", margin=40, tile_overlap=50)


def test_tile_overlap_travels_through_every_settings_surface(tmp_path):
    _, overrides = parse_settings_file("layout:\n  tile_overlap: 6\n", "yml")
    assert overrides["tile_overlap"] == 6
    source = tmp_path / "a.md"
    source.write_text("# A\n")
    args = parse_args([str(source), "--output-mode", "tiled", "--tile-overlap", "0"])
    assert build_settings(args, str(tmp_path))[0].tile_overlap == 0
    options = ExportOptions.model_validate({"output_mode": "tiled", "tile_overlap": 4.5})
    assert options.settings(JobLimits()).tile_overlap == 4.5
    with pytest.raises(ValueError):
        ExportOptions.model_validate({"tile_overlap": 60})
    request = request_from(
        {
            "input_path": str(source),
            "output_dir": str(tmp_path / "o"),
            "settings": {"tile_overlap": 3},
        }
    )
    assert request.settings.tile_overlap == 3


@pytest.mark.parametrize(
    "margin,expected",
    [
        # Full margins: trim and overlap marks on all four sides.
        ({"margin_mode": "full"}, {"top", "bottom", "left", "right"}),
        # No margins: only the label strip below the window can carry marks.
        ({"margin_mode": "none"}, {"bottom"}),
        # Narrow top and left margins would put marks where printers can't print.
        ({"margin_edges": [6, 12, 18, 9]}, {"bottom", "right"}),
    ],
)
def test_marks_sit_outside_the_window_and_only_in_printable_margins(margin, expected):
    from platefold.tiles import mark_paths

    s = make_settings("letter", output_mode="tiled", **margin)
    plan = plan_figure(2000, 2000, s)
    x0, y0 = plan["left"], plan["top"]
    x1, y1 = x0 + plan["width"], y0 + plan["height"]
    trims, overlaps = mark_paths(plan, s, 0, 0)
    sides = set()
    for path in trims:
        numbers = [float(n) for n in re.findall(r"-?[\d.]+", path)]
        if "V" in path:  # vertical stroke: M x y V y2
            x, start, end = numbers
            assert x in (x0, x1)
            assert end <= y0 or start >= y1, "mark crosses the window"
            sides.add("top" if end <= y0 else "bottom")
        else:
            y, start, end = numbers[1], numbers[0], numbers[2]
            assert y in (y0, y1)
            assert end <= x0 or start >= x1, "mark crosses the window"
            sides.add("left" if end <= x0 else "right")
    assert sides == expected
    # The first sheet has neighbours to the right and below, so overlap marks
    # appear wherever marks do; the last sheet has none.
    assert overlaps
    assert not mark_paths(plan, s, plan["rows"] - 1, plan["columns"] - 1)[1]


def test_extreme_tile_count_fails_before_allocating_html():
    with pytest.raises(ResourceLimitError, match="400 tiles"):
        plan_figure(100000, 100000, make_settings(output_mode="tiled"))


def test_small_tiled_diagram_is_never_shrunk():
    assert plan_figure(100, 100, make_settings(output_mode="tiled")) == {
        "mode": "inline",
        "scale": 1.0,
    }


@pytest.mark.parametrize(
    "key,value",
    [
        ("output_mode", "poster"),
        ("margin_mode", "left"),
        ("orientation", "upside-down"),
        ("locale_hint", True),
    ],
)
def test_invalid_options_fail(key, value):
    with pytest.raises(ValueError, match=key):
        make_settings(**{key: value})


def test_cli_file_worker_and_api_agree(tmp_path):
    text = "page:\n  preset: a4\n  output_mode: print\n  margin_mode: bottom-right\n  orientation: landscape\n"
    (tmp_path / "platefold.yml").write_text(text)
    source = tmp_path / "sample.md"
    source.write_text("# Sample")
    preset, options = parse_settings_file(text)
    expected = make_settings(preset, **options)
    args = parse_args([str(source)])
    assert build_settings(args, str(tmp_path))[0] == expected
    worker = request_from({"input_path": str(source), "output_dir": str(tmp_path / "out")}).settings
    assert worker.page_width == expected.page_width
    assert worker.page_margins == expected.page_margins
    assert worker.output_mode == expected.output_mode
    api = ExportOptions(
        preset="a4", output_mode="print", margin_mode="bottom-right", orientation="landscape"
    ).settings(JobLimits())
    assert api.page_width == expected.page_width
    assert api.page_margins == expected.page_margins
    args = parse_args(
        [str(source), "--output-mode", "tiled", "--preset", "letter", "--margins", "none"]
    )
    actual = build_settings(args, str(tmp_path))[0]
    assert actual.output_mode == "tiled" and actual.page_height == 215.9
    assert actual.page_margins == (0, 0, 0, 0)


def test_http_auto_uses_client_locale_not_server(monkeypatch):
    monkeypatch.setattr("platefold.paper.locale.getlocale", lambda: ("en_US", "UTF-8"))
    assert ExportOptions(output_mode="print").settings(JobLimits()).page_width == 210
    assert (
        ExportOptions(output_mode="print", locale_hint="en-US").settings(JobLimits()).page_width
        == 215.9
    )


@pytest.mark.parametrize(
    "attributes,expected",
    [
        ('width="12in" height="20in"', (1152, 1920)),
        ('viewBox="10 20 1200 1400"', (1200, 1400)),
        ('width="600" viewBox="0 0 1200 1400"', (600, 700)),
        ('height="700" viewBox="0,0,1200,1400"', (600, 700)),
    ],
)
def test_svg_print_measurement(attributes, expected):
    from platefold.images import svg_dimensions

    assert (
        svg_dimensions(f'<svg xmlns="http://www.w3.org/2000/svg" {attributes}/>'.encode())
        == expected
    )


@pytest.mark.parametrize(
    "source",
    [
        "<svg/>",
        '<svg viewBox="0 0 NaN 10"/>',
        '<svg width="-1" height="2"/>',
        '<!DOCTYPE svg [<!ENTITY x "500">]><svg width="&x;" height="10"/>',
    ],
)
def test_svg_print_requires_bounded_dimensions(source):
    from platefold.images import svg_dimensions

    with pytest.raises(ValueError):
        svg_dimensions(source.encode())


def test_tile_identifiers_do_not_rewrite_visible_labels():
    from types import SimpleNamespace

    from platefold.document import Document
    from platefold.placeholders import Placeholders

    s = make_settings("letter", output_mode="tiled")
    marks = Placeholders("test")
    document = SimpleNamespace(
        _reserve_embedded=lambda *_: None,
        cancel_check=lambda: None,
        s=s,
        tiled_figures=[],
        placeholders=marks,
    )
    svg = '<svg id="figure"><style>#figure {color:red}</style><g id="node"><text>node</text></g><use href="#node"/></svg>'
    plan = {"left": 57, "top": 57, "width": 100, "height": 100, "overlap": 10}
    result = Document._tiles(
        document,
        svg,
        plan | {"rows": 1, "columns": 2, "scale": 1.0},
        "Caption",
        ' role="presentation"',
        "node",
    )
    assert result.count("<text>node</text>") == 2
    assert 'id="node-tile-0-0"' in result and 'href="#node-tile-0-0"' in result
    assert 'id="node-tile-0-1"' in result and "#figure-tile-0-1" in result
    # The per-sheet label is user text too, so it keeps the figure name as written.
    assert result.count(f'<span class="tile-name">Figure {marks.figref} &middot; node</span>') == 2
    assert "Tile 2 of 2: row 1 of 1, column 2 of 2" in result
    # The second sheet starts one window less the overlap into the figure.
    assert 'class="tile-art" style="left:-90px;top:0px"' in result
