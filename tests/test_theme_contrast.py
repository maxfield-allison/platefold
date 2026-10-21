"""Every built-in theme meets WCAG 2.1 AA on the pairings the renderer actually draws.

The rule table lives in `platefold.contrast`. Built-ins are gated here; a user's
own theme gets the same check as a warning when it is selected, and the desktop
shows the same pairings live while a palette is edited.
"""

from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

import pytest

from platefold import contrast
from platefold.config import make_settings, parse_settings_file
from platefold.contrast import RULES, contrast_failures, contrast_report, describe_failure
from platefold.style import WARN_BORDER, WARN_TEXT, build_css
from platefold.theme import COLOR_TOKENS, THEMES, ThemeContrastWarning, colours_of, get_theme


@pytest.mark.parametrize("name", sorted(THEMES))
def test_every_builtin_meets_every_scored_pairing(name):
    failures = contrast_failures(colours_of(THEMES[name]))
    assert not failures, [describe_failure(f) for f in failures]


def test_rules_name_real_tokens_and_cover_every_token():
    """A new colour token must be paired with something, or it ships unchecked."""
    referenced = set()
    for rule in RULES:
        for token in (rule.fg, rule.bg, *(rule.alternate or ())):
            if token.startswith("#"):
                assert contrast.HEX.match(token), token
                continue
            assert token in COLOR_TOKENS, token
            referenced.add(token)
    assert referenced == COLOR_TOKENS, COLOR_TOKENS - referenced


def test_decorative_pairings_are_reported_without_a_verdict():
    report = contrast_report(colours_of(THEMES["bristol"]))
    decorative = [entry for entry in report if entry["kind"] == "decorative"]
    assert {entry["fg"] for entry in decorative} == {"accent_soft", "rule", "diagram_note_stroke"}
    assert all(entry["passes"] is None and entry["ratio"] >= 1 for entry in decorative)
    assert all(entry["passes"] is not None for entry in report if entry["kind"] != "decorative")


@pytest.mark.parametrize("name", sorted(THEMES))
def test_warning_callout_stays_visible_by_border_on_light_pages_and_fill_on_dark(name):
    theme = THEMES[name]
    entry = next(e for e in contrast_report(colours_of(theme)) if e["kind"] == "callout")
    assert entry["passes"]
    if theme.mode == "dark":
        assert entry["alternate_ratio"] >= 3.0, "the light fill must stand out from a dark page"
    else:
        assert entry["ratio"] >= 3.0, "the dashed border must stand out from a light page"


def test_callout_border_is_drawn_in_the_callout_text_colour():
    assert WARN_BORDER == WARN_TEXT == contrast.CALLOUT_TEXT
    assert f"dashed {contrast.CALLOUT_TEXT}" in build_css({}, make_settings())


def low_contrast_theme(directory: Path, name: str) -> Path:
    colors = colours_of(THEMES["bristol"]) | {"ink_soft": "#e0d8c8"}
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps({"name": name, "mode": "light", "colors": colors}), encoding="utf-8")
    return path


def test_selecting_a_user_theme_below_aa_warns_but_still_loads(tmp_path, monkeypatch):
    monkeypatch.setattr("platefold.theme.user_theme_dir", lambda: tmp_path / "user")
    low_contrast_theme(tmp_path / "user", "dim")
    with pytest.warns(ThemeContrastWarning, match=r"ink_soft on page is 1\.\d\d:1") as record:
        theme = get_theme("dim")
    assert theme.ink_soft == "#e0d8c8"
    message = str(record[0].message)
    assert "captions" in message and "ink_soft on surface_alt" in message


def test_project_theme_warns_with_its_path_and_builtins_never_do(tmp_path, monkeypatch):
    monkeypatch.setattr("platefold.theme.user_theme_dir", lambda: tmp_path / "user")
    path = low_contrast_theme(tmp_path / "themes", "faint")
    with pytest.warns(ThemeContrastWarning, match=re.escape(str(path))):
        get_theme("faint", tmp_path)
    with warnings.catch_warnings():
        warnings.simplefilter("error", ThemeContrastWarning)
        for name in THEMES:
            get_theme(name, tmp_path)


def test_settings_file_recolouring_warns_on_the_merged_palette():
    with pytest.warns(ThemeContrastWarning, match="settings file"):
        parse_settings_file("theme:\n  base: bristol\n  ink: '#f0f0f0'\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error", ThemeContrastWarning)
        parse_settings_file("theme:\n  base: bristol\n  accent: '#123456'\n")


def test_page_colour_is_part_of_the_diagram_cache_signature():
    """Edge labels are backed by the page colour, so a page change must re-render."""
    base = make_settings("letter", theme="bristol")
    changed = make_settings("letter", theme=THEMES["bristol"].merged(page="#000000"))
    assert base.diagram_signature() != changed.diagram_signature()


@pytest.mark.integration
@pytest.mark.parametrize("name", ["midnight", "bristol"])
def test_diagram_edge_labels_notes_and_cluster_titles_follow_the_theme(name, tmp_path):
    """Mermaid's own defaults are white edge-label boxes and grey note text.

    On a dark palette that printed light text on white. Every text colour and
    backing box a diagram draws must come from the theme.
    """
    from platefold.service import ExportRequest, export

    theme = THEMES[name]
    source = tmp_path / "src"
    source.mkdir()
    (source / "labels.md").write_text(
        "# Labels\n\nLanguage: en\n\n```mermaid\ngraph LR\n  accDescr: Source leads to Page through a labelled edge inside Cluster.\n  subgraph cluster [Cluster]\n"
        "    A[Source] -->|labelled edge| B[Page]\n  end\n```\n\n"
        "```mermaid\nsequenceDiagram\n  accDescr: A sends a request to B, which has a note.\n  A->>B: request\n  Note over B: a note\n```\n",
        encoding="utf-8",
    )
    cache = tmp_path / "cache"
    result = export(
        ExportRequest(
            str(source),
            str(tmp_path / "out"),
            make_settings("letter", theme=name, font_set="sora"),
            diagram_cache=str(cache),
        )
    )
    assert result.status == "completed", result.report
    css = "\n".join(path.read_text(encoding="utf-8") for path in cache.rglob("*.svg"))
    assert ".edgeLabel{background-color:" + theme.page + ";" in css
    assert re.search(r"\.noteText[^{]*\{fill:" + theme.diagram_text + ";", css)
    assert ".cluster-label span{color:" + theme.diagram_text + ";}" in css
    assert "#ffffff" not in css.split(".edgeLabel{", 1)[1][:80]
