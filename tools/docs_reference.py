"""Generate reference tables for the documentation from the package itself.

The configuration reference used to be the page most likely to go stale: a
schema key gets added, a default moves, and the prose table quietly disagrees.
These functions read the shipped JSON schemas, the settings dataclass defaults,
the resource limits, the built-in theme files and the real argument parser, so
the docs build describes whatever the package currently does.
tools/prepare_docs.py expands the markers when it stages the site.

Only the standard library and `platefold` are imported, which lets
tests/test_docs_reference.py exercise this in the ordinary unit-test job without
the documentation toolchain installed.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import os
import sys
from importlib import resources

from platefold import cli, config, limits, theme

MIB = 1024 * 1024

# Sections whose keys map straight onto Settings fields through config._FILE_SCHEMA.
MAPPED_SECTIONS = ("page", "layout", "typography", "behaviour")

# Settings keys whose default is not a plain Settings attribute of the same name.
SPECIAL_DEFAULTS = {
    ("page", "preset"): "auto",
    ("page", "locale"): "device locale",
    ("typography", "font_set"): theme.DEFAULT_FONT_SET,
    ("theme", "base"): theme.DEFAULT_THEME,
}

UNITS = {
    "width": "mm",
    "height": "mm",
    "margin": "mm",
    "margin_bottom": "mm",
    "margin_edges": "mm, top/right/bottom/left",
    "max_custom_width": "mm",
    "caption_px": "CSS px",
    "tile_overlap": "mm",
    "body_size_pt": "pt",
    "image_dpi": "DPI",
    "mermaid_font_px": "CSS px",
    "mermaid_wrap_px": "CSS px",
    "max_export_seconds": "seconds",
}


class DocsReferenceError(ValueError):
    """The schema and the code disagree in a way the docs cannot paper over."""


def load_schema(name: str) -> dict:
    text = resources.files("platefold").joinpath("schemas", name).read_text(encoding="utf-8")
    return json.loads(text)


# ------------------------------------------------------------------- settings


def settings_default(section: str, key: str):
    """Return the effective default for one settings key, or raise."""
    if (section, key) in SPECIAL_DEFAULTS:
        return SPECIAL_DEFAULTS[(section, key)]
    if section == "limits":
        return getattr(limits.ResourceLimits(), key)
    if section == "theme":
        return None  # colour overrides inherit from the base theme
    field = config._FILE_SCHEMA.get(section, {}).get(key)
    if field is None:
        raise DocsReferenceError(
            f"settings key {section}.{key} has no mapping in config._FILE_SCHEMA"
        )
    defaults = config.Settings()
    if not hasattr(defaults, field):
        raise DocsReferenceError(f"settings key {section}.{key} maps to missing field {field!r}")
    return getattr(defaults, field)


def _format_default(key: str, value) -> str:
    if value is None:
        return "inherited from `base`"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    if isinstance(value, int) and value >= MIB and value % MIB == 0:
        return f"`{value}` ({value // MIB} MiB)"
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    unit = UNITS.get(key)
    return f"`{value}`" + (f" {unit}" if unit else "")


def _bounds(spec: dict) -> list[str]:
    bounds = []
    if "minimum" in spec:
        bounds.append(f"≥ {spec['minimum']}")
    if "exclusiveMinimum" in spec:
        bounds.append(f"> {spec['exclusiveMinimum']}")
    if "maximum" in spec:
        bounds.append(f"≤ {spec['maximum']}")
    alternatives = [
        str(alt["const"]) if "const" in alt else f"> {alt['exclusiveMinimum']}"
        for alt in spec.get("anyOf", [])
        if "const" in alt or "exclusiveMinimum" in alt
    ]
    if alternatives:
        bounds.append(" or ".join(alternatives))
    return bounds


def _format_type(spec: dict) -> str:
    if "enum" in spec:
        return " \\| ".join(f"`{item}`" for item in spec["enum"])
    kind = "hex colour" if spec.get("pattern") == "^#[0-9a-fA-F]{6}$" else spec.get("type", "")
    bounds = ", ".join(_bounds(spec))
    return " ".join(part for part in (kind, bounds) if part)


def settings_rows(section: str) -> list[tuple[str, str, str, str]]:
    schema = load_schema("settings.schema.json")
    block = schema["properties"].get(section)
    if block is None:
        raise DocsReferenceError(f"settings schema has no section {section!r}")
    rows = []
    for key, spec in block["properties"].items():
        default = settings_default(section, key)
        description = spec.get("description", "")
        if not description:
            raise DocsReferenceError(f"settings key {section}.{key} has no description")
        rows.append((key, _format_type(spec), _format_default(key, default), description))
    return rows


def settings_table(section: str) -> str:
    lines = ["| Key | Type | Default | Meaning |", "| --- | --- | --- | --- |"]
    for key, kind, default, meaning in settings_rows(section):
        lines.append(f"| `{key}` | {kind} | {default} | {meaning} |")
    return "\n".join(lines)


def settings_sections() -> list[str]:
    return [name for name in load_schema("settings.schema.json")["properties"] if name != "$schema"]


# --------------------------------------------------------------------- themes


def theme_token_table() -> str:
    schema = load_schema("theme.schema.json")
    colours = schema["properties"]["colors"]["properties"]
    lines = ["| Token | Used for |", "| --- | --- |"]
    for key, spec in colours.items():
        meaning = spec.get("description", "")
        lines.append(f"| `{key}` | {meaning} |")
    return "\n".join(lines)


def builtin_themes() -> list[dict]:
    folder = resources.files("platefold").joinpath("themes")
    loaded = []
    for entry in sorted(folder.iterdir(), key=lambda item: item.name):
        if not entry.name.endswith(".json"):
            continue
        loaded.append(json.loads(entry.read_text(encoding="utf-8")))
    return loaded


SWATCH_TOKENS = ("page", "ink", "heading", "accent", "link", "diagram_fill", "diagram_stroke")


def builtin_theme_table() -> str:
    """A swatch book of the built-in themes, from the files the package ships."""
    header = "| Theme | Mode | Swatches | Description |"
    lines = [header, "| --- | --- | --- | --- |"]
    for data in builtin_themes():
        resolved = theme.get_theme(data["name"])
        chips = "".join(
            f'<span class="pf-swatch" style="background:{getattr(resolved, token)}"'
            f' title="{token} {getattr(resolved, token)}"></span>'
            for token in SWATCH_TOKENS
        )
        mode = data.get("mode", "light")
        label = data.get("label", data["name"])
        description = data.get("description", "").replace("|", "\\|")
        lines.append(f"| `{data['name']}`<br>{label} | {mode} | {chips} | {description} |")
    return "\n".join(lines)


def contrast_rule_table() -> str:
    """The pairings every theme is checked against, from platefold/contrast.py."""
    from platefold import contrast

    def name(token: str) -> str:
        return "the warning callout" if token.startswith("#") else f"`{token}`"

    lines = ["| Where it's drawn | Colour | On | Needs |", "| --- | --- | --- | --- |"]
    for rule in contrast.RULES:
        needs = "reported, not scored" if rule.kind == "decorative" else f"{rule.minimum:g}:1"
        fg = name(rule.fg) if not rule.alternate else "border or fill"
        lines.append(f"| {rule.label} | {fg} | {name(rule.bg)} | {needs} |")
    return "\n".join(lines)


def font_set_table() -> str:
    lines = ["| Font set | Faces |", "| --- | --- |"]
    for name in sorted(theme.FONT_SETS):
        lines.append(f"| `{name}` | {theme.FONT_SETS[name].label} |")
    return "\n".join(lines)


# ------------------------------------------------------------------------ CLI


def cli_help() -> str:
    """Return `platefold --help` exactly as argparse prints it at 88 columns."""
    previous = os.environ.get("COLUMNS")
    previous_argv0 = sys.argv[0] if sys.argv else ""
    os.environ["COLUMNS"] = "88"
    # argparse names the program after argv[0]; under the docs build or pytest that
    # would print their name instead of the installed command's.
    if sys.argv:
        sys.argv[0] = "platefold"
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer), contextlib.suppress(SystemExit):
            cli.parse_args(["--help"])
    finally:
        if sys.argv:
            sys.argv[0] = previous_argv0
        if previous is None:
            os.environ.pop("COLUMNS", None)
        else:
            os.environ["COLUMNS"] = previous
    return buffer.getvalue().rstrip()


def limits_fields() -> list[str]:
    return [item.name for item in dataclasses.fields(limits.ResourceLimits)]


# ----------------------------------------------------------------- directives

GENERATORS = {
    "settings-page": lambda: settings_table("page"),
    "settings-layout": lambda: settings_table("layout"),
    "settings-typography": lambda: settings_table("typography"),
    "settings-behaviour": lambda: settings_table("behaviour"),
    "settings-theme": lambda: settings_table("theme"),
    "settings-limits": lambda: settings_table("limits"),
    "theme-tokens": theme_token_table,
    "builtin-themes": builtin_theme_table,
    "font-sets": font_set_table,
    "contrast-rules": contrast_rule_table,
    "cli-help": lambda: "```text\n" + cli_help() + "\n```",
}

MARKER = "<!-- platefold:generate {name} -->"


def expand(markdown: str) -> str:
    """Replace each generation marker with its generated block.

    An unknown marker is an error rather than a silent blank, for the same reason
    an unknown settings key is.
    """
    out = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("<!-- platefold:generate ") and stripped.endswith("-->"):
            name = stripped[len("<!-- platefold:generate ") : -len("-->")].strip()
            if name not in GENERATORS:
                raise DocsReferenceError(f"unknown generation marker {name!r}")
            out.append(GENERATORS[name]())
            continue
        out.append(line)
    return "\n".join(out) + ("\n" if markdown.endswith("\n") else "")
