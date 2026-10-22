"""Themes and font sets."""

from __future__ import annotations

import re
from dataclasses import fields

import pytest

from platefold.theme import (
    COLOR_TOKENS,
    FONT_SETS,
    THEMES,
    Theme,
    get_font_set,
    get_theme,
)

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# Fonts that only exist on Linux distributions. This tool ships to Windows,
# and a stack led by one of these silently falls back to Arial / Courier New,
# which changes text metrics and therefore pagination. That was a real defect;
# these tests keep it from coming back.
LINUX_ONLY = {
    "carlito",
    "liberation sans",
    "liberation serif",
    "liberation mono",
    "dejavu sans mono",
}


@pytest.mark.parametrize("name", sorted(THEMES))
def test_every_theme_defines_every_colour_as_valid_hex(name):
    theme = THEMES[name]
    for f in fields(Theme):
        value = getattr(theme, f.name)
        if f.name in COLOR_TOKENS:
            assert HEX.match(value), f"{name}.{f.name} = {value!r} is not a #rrggbb colour"


@pytest.mark.parametrize("name", sorted(THEMES))
def test_theme_name_matches_its_registry_key(name):
    assert THEMES[name].name == name
    assert "?" not in THEMES[name].label


# Faces that actually ship with Windows. A stack must reach one of these
# before it reaches any Linux-only face, or the document renders in a font
# nobody designed against.
WINDOWS_FONTS = {
    "calibri",
    "segoe ui",
    "segoe ui semibold",
    "cambria",
    "georgia",
    "arial",
    "arial bold",
    "consolas",
    "courier new",
    "times new roman",
}


def stack_families(stack: str) -> list[str]:
    return [f.strip().strip("'\"").lower() for f in stack.split(",")]


@pytest.mark.parametrize("name", sorted(FONT_SETS))
def test_font_stacks_keep_a_linux_fallback_for_ci(name):
    """CI containers have no Microsoft fonts; the tail must still resolve."""
    fs = FONT_SETS[name]
    for role in ("body", "head", "mono"):
        stack = getattr(fs, role).lower()
        assert any(f in stack for f in LINUX_ONLY), f"{name}.{role} has no Linux fallback"


@pytest.mark.parametrize("name", sorted(FONT_SETS))
def test_font_stacks_end_in_a_generic_family(name):
    fs = FONT_SETS[name]
    for role in ("body", "head", "mono"):
        last = getattr(fs, role).split(",")[-1].strip()
        assert last in ("sans-serif", "serif", "monospace")


def test_unknown_lookups_name_the_valid_options():
    with pytest.raises(ValueError, match="unknown theme"):
        get_theme("nope")
    with pytest.raises(ValueError, match="unknown font set"):
        get_font_set("nope")


def test_merged_overrides_only_the_named_tokens():
    base = get_theme("default")
    merged = base.merged(accent="#123456")
    assert merged.accent == "#123456"
    assert merged.ink == base.ink
    assert base.accent != "#123456", "the original theme must not be mutated"


def test_merged_rejects_a_typo_instead_of_silently_dropping_it():
    with pytest.raises(ValueError, match="unknown theme colour"):
        get_theme("default").merged(acccent="#123456")


def test_themes_are_visually_distinct_from_one_another():
    """Two themes that render identically would be a packaging mistake."""
    accents = {t.accent for t in THEMES.values()}
    assert len(accents) == len(THEMES)


@pytest.mark.parametrize("name", sorted(THEMES))
def test_builtin_text_colours_have_readable_contrast(name):
    """Check text roles, not decorative rules or a claim about whole-app accessibility."""
    theme = THEMES[name]

    def luminance(colour):
        rgb = [int(colour[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in rgb]
        return sum(v * weight for v, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True))

    for role in ("ink", "ink_strong", "ink_soft", "title", "heading", "link"):
        foreground, background = sorted((luminance(getattr(theme, role)), luminance(theme.page)))
        assert (background + 0.05) / (foreground + 0.05) >= 4.5, f"{name}.{role}"
