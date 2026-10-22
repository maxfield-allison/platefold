"""Settings files are YAML or JSON; a TOML file stops with conversion steps."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from platefold.cli import build_settings, find_settings_file, parse_args
from platefold.config import (
    LegacySettingsError,
    load_settings_file,
    make_settings,
    parse_settings_file,
    settings_files,
)
from platefold.i18n import translator
from platefold.limits import ResourceLimits
from platefold.worker import recovery_id, request_from

REPO = Path(__file__).resolve().parents[1]
SCHEMA = json.loads((REPO / "platefold/schemas/settings.schema.json").read_text("utf-8"))
EXAMPLE = REPO / "platefold.example.yml"

YAML_TEXT = "page:\n  preset: a4\n  margin_edges: [0, 8, 12, 0]\nbehaviour:\n  image_dpi: 150\n"
JSON_TEXT = (
    '{"page": {"preset": "a4", "margin_edges": [0, 8, 12, 0]}, "behaviour": {"image_dpi": 150}}'
)

# A pre-1.0 export.toml using every kind of value the old example used.
LEGACY_TOML = """\
# old settings
[page]
preset = "a4"   # paper
margin_edges = [0, 8, 12, 0]

[layout]
inline_fit = 0.75

[typography]
font_set = "segoe"

[theme]
base = "mono"
accent        = "#2f5d8c"   # aligned like the old example

[behaviour]
soft_breaks = false
sheets = ["Summary", "#2"]
image_dpi = 150

[limits]
max_export_seconds = 600
"""


# ------------------------------------------------------------------ loading


@pytest.mark.parametrize(
    ("name", "text"),
    [("platefold.yml", YAML_TEXT), ("platefold.yaml", YAML_TEXT), ("platefold.json", JSON_TEXT)],
)
def test_each_settings_name_is_discovered_and_loaded(tmp_path, name, text):
    (tmp_path / name).write_text(text, encoding="utf-8")
    assert settings_files(tmp_path) == [tmp_path / name]
    preset, overrides = load_settings_file(str(tmp_path / name))
    settings = make_settings(preset, **overrides)
    assert (settings.page_width, settings.page_height) == (210, 297)
    assert settings.page_margins == (0, 8, 12, 0)
    assert settings.image_dpi == 150


def test_yaml_and_json_load_to_identical_settings(tmp_path):
    loaded = []
    for name, text in (("platefold.yml", YAML_TEXT), ("platefold.json", JSON_TEXT)):
        (tmp_path / name).write_text(text, encoding="utf-8")
        preset, overrides = load_settings_file(str(tmp_path / name))
        loaded.append(make_settings(preset, **overrides))
    assert loaded[0] == loaded[1]


def test_settings_file_with_a_byte_order_mark_loads(tmp_path):
    path = tmp_path / "platefold.json"
    path.write_bytes(b"\xef\xbb\xbf" + JSON_TEXT.encode("utf-8"))
    assert load_settings_file(str(path))[0] == "a4"


def test_invalid_yaml_names_the_file(tmp_path):
    path = tmp_path / "platefold.yml"
    path.write_text("page: [unclosed\n", encoding="utf-8")
    with pytest.raises(ValueError, match=r"platefold\.yml: invalid YAML"):
        load_settings_file(str(path))


# ---------------------------------------------------------- legacy TOML files


def test_toml_text_is_refused_with_conversion_steps():
    with pytest.raises(LegacySettingsError) as caught:
        parse_settings_file(LEGACY_TOML, "toml")
    message = str(caught.value)
    assert "no longer reads TOML" in message
    assert "platefold.yml" in message and "platefold.json" in message
    assert "'section:'" in message and "'key: value'" in message


def test_an_explicit_toml_path_is_refused_before_it_is_read(tmp_path):
    path = tmp_path / "export.toml"
    path.write_bytes(b"\xff not even text")
    with pytest.raises(LegacySettingsError, match=re.escape(str(path))):
        load_settings_file(str(path))
    with pytest.raises(LegacySettingsError):
        load_settings_file(str(tmp_path / "Other.TOML"))


def test_cli_stops_on_a_lone_export_toml_instead_of_ignoring_it(tmp_path):
    (tmp_path / "export.toml").write_text(LEGACY_TOML, encoding="utf-8")
    (tmp_path / "doc.md").write_text("# Doc\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        build_settings(parse_args([str(tmp_path / "doc.md")]), str(tmp_path))
    message = str(caught.value.code)
    assert "export.toml" in message and "no longer reads TOML" in message
    assert "save the result as platefold.yml" in message


def test_cli_explicit_toml_config_is_refused(tmp_path):
    (tmp_path / "export.toml").write_text(LEGACY_TOML, encoding="utf-8")
    args = parse_args([str(tmp_path), "--config", str(tmp_path / "export.toml")])
    with pytest.raises(SystemExit, match="no longer reads TOML"):
        build_settings(args, str(tmp_path))


def test_leftover_export_toml_beside_a_converted_file_names_both(tmp_path):
    (tmp_path / "export.toml").write_text(LEGACY_TOML, encoding="utf-8")
    (tmp_path / "platefold.yml").write_text(YAML_TEXT, encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        find_settings_file(None, str(tmp_path))
    message = str(caught.value.code)
    assert "platefold.yml lacks into platefold.yml" in message
    assert "then delete export.toml" in message


def test_explicit_config_still_works_beside_a_leftover_export_toml(tmp_path):
    (tmp_path / "export.toml").write_text(LEGACY_TOML, encoding="utf-8")
    (tmp_path / "platefold.yml").write_text(YAML_TEXT, encoding="utf-8")
    chosen = str(tmp_path / "platefold.yml")
    assert find_settings_file(chosen, str(tmp_path)) == chosen
    settings, path = build_settings(parse_args([str(tmp_path), "--config", chosen]), str(tmp_path))
    assert path == chosen and settings.image_dpi == 150


def test_desktop_worker_refuses_export_toml_with_specific_guidance(tmp_path):
    (tmp_path / "export.toml").write_text(LEGACY_TOML, encoding="utf-8")
    source = tmp_path / "doc.md"
    source.write_text("# Doc\n", encoding="utf-8")
    with pytest.raises(LegacySettingsError) as caught:
        request_from({"input_path": str(source), "output_dir": str(tmp_path / "out")})
    assert recovery_id(caught.value) == "help.legacy_settings"
    assert "export.toml" in translator("en").text("help.legacy_settings")


def test_the_conversion_described_in_the_error_preserves_every_value():
    """Apply the error message's rule literally and compare with a TOML parse."""
    converted = []
    for line in LEGACY_TOML.splitlines():
        if section := re.fullmatch(r"\[(\w+)\]", line.strip()):
            converted.append(f"{section.group(1)}:")
        elif assignment := re.fullmatch(r"(\w+)\s*=\s*(.*)", line.strip()):
            converted.append(f"  {assignment.group(1)}: {assignment.group(2)}")
        else:
            converted.append(line)
    yaml_text = "\n".join(converted)
    assert yaml.safe_load(yaml_text) == tomllib.loads(LEGACY_TOML)
    preset, overrides = parse_settings_file(yaml_text, "yml")
    settings = make_settings(preset, **overrides)
    assert settings.theme.accent == "#2f5d8c"
    assert settings.xlsx_sheets == ("Summary", "#2")
    assert settings.limits.max_export_seconds == 600


