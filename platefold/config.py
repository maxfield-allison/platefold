"""Page geometry, layout thresholds and the settings file.

All sizes are millimetres unless the name ends in `_px`. Chromium lays out at
96 CSS px per inch, so px <-> mm conversion goes through PX_PER_MM.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import yaml

from .limits import MAX_CONFIG_BYTES, ResourceLimits, read_limited_text
from .paper import PAPER_SIZES, local_paper, margins
from .theme import (
    DEFAULT_FONT_SET,
    DEFAULT_THEME,
    FontSet,
    Theme,
    get_font_set,
    get_theme,
    warn_on_low_contrast,
)
from .validation import validate

PX_PER_MM = 96 / 25.4

# Tiled print: the label strip below each tile window, in whole CSS px (7.4 mm).
TILE_LABEL_PX = 28

SETTINGS_FILENAMES = ("platefold.yml", "platefold.yaml", "platefold.json")
# The pre-release TOML settings file. It is never read; finding one stops the
# export with conversion steps, because exporting with default paper, theme and
# resource limits the user thought they had changed is worse than stopping.
LEGACY_SETTINGS_FILENAME = "export.toml"


class LegacySettingsError(ValueError):
    """A TOML settings file was found or named; TOML settings are no longer read."""


@dataclass
class PageSpec:
    """One entry in the page-size ladder."""

    name: str
    width: float
    height: float
    margin: float
    margin_mode: str = "full"
    margin_edges: list[float] | None = None

    @property
    def page_margins(self) -> tuple[float, float, float, float]:
        if self.margin_edges is not None:
            top, right, bottom, left = self.margin_edges
            return top, right, bottom, left
        return margins(self.margin_mode, self.margin, self.margin)

    @property
    def content_w_px(self) -> float:
        _, right, _, left = self.page_margins
        return (self.width - right - left) * PX_PER_MM

    @property
    def content_h_px(self) -> float:
        top, _, bottom, _ = self.page_margins
        return (self.height - top - bottom) * PX_PER_MM


def _default_plates() -> list[PageSpec]:
    return [
        PageSpec("plate-letter-l", 279.4, 215.9, 12),
        PageSpec("plate-a3-l", 420.0, 297.0, 14),
    ]


@dataclass
class Settings:
    output_mode: str = "online"
    margin_mode: str = "full"
    orientation: str = "portrait"
    # --- body page -------------------------------------------------------
    page_width: float = 215.9  # US Letter portrait
    page_height: float = 279.4
    margin: float = 15.0
    margin_bottom: float = 18.0  # extra room for the running footer
    margin_edges: list[float] | None = None  # explicit top, right, bottom, left on every page

    # --- plate ladder ----------------------------------------------------
    # Diagrams too wide for the body column get their own page. The first
    # entry that holds the diagram at >= plate_fit of natural size wins.
    plates: list[PageSpec] = field(default_factory=_default_plates)
    plate_fit: float = 0.85  # min scale accepted on a ladder page
    inline_fit: float = 0.80  # min scale accepted in the text column
    max_custom_width: float = 900.0  # cap on bespoke plate pages
    caption_px: float = 52  # vertical room reserved for a caption
    tile_overlap: float = 10.0  # mm of figure repeated where tiled sheets meet

    # --- appearance ------------------------------------------------------
    theme: Theme = field(default_factory=lambda: get_theme(DEFAULT_THEME))
    fonts: FontSet = field(default_factory=lambda: get_font_set(DEFAULT_FONT_SET))
    # ``source`` is resolved per input document after ingestion.  Keeping the
    # selected fallback theme here preserves mixed-folder and settings-file
    # behavior for formats without a source palette.
    source_theme: bool = False
    source_theme_colors: dict[str, str] = field(default_factory=dict)
    source_fonts: bool = True
    body_size_pt: float = 10.2

    # --- behaviour -------------------------------------------------------
    document_language: str = ""  # explicit source-language override; never interface locale
    soft_breaks: bool = True  # honour single newlines (Obsidian-style)
    # DOC/ODT/RTF through an installed LibreOffice or Pandoc. The HTTP server
    # turns this off unless its operator enables legacy_conversion, because
    # LibreOffice is not a sandbox and server users are remote.
    external_converters: bool = True
    csv_header: bool = True  # use the first CSV/TSV row as a repeating header
    xlsx_sheets: tuple[str, ...] = ()  # exact names or one-based workbook indexes
    toc: bool = True  # build a contents page at all
    toc_min_headings: int = 9  # ...but only above this many headings
    bookmarks: bool = True
    image_dpi: int = 300  # raster images are downsampled to this at print size
    mermaid_font_px: int = 16
    mermaid_wrap_px: int = 260
    limits: ResourceLimits = field(default_factory=ResourceLimits)

    # -- derived ----------------------------------------------------------
    @property
    def body_font(self) -> str:
        return self.fonts.body

    @property
    def head_font(self) -> str:
        return self.fonts.head

    @property
    def mono_font(self) -> str:
        return self.fonts.mono

    @property
    def accent(self) -> str:
        return self.theme.accent

    @property
    def body_page(self) -> PageSpec:
        return PageSpec(
            "body",
            self.page_width,
            self.page_height,
            self.margin,
            self.margin_mode,
            list(self.page_margins),
        )

    @property
    def content_w_px(self) -> float:
        _, right, _, left = self.page_margins
        return (self.page_width - right - left) * PX_PER_MM

    @property
    def content_h_px(self) -> float:
        top, _, bottom, _ = self.page_margins
        return (self.page_height - top - bottom) * PX_PER_MM

    @property
    def page_margins(self) -> tuple[float, float, float, float]:
        return self.margins_for(self.margin, self.margin_bottom)

    @property
    def tile_frame(self) -> tuple[int, int, int, int, int]:
        """Left, top, width, height and overlap of the tile window, in whole CSS px.

        Whole pixels keep every tile edge, mark and figure offset on the same
        grid, so Chromium never snaps one of them differently from the others.
        The window starts at or inside the margins and leaves room below it
        for the tile label.
        """
        top, right, bottom, left = self.page_margins
        x = math.ceil(left * PX_PER_MM - 1e-6)
        y = math.ceil(top * PX_PER_MM - 1e-6)
        width = math.floor((self.page_width - right) * PX_PER_MM + 1e-6) - x
        height = math.floor((self.page_height - bottom) * PX_PER_MM + 1e-6) - y - TILE_LABEL_PX
        return x, y, width, height, round(self.tile_overlap * PX_PER_MM)

    def margins_for(self, side: float, bottom: float) -> tuple[float, float, float, float]:
        if self.margin_edges is not None:
            top, right, bottom, left = self.margin_edges
            return top, right, bottom, left
        return margins(self.margin_mode, side, bottom)

    def diagram_signature(self) -> str:
        """Everything that changes how a mermaid diagram is *rendered*.

        The diagram cache is keyed on this alongside the diagram source. Font
        family and size change the measured geometry of a diagram, and that
        geometry feeds the placement ladder — so without this in the key, a
        font change silently reuses SVGs measured under the old settings and
        figures land on the wrong page size.
        """
        t = self.theme
        return "|".join(
            str(x)
            for x in (
                4,  # strict Mermaid security and resource policy; page-coloured edge labels
                self.fonts.head,
                self.mermaid_font_px,
                self.mermaid_wrap_px,
                t.page,
                t.diagram_fill,
                t.diagram_stroke,
                t.diagram_text,
                t.diagram_alt_fill,
                t.diagram_note_fill,
                t.diagram_note_stroke,
            )
        )


PRESETS: dict[str, dict[str, Any]] = {
    name: {"page_width": width, "page_height": height}
    for name, (width, height) in PAPER_SIZES.items()
}
PRESETS.update(
    {
        # Everything on one page size; wide diagrams simply scale down.
        "uniform-letter": {"plates": [], "max_custom_width": 0.0},
    }
)


def _resolve_preset(preset: str, overrides: dict[str, Any]) -> str:
    locale_hint = overrides.pop("locale_hint", None)
    if locale_hint is not None and not isinstance(locale_hint, str):
        raise ValueError("locale_hint: expected a locale string")
    if preset == "auto":
        preset = (
            "letter"
            if (overrides.get("output_mode") or "online") == "online"
            else local_paper(locale_hint)
        )
    if preset not in PRESETS:
        raise ValueError("unknown preset %r. Available: %s" % (preset, ", ".join(sorted(PRESETS))))
    return preset


def _normalize_xlsx_sheets(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError("xlsx_sheets: expected a list of sheet names or indexes")
    return tuple(value)


def _apply_xlsx_sheets(supplied: dict[str, Any], base: dict[str, Any]) -> None:
    value = supplied.pop("xlsx_sheets", None)
    if value is not None:
        base["xlsx_sheets"] = _normalize_xlsx_sheets(value)


def make_settings(preset: str = "auto", **overrides: Any) -> Settings:
    """Build Settings from a preset name plus explicit overrides.

    `theme` and `font_set` may be passed as names; anything else is set on the
    dataclass directly. `None` values are ignored so callers can pass optional
    CLI arguments straight through.
    """
    preset = _resolve_preset(preset, overrides)
    base: dict[str, Any] = dict(PRESETS[preset])
    supplied = {k: v for k, v in overrides.items() if v is not None}

    theme = supplied.pop("theme", None)
    if isinstance(theme, str):
        theme = get_theme(theme)
    if theme is not None:
        base["theme"] = theme

    fonts = supplied.pop("font_set", None)
    if fonts is None:
        fonts = supplied.pop("fonts", None)
    else:
        supplied.pop("fonts", None)
    if isinstance(fonts, str):
        fonts = get_font_set(fonts)
    if fonts is not None:
        base["fonts"] = fonts

    limits = supplied.pop("limits", None)
    if isinstance(limits, dict):
        limits = ResourceLimits(**limits)
    if limits is not None:
        base["limits"] = limits

    _apply_xlsx_sheets(supplied, base)

    unknown = set(supplied) - {f.name for f in Settings.__dataclass_fields__.values()}
    if unknown:
        raise ValueError("unknown setting(s): %s" % ", ".join(sorted(unknown)))
    base.update(supplied)
    settings = Settings(**base)
    if settings.orientation == "landscape":
        settings.page_width, settings.page_height = settings.page_height, settings.page_width
    settings.plates = [
        replace(p, margin_mode=settings.margin_mode, margin_edges=settings.margin_edges)
        for p in settings.plates
    ]
    validate_settings(settings)
    return settings


def _validate_numbers(settings: Settings, keys: tuple[str, ...], *, allow_zero: bool) -> None:
    """Validate a group of scalar settings that share the same lower bound."""
    for key in keys:
        value = getattr(settings, key)
        valid_type = not isinstance(value, bool) and isinstance(value, (int, float))
        if not valid_type or not math.isfinite(value):
            expectation = "finite nonnegative" if allow_zero else "finite positive"
            raise ValueError(f"{key}: expected a {expectation} number")
        outside_bound = value < 0 if allow_zero else value <= 0
        if outside_bound:
            expectation = "finite nonnegative" if allow_zero else "finite positive"
            raise ValueError(f"{key}: expected a {expectation} number")


def _validate_modes(settings: Settings) -> None:
    for key, choices in (
        ("output_mode", ("online", "print", "tiled")),
        ("margin_mode", ("full", "none", "bottom-right")),
        ("orientation", ("portrait", "landscape")),
    ):
        if getattr(settings, key) not in choices:
            raise ValueError(f"{key}: expected one of {', '.join(choices)}")


def _validate_document_language(value: str) -> None:
    from .accessibility import valid_language

    if not (value == "" or valid_language(value)):
        raise ValueError("document_language: expected a BCP 47 language tag string")


def validate_settings(settings: Settings) -> None:
    """Reject geometry and scalar values that cannot produce a usable page."""
    _validate_document_language(settings.document_language)
    _validate_modes(settings)
    _validate_margin_edges(settings.margin_edges)
    _validate_numbers(
        settings,
        (
            "page_width",
            "page_height",
            "body_size_pt",
            "image_dpi",
            "mermaid_font_px",
            "mermaid_wrap_px",
        ),
        allow_zero=False,
    )
    _validate_numbers(
        settings,
        (
            "margin",
            "margin_bottom",
            "max_custom_width",
            "caption_px",
            "tile_overlap",
            "toc_min_headings",
        ),
        allow_zero=True,
    )

    for key in ("inline_fit", "plate_fit"):
        value = getattr(settings, key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
            raise ValueError(f"{key}: expected a number greater than zero and at most one")
    for key in ("soft_breaks", "csv_header", "toc", "bookmarks", "source_theme", "source_fonts"):
        if not isinstance(getattr(settings, key), bool):
            raise ValueError(f"{key}: expected a boolean")
    if not isinstance(settings.limits, ResourceLimits):
        raise ValueError("limits: expected a ResourceLimits value")
    if (
        not isinstance(settings.xlsx_sheets, tuple)
        or len(settings.xlsx_sheets) > settings.limits.max_sheet_count
        or any(
            not isinstance(selector, str)
            or not selector
            or len(selector) > 128
            or any(character in "\r\n\x00" for character in selector)
            for selector in settings.xlsx_sheets
        )
    ):
        raise ValueError("xlsx_sheets: expected bounded non-empty sheet names or indexes")
    if not isinstance(settings.source_theme_colors, dict):
        raise ValueError("source_theme_colors: expected an object")
    # Reuse Theme.merged's exhaustive token and colour validation.
    settings.theme.merged(**settings.source_theme_colors)
    for key in ("image_dpi", "mermaid_font_px", "mermaid_wrap_px", "toc_min_headings"):
        if not isinstance(getattr(settings, key), int):
            raise ValueError(f"{key}: expected an integer")
    _validate_page_area(settings)
    settings.limits.validate()


def _validate_page_area(settings: Settings) -> None:
    if settings.content_w_px < 1 or settings.content_h_px <= max(1, settings.caption_px):
        raise ValueError("page margins and caption leave no usable content area")
    _validate_tiles(settings)
    if 0 < settings.max_custom_width <= 24:
        raise ValueError("max_custom_width: use zero to disable or greater than 24 mm for margins")
    _, right, _, left = settings.margins_for(12, 12)
    if settings.max_custom_width and settings.max_custom_width <= right + left:
        raise ValueError("max_custom_width: must exceed the combined left and right margins")


def _validate_tiles(settings: Settings) -> None:
    if settings.tile_overlap > 50:
        raise ValueError("tile_overlap: expected at most 50 mm")
    if settings.output_mode != "tiled":
        return
    _, _, width, height, overlap = settings.tile_frame
    if width < 1 or height < 1:
        raise ValueError("page margins leave no room for tiles and their labels")
    if 3 * overlap >= min(width, height):
        raise ValueError(
            "tile_overlap: must be less than a third of the tile's printable width and height"
        )


def _validate_margin_edges(edges: list[float] | None) -> None:
    if edges is None:
        return
    if not isinstance(edges, list) or len(edges) != 4:
        raise ValueError("margin_edges: expected four numbers in top, right, bottom, left order")
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0
        for v in edges
    ):
        raise ValueError("margin_edges: expected finite nonnegative numbers")


# ---------------------------------------------------------------------------
# Settings files: platefold.yml, platefold.yaml or platefold.json
# ---------------------------------------------------------------------------

# Maps a [section] in the settings file onto Settings field names. Anything
# not listed is rejected, so a misspelled key is an error rather than a
# setting that silently does nothing.
_FILE_SCHEMA: dict[str, dict[str, str]] = {
    "page": {
        "output_mode": "output_mode",
        "margin_mode": "margin_mode",
        "orientation": "orientation",
        "locale": "locale_hint",
        "width": "page_width",
        "height": "page_height",
        "margin": "margin",
        "margin_bottom": "margin_bottom",
        "margin_edges": "margin_edges",
    },
    "layout": {
        "inline_fit": "inline_fit",
        "plate_fit": "plate_fit",
        "max_custom_width": "max_custom_width",
        "caption_px": "caption_px",
        "tile_overlap": "tile_overlap",
    },
    "typography": {
        "font_set": "font_set",
        "body_size_pt": "body_size_pt",
    },
    "behaviour": {
        "document_language": "document_language",
        "soft_breaks": "soft_breaks",
        "csv_header": "csv_header",
        "sheets": "xlsx_sheets",
        "toc": "toc",
        "toc_min_headings": "toc_min_headings",
        "bookmarks": "bookmarks",
        "image_dpi": "image_dpi",
        "mermaid_font_px": "mermaid_font_px",
        "mermaid_wrap_px": "mermaid_wrap_px",
    },
}


def _decode_settings(text: str, format: str) -> dict[str, Any]:
    """Decode one supported settings syntax into a section mapping."""
    if format == "toml":
        raise LegacySettingsError(legacy_settings_message())
    if format == "json":
        data = json.loads(text)
    elif format in ("yml", "yaml"):
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise ValueError(f"invalid YAML: {exc}") from None
        if data is None:
            data = {}
    else:
        raise ValueError(f"unsupported settings format: {format}")
    if not isinstance(data, dict):
        raise ValueError("settings must be an object of sections")
    return {key: value for key, value in data.items() if key != "$schema"}


def _reject_unknown_sections(data: dict[str, Any]) -> None:
    known = set(_FILE_SCHEMA) | {"theme", "limits"}
    unknown = set(data) - known
    if not unknown:
        return
    raise ValueError(
        "unknown section(s): %s. Valid sections: %s"
        % (", ".join(sorted(unknown)), ", ".join(sorted(known)))
    )


def _mapped_overrides(data: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Map ordinary file sections onto Settings field names."""
    preset = None
    overrides: dict[str, Any] = {}
    for section, mapping in _FILE_SCHEMA.items():
        block = data.get(section, {})
        if not isinstance(block, dict):
            raise ValueError(f"{section}: expected an object")
        block = dict(block)
        if section == "page":
            preset = block.pop("preset", None)
        unknown = set(block) - set(mapping)
        if unknown:
            valid = sorted(mapping) + (["preset"] if section == "page" else [])
            raise ValueError(
                "unknown key(s) in %s: %s. Valid keys: %s"
                % (section, ", ".join(sorted(unknown)), ", ".join(valid))
            )
        overrides.update((mapping[key], value) for key, value in block.items())
    return preset, overrides


