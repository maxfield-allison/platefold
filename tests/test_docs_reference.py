"""The documentation reference is generated from the package; keep it that way.

tools/docs_reference.py turns the settings schema, the Settings defaults, the
resource limits, the theme files and the CLI parser into tables when
tools/prepare_docs.py stages the site. These tests run the same code in the ordinary unit suite, so a schema key
added without a mapping, a default or a description fails here, before anyone
builds the site.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import re
from pathlib import Path

import pytest

from platefold import config, limits, theme

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"


def load_reference():
    spec = importlib.util.spec_from_file_location(
        "docs_reference", REPO / "tools" / "docs_reference.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reference = load_reference()


def settings_schema() -> dict:
    return json.loads((REPO / "platefold" / "schemas" / "settings.schema.json").read_text("utf-8"))


@pytest.mark.parametrize("section", reference.settings_sections())
def test_every_settings_section_renders(section):
    table = reference.settings_table(section)
    keys = settings_schema()["properties"][section]["properties"]
    for key in keys:
        assert f"| `{key}` |" in table


@pytest.mark.parametrize("section", reference.MAPPED_SECTIONS)
def test_schema_and_file_mapping_describe_the_same_keys(section):
    schema_keys = set(settings_schema()["properties"][section]["properties"])
    mapped = set(config._FILE_SCHEMA[section])
    special = {key for sec, key in reference.SPECIAL_DEFAULTS if sec == section}
    assert schema_keys == mapped | special


def test_every_resource_limit_is_in_the_schema():
    fields = {item.name for item in dataclasses.fields(limits.ResourceLimits)}
    schema_keys = set(settings_schema()["properties"]["limits"]["properties"])
    assert fields == schema_keys


def test_every_settings_key_carries_its_own_description():
    for section, block in settings_schema()["properties"].items():
        assert block.get("description"), section
        for key, spec in block.get("properties", {}).items():
            assert spec.get("description", "").strip(), f"{section}.{key}"


def test_defaults_come_from_the_code():
    defaults = config.Settings()
    rows = {key: default for key, _kind, default, _meaning in reference.settings_rows("layout")}
    assert rows["plate_fit"] == f"`{defaults.plate_fit}`"
    assert rows["max_custom_width"].startswith(f"`{int(defaults.max_custom_width)}`")
    typography = {key: default for key, _k, default, _m in reference.settings_rows("typography")}
    assert typography["font_set"] == f"`{theme.DEFAULT_FONT_SET}`"


def test_every_builtin_theme_and_font_set_is_listed():
    table = reference.builtin_theme_table()
    for path in sorted((REPO / "platefold" / "themes").glob("*.json")):
        assert f"`{path.stem}`" in table
    fonts = reference.font_set_table()
    for name in theme.FONT_SETS:
        assert f"`{name}`" in fonts


def test_every_contrast_rule_is_listed():
    from platefold import contrast

    table = reference.contrast_rule_table()
    for rule in contrast.RULES:
        assert f"| {rule.label} |" in table
    assert table.count("reported, not scored") == sum(
        1 for rule in contrast.RULES if rule.kind == "decorative"
    )


def test_cli_help_lists_every_option():
    source = (REPO / "platefold" / "cli.py").read_text("utf-8")
    options = set(re.findall(r'add_argument\(\s*(?:"-[a-z]",\s*)?"(--[a-z-]+)"', source))
    assert options, "no options found in cli.py"
    help_text = reference.cli_help()
    assert help_text.startswith("usage: platefold ")
    for option in options:
        assert option in help_text


def test_unknown_marker_is_an_error():
    with pytest.raises(reference.DocsReferenceError):
        reference.expand("<!-- platefold:generate no-such-table -->\n")


def test_every_marker_in_the_docs_is_known():
    markers = set()
    for page in DOCS.rglob("*.md"):
        markers |= set(
            re.findall(r"<!-- platefold:generate ([a-z-]+) -->", page.read_text("utf-8"))
        )
    assert markers, "no generation markers found in docs/"
    assert markers <= set(reference.GENERATORS)
