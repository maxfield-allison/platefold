"""Settings, presets, and the diagram cache signature."""

from __future__ import annotations

import pytest

from platefold.config import Settings, make_settings, parse_settings_file
from platefold.limits import MAX_CONFIG_BYTES
from platefold.theme import get_font_set, get_theme

# ---------------------------------------------------------------------------
# Presets and overrides
# ---------------------------------------------------------------------------


def test_presets_change_real_geometry():
    letter, a4 = make_settings("letter"), make_settings("a4")
    assert (letter.page_width, letter.page_height) != (a4.page_width, a4.page_height)
    assert a4.content_w_px < letter.content_w_px


def test_uniform_preset_removes_the_plate_ladder():
    s = make_settings("uniform-letter")
    assert s.plates == []
    assert s.max_custom_width == 0.0


@pytest.mark.parametrize("width", [1, 12, 24])
def test_custom_page_width_must_leave_content_between_margins(width):
    with pytest.raises(ValueError, match="max_custom_width"):
        make_settings(max_custom_width=width)
    with pytest.raises(ValueError, match="max_custom_width"):
        parse_settings_file('{"layout":{"max_custom_width":%s}}' % width, "json")


@pytest.mark.parametrize("width", [0, 24.1, 900])
def test_valid_custom_width_still_produces_positive_scale(width):
    from platefold.layout import plan_figure

    plan = plan_figure(4000, 2000, make_settings(max_custom_width=width))
    assert plan["scale"] > 0


def test_unknown_preset_is_rejected_by_name():
    with pytest.raises(ValueError, match="unknown preset"):
        make_settings("unknown-paper")


def test_unknown_setting_is_rejected_rather_than_ignored():
    with pytest.raises(ValueError, match="unknown setting"):
        make_settings("letter", margni=12.0)


def test_none_overrides_are_ignored_so_cli_defaults_pass_through():
    s = make_settings("letter", soft_breaks=None, image_dpi=None)
    assert s.soft_breaks is Settings().soft_breaks
    assert s.image_dpi == Settings().image_dpi


def test_theme_and_font_set_accept_names():
    s = make_settings("letter", theme="mono", font_set="cambria")
    assert s.theme.name == "mono"
    assert s.fonts.name == "cambria"
    assert s.body_font == get_font_set("cambria").body
    assert s.accent == get_theme("mono").accent


def test_unknown_theme_name_is_rejected():
    with pytest.raises(ValueError, match="unknown theme"):
        make_settings("letter", theme="neon")


# ---------------------------------------------------------------------------
# Diagram cache signature.
#
# Regression guard: the cache used to be keyed on diagram source alone, so
# changing the font silently reused SVGs measured under the old settings --
# and since measured geometry drives the placement ladder, figures landed on
# the wrong page size.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field,value",
    [
        ("mermaid_font_px", 22),
        ("mermaid_wrap_px", 400),
        ("font_set", "arial"),  # a set with a different *heading* font
        ("theme", "bond"),
    ],
)
def test_signature_changes_when_rendering_inputs_change(field, value):
    base = make_settings("letter")
    changed = make_settings("letter", **{field: value})
    assert base.diagram_signature() != changed.diagram_signature()


def test_signature_ignores_a_font_set_that_only_changes_body_text():
    """Mermaid renders in the heading font; body-only changes are irrelevant.

    calibri and cambria differ in body face but share a heading face, so the
    diagram cache should survive switching between them.
    """
    calibri = make_settings("letter", font_set="calibri")
    cambria = make_settings("letter", font_set="cambria")
    assert calibri.fonts.body != cambria.fonts.body
    assert calibri.fonts.head == cambria.fonts.head
    assert calibri.diagram_signature() == cambria.diagram_signature()


@pytest.mark.parametrize(
    "field,value",
    [
        ("margin", 22.0),
        ("page_width", 210.0),
        ("body_size_pt", 14.0),
        ("inline_fit", 0.5),
        ("soft_breaks", False),
        ("image_dpi", 150),
    ],
)
def test_signature_is_stable_for_settings_that_do_not_affect_diagrams(field, value):
    """Otherwise every unrelated tweak would throw away the diagram cache."""
    base = make_settings("letter")
    changed = make_settings("letter", **{field: value})
    assert base.diagram_signature() == changed.diagram_signature()


def test_signature_is_deterministic_across_equivalent_settings():
    assert (
        make_settings("letter").diagram_signature() == make_settings("letter").diagram_signature()
    )


# ---------------------------------------------------------------------------
# Settings files
# ---------------------------------------------------------------------------


def test_settings_file_sets_geometry_and_behaviour():
    preset, overrides = parse_settings_file(
        """
        page:
          preset: a4
          margin: 20.0
        layout:
          inline_fit: 0.6
        typography:
          font_set: segoe
        behaviour:
          toc: false
          image_dpi: 150
        """
    )
    assert preset == "a4"
    s = make_settings(preset, **overrides)
    assert s.page_width == 210.0  # from the preset
    assert s.margin == 20.0  # overridden by the file
    assert s.inline_fit == 0.6
    assert s.fonts.name == "segoe"
    assert s.toc is False
    assert s.image_dpi == 150


def test_settings_file_can_raise_a_resource_limit():
    preset, overrides = parse_settings_file("limits:\n  max_markdown_bytes: 100000000\n")
    settings = make_settings(preset or "letter", **overrides)
    assert settings.limits.max_markdown_bytes == 100_000_000


@pytest.mark.parametrize("value", [0, -1, 1.5, True])
def test_resource_limits_must_be_positive_integers(value):
    with pytest.raises(ValueError, match=r"limits\.max_markdown_bytes"):
        make_settings(limits={"max_markdown_bytes": value})


def test_settings_content_is_bounded_before_parsing():
    with pytest.raises(ValueError, match="settings content exceeds"):
        parse_settings_file(" " * (MAX_CONFIG_BYTES + 1))


def test_settings_file_can_recolour_a_base_theme():
    _, overrides = parse_settings_file("theme:\n  base: mono\n  accent: '#ff0000'\n")
    theme = overrides["theme"]
    assert theme.accent == "#ff0000"
    # Untouched tokens still come from the base theme.
    assert theme.ink == get_theme("mono").ink
    assert "mono" in theme.name


def test_settings_file_rejects_an_unknown_section():
    with pytest.raises(ValueError, match="unknown section"):
        parse_settings_file("colours:\n  accent: '#ffffff'\n")


def test_settings_file_rejects_a_misspelled_key_rather_than_ignoring_it():
    with pytest.raises(ValueError, match="inline_fitt"):
        parse_settings_file("layout:\n  inline_fitt: 0.9\n")


def test_settings_file_rejects_an_unknown_theme_token():
    with pytest.raises(ValueError, match="unknown theme colour"):
        parse_settings_file("theme:\n  base: default\n  background: '#ffffff'\n")


def test_empty_settings_file_changes_nothing():
    preset, overrides = parse_settings_file("")
    assert preset is None
    assert overrides == {}