def _theme_override(data: dict[str, Any], root: str | Path | None) -> Theme | None:
    block = data.get("theme", {})
    if not isinstance(block, dict):
        raise ValueError("theme: expected an object")
    block = dict(block)
    if not block:
        return None
    base_name = block.pop("base", DEFAULT_THEME)
    if not isinstance(base_name, str):
        raise ValueError("theme.base: expected a name or path")
    theme = get_theme(base_name, root)
    if not block:
        return theme
    custom = replace(theme.merged(**block), name=f"{base_name}+custom")
    warn_on_low_contrast(custom, "settings file theme section")
    return custom


def _limits_override(data: dict[str, Any]) -> ResourceLimits | None:
    block = data.get("limits", {})
    if not isinstance(block, dict):
        raise ValueError("limits: expected an object")
    if not block:
        return None
    try:
        return ResourceLimits(**block)
    except TypeError as exc:
        raise ValueError(f"limits: {exc}") from None


def parse_settings_file(
    text: str, format: str = "yaml", root: str | Path | None = None
) -> tuple[str | None, dict[str, Any]]:
    """Parse YAML or JSON settings into (preset, overrides for make_settings).

    `format` is the file suffix without its dot: ``yml``, ``yaml`` or ``json``.
    Raises ValueError on an unknown section or key, naming the offender, and
    LegacySettingsError for ``toml``.
    """
    if len(text.encode("utf-8")) > MAX_CONFIG_BYTES:
        raise ValueError(f"settings content exceeds the {MAX_CONFIG_BYTES}-byte limit")
    data = _decode_settings(text, format)
    _reject_unknown_sections(data)
    preset, overrides = _mapped_overrides(data)

    if theme := _theme_override(data, root):
        overrides["theme"] = theme
    if limits := _limits_override(data):
        overrides["limits"] = limits

    validate(data, "settings")
    return preset, overrides