# ------------------------------------------------------------------- schema


def test_the_settings_schema_is_a_valid_draft_2020_12_schema():
    Draft202012Validator.check_schema(SCHEMA)


@pytest.mark.parametrize(
    ("text", "offender"),
    [
        ("page:\n  preset: b5\n", "page.preset"),
        ("page:\n  margin_edges: [1, 2, 3]\n", "page.margin_edges"),
        ("layout:\n  plate_fit: 1.5\n", "layout.plate_fit"),
        ("behaviour:\n  toc_min_headings: 2.5\n", "behaviour.toc_min_headings"),
        ("theme:\n  accent: '#12345'\n", "accent"),
        ("limits:\n  max_pdf_bytes: 0\n", "limits.max_pdf_bytes"),
        ("limits:\n  max_pdf_byte: 10\n", "limits"),
    ],
)
def test_schema_violations_name_the_setting(text, offender):
    with pytest.raises(ValueError, match=re.escape(offender)):
        parse_settings_file(text, "yml")


def test_the_example_file_validates_and_changes_nothing_as_shipped():
    data = yaml.safe_load(EXAMPLE.read_text("utf-8"))
    Draft202012Validator(SCHEMA).validate(data)
    preset, overrides = load_settings_file(str(EXAMPLE))
    assert make_settings(preset or "auto", **overrides) == make_settings()
    assert overrides["limits"] == ResourceLimits()


def test_the_example_file_mentions_every_setting():
    text = EXAMPLE.read_text("utf-8")
    for section, block in SCHEMA["properties"].items():
        if section == "$schema":
            continue
        assert re.search(rf"^{section}:", text, re.MULTILINE), section
        for key in block["properties"]:
            assert re.search(rf"^\s+#?\s*{key}:", text, re.MULTILINE), f"{section}.{key}"
