"""Advanced overrides must survive every entry point and share print geometry."""

import json

import pytest

from platefold.cli import build_settings, parse_args
from platefold.config import PX_PER_MM, make_settings, parse_settings_file
from platefold.layout import plan_figure
from platefold.server_models import ExportOptions, JobLimits
from platefold.style import build_css
from platefold.worker import request_from

OPTIONS = {
    "page_width": 230.5,
    "page_height": 340.25,
    "margin_edges": [2, 12, 19, 5],
    "inline_fit": 0.95,
    "plate_fit": 0.9,
    "max_custom_width": 750,
    "caption_px": 40,
    "body_size_pt": 12,
    "mermaid_font_px": 18,
    "mermaid_wrap_px": 300,
    "image_dpi": 240,
}


def test_advanced_values_override_presets_across_transports(tmp_path):
    source = tmp_path / "note.md"
    source.write_text("# Note")
    text = json.dumps(
        {
            "page": {"preset": "a4", "width": 200, "margin_edges": [1, 1, 1, 1]},
            "typography": {"body_size_pt": 9},
        }
    )
    (tmp_path / "platefold.json").write_text(text)
    worker = request_from(
        {
            "input_path": str(source),
            "output_dir": str(tmp_path / "out"),
            "settings": {"preset": "letter", "orientation": "landscape", **OPTIONS},
        }
    ).settings
    flags = [str(source), "--preset", "letter", "--orientation", "landscape"]
    for key, value in OPTIONS.items():
        flags.append("--dpi" if key == "image_dpi" else "--" + key.replace("_", "-"))
        flags.extend(str(v) for v in (value if isinstance(value, list) else [value]))
    cli = build_settings(parse_args(flags), str(tmp_path))[0]
    api = ExportOptions.model_validate_json(
        json.dumps(
            {
                "preset": "letter",
                "orientation": "landscape",
                **OPTIONS,
            }
        )
    ).settings(JobLimits())
    for settings in (worker, cli, api):
        assert (settings.page_width, settings.page_height) == (340.25, 230.5)
        for key, value in OPTIONS.items():
            if not key.startswith("page_"):
                assert getattr(settings, key) == value
    inherited = request_from(
        {"input_path": str(source), "output_dir": str(tmp_path / "out")}
    ).settings
    assert inherited.page_width == 200 and inherited.body_size_pt == 9
    assert inherited.margin_edges == [1, 1, 1, 1]


@pytest.mark.parametrize(
    "format,text",
    [
        ("json", '{"page":{"margin_edges":[0,8,12,0]}}'),
        ("yaml", "page:\n  margin_edges: [0, 8, 12, 0]\n"),
    ],
)
def test_exact_margins_in_all_settings_formats(format, text):
    preset, options = parse_settings_file(text, format)
    assert make_settings(preset or "auto", **options).page_margins == (0, 8, 12, 0)


@pytest.mark.parametrize(
    "edges",
    [
        [1, 2, 3],
        [0] * 5,
        "1,2,3,4",
        [True, 2, 3, 4],
        [-1, 2, 3, 4],
        [float("nan"), 2, 3, 4],
        [0, 0, float("inf"), 0],
    ],
)
def test_invalid_exact_margins_fail_before_layout(edges):
    with pytest.raises(ValueError, match="margin_edges"):
        make_settings(margin_edges=edges)
    with pytest.raises(ValueError):
        ExportOptions(margin_edges=edges)


def test_impossible_content_area_and_custom_plate_cap_fail():
    with pytest.raises(ValueError, match="no usable content"):
        make_settings(page_width=100, margin_edges=[0, 60, 0, 60])
    with pytest.raises(ValueError, match="combined left and right"):
        make_settings(margin_edges=[2, 40, 2, 40], max_custom_width=70)


@pytest.mark.parametrize("mode", ["full", "none", "bottom-right"])
def test_custom_edges_override_all_page_margins_and_bespoke_geometry(mode):
    s = make_settings(margin_mode=mode, margin_edges=[3, 17, 21, 7])
    assert s.page_margins == (3, 17, 21, 7)
    assert s.body_page.content_h_px == s.content_h_px
    for plate in s.plates:
        assert plate.content_w_px == (plate.width - 24) * PX_PER_MM
        assert plate.content_h_px == (plate.height - 24) * PX_PER_MM
    plan = plan_figure(5000, 2000, s)
    w, h, _ = plan["custom"]
    assert plan["scale"] > 0
    assert (w - 24) * PX_PER_MM >= 5000 * plan["scale"] - 1e-8
    assert (h - 24) * PX_PER_MM >= 2000 * plan["scale"] + s.caption_px - 1e-8
    css = build_css({"custom-test": plan["custom"]}, s)
    assert css.count("margin: 3mm 17mm 21mm 7mm") == 5


def test_blank_api_overrides_preserve_online_preset_defaults():
    defaults = make_settings()
    api = ExportOptions().settings(JobLimits())
    assert api.page_width == defaults.page_width
    assert api.page_margins == defaults.page_margins
    assert api.inline_fit == defaults.inline_fit
    assert api.margin_edges is None
    with pytest.raises(ValueError):
        ExportOptions.model_validate({"limits": {"max_embedded_bytes": 999999999}})