def load_settings_file(path: str) -> tuple[str | None, dict[str, Any]]:
    """Read and parse a settings file, prefixing errors with the file name."""
    if Path(path).suffix.lower() == ".toml":
        raise LegacySettingsError(legacy_settings_message(path))
    try:
        text = read_limited_text(
            path, MAX_CONFIG_BYTES, label="Settings file", encoding="utf-8-sig"
        )
        return parse_settings_file(text, Path(path).suffix.lstrip(".").lower(), Path(path).parent)
    except (ValueError, OSError) as exc:
        raise ValueError(f"{path}: {exc}") from None


def legacy_settings_message(
    path: str | Path | None = None, current: list[Path] | None = None
) -> str:
    """Explain that TOML settings are no longer read, and how to move off them."""
    name = str(path) if path is not None else LEGACY_SETTINGS_FILENAME
    legacy = Path(name).name
    if current:
        target = current[0].name if len(current) == 1 else "your YAML or JSON settings file"
        return (
            f"{name}: Plate|Fold no longer reads TOML settings, so it stopped rather than "
            f"ignore this file. Copy any setting it has that {target} lacks into {target}, "
            f"then delete {legacy}."
        )
    return (
        f"{name}: Plate|Fold no longer reads TOML settings, so it stopped rather than export "
        "without them. Settings now live in platefold.yml, platefold.yaml or platefold.json, "
        "with the same sections and keys. To convert, write each [section] "
        "line as 'section:', indent each 'key = value' line beneath it as 'key: value' with "
        "the value exactly as written, save the result as platefold.yml in the same folder, "
        f"then delete {legacy}. The configuration reference has a worked example."
    )


def settings_files(root: str | Path) -> list[Path]:
    """Return the settings files beside the documents, refusing a legacy TOML file.

    More than one result means the caller must ask for an explicit choice.
    """
    root = Path(root)
    found = [root / name for name in SETTINGS_FILENAMES if (root / name).is_file()]
    legacy = root / LEGACY_SETTINGS_FILENAME
    if legacy.is_file():
        raise LegacySettingsError(legacy_settings_message(legacy, found))
    return found
