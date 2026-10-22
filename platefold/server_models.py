"""The v1 HTTP contract and operator-owned limits. No local-path client fields."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.requests import Request

from platefold.accessibility import valid_language
from platefold.config import PRESETS, make_settings
from platefold.limits import MIB, ResourceLimits
from platefold.theme import COLOR_TOKENS, discover_themes, get_font_set


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def no_query(request: Request, allowed: set[str] | None = None) -> None:
    keys = list(request.query_params.keys())
    if set(keys) - (allowed or set()) or any(
        len(request.query_params.getlist(k)) != 1 for k in keys
    ):
        raise APIError(400, "invalid_query", "Unexpected or repeated query parameter")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ExportOptions(StrictModel):
    preset: str = "auto"
    output_mode: Literal["online", "print", "tiled"] = "online"
    margin_mode: Literal["full", "none", "bottom-right"] = "full"
    orientation: Literal["portrait", "landscape"] = "portrait"
    locale_hint: str = Field(default="", max_length=128)
    document_language: str = Field(default="", max_length=255)
    page_width: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    page_height: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    margin_edges: list[Annotated[float, Field(ge=0, allow_inf_nan=False)]] | None = Field(
        default=None, min_length=4, max_length=4
    )
    inline_fit: float | None = Field(default=None, gt=0, le=1)
    plate_fit: float | None = Field(default=None, gt=0, le=1)
    max_custom_width: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    caption_px: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    tile_overlap: float | None = Field(default=None, ge=0, le=50, allow_inf_nan=False)
    body_size_pt: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    mermaid_font_px: int | None = Field(default=None, gt=0)
    mermaid_wrap_px: int | None = Field(default=None, gt=0)
    theme: str = Field(default="bristol", max_length=48)
    colors: dict[str, str] = Field(default_factory=dict, max_length=32)
    font_set: str = Field(default="sora", max_length=135)
    image_dpi: int = Field(default=150, ge=36, le=600)
    toc: bool = True
    bookmarks: bool = True
    csv_header: bool = True
    xlsx_sheets: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(
        default_factory=list, max_length=128
    )

    @field_validator("document_language")
    @classmethod
    def authored_language(cls, value: str) -> str:
        if value and not valid_language(value):
            raise ValueError("Expected a BCP 47 document language")
        return value

    @field_validator("preset")
    @classmethod
    def known_paper(cls, value: str) -> str:
        if value != "auto" and value not in PRESETS:
            raise ValueError("Unknown paper size")
        return value

    @field_validator("theme")
    @classmethod
    def builtin_theme(cls, value: str) -> str:
        if value != "source" and value not in discover_themes(user=False):
            raise ValueError("Expected a built-in theme ID or source")
        return value

    @field_validator("font_set")
    @classmethod
    def known_fonts(cls, value: str) -> str:
        get_font_set(value)
        return value

    @field_validator("colors")
    @classmethod
    def palette(cls, value: dict[str, str]) -> dict[str, str]:
        import re

        if any(
            k not in COLOR_TOKENS or not re.fullmatch(r"#[0-9a-fA-F]{6}", v)
            for k, v in value.items()
        ):
            raise ValueError("Expected known color tokens with six-digit hex colors")
        return value

    def settings(self, limits: JobLimits):
        options = self.model_dump(exclude={"preset", "theme", "colors"})
        source = self.theme == "source"
        theme = discover_themes(user=False)["bristol"]
        if not source:
            theme = discover_themes(user=False)[self.theme].merged(**self.colors)
        elif "font_set" not in self.model_fields_set:
            options.pop("font_set", None)
        return make_settings(
            self.preset,
            theme=theme,
            source_theme=source,
            source_theme_colors=self.colors if source else {},
            source_fonts=source and "font_set" not in self.model_fields_set,
            external_converters=limits.legacy_conversion,
            limits=ResourceLimits(
                max_markdown_bytes=limits.file_bytes,
                max_csv_bytes=limits.file_bytes,
                max_archive_entries=limits.files,
                max_archive_expanded_bytes=limits.source_bytes,
                max_image_bytes=limits.file_bytes,
                max_image_pixels=16_000_000,
                max_gif_frames=100,
                max_embedded_bytes=64 * MIB,
                max_mermaid_source_bytes=MIB,
                max_mermaid_svg_bytes=8 * MIB,
                max_pdf_bytes=limits.output_bytes,
                max_export_seconds=float(limits.export_seconds),
                max_sheet_cells=limits.table_cells,
                max_workbook_cells=limits.table_cells,
                max_csv_cells=limits.table_cells,
                max_document_cells=limits.table_cells,
                max_xlsx_xml_nodes=max(1_000_000, limits.table_cells * 6),
                max_cells_per_pass=limits.cells_per_pass,
            ),
            **options,
        )


class UploadSource(StrictModel):
    kind: Literal["upload"]
    format: Literal["markdown", "doc", "docx", "odt", "rtf", "xlsx", "csv", "tsv", "zip"]


class MountSource(StrictModel):
    kind: Literal["mount"]
    mount: str = Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")
    path: str = Field(default="", max_length=512)


class JobRequest(StrictModel):
    source: Annotated[UploadSource | MountSource, Field(discriminator="kind")]
    settings: ExportOptions = Field(default_factory=ExportOptions)


_MIN_CELLS_PER_PASS = 2_000


@dataclass(frozen=True)
class JobLimits:
    """Finite defaults; HTTP requests cannot override any of these values."""

    upload_bytes: int = 16 * MIB
    source_bytes: int = 64 * MIB
    file_bytes: int = 16 * MIB
    files: int = 128
    documents: int = 32
    output_bytes: int = 64 * MIB
    scratch_bytes: int = 256 * MIB
    scratch_files: int = 4096
    memory_bytes: int = 1024 * MIB
    processes: int = 64
    cpu_seconds: int = 120
    export_seconds: int = 180
    upload_seconds: int = 60
    download_seconds: int = 60
    retention_seconds: int = 3600
    concurrent_jobs: int = 2
    max_jobs: int = 32
    owner_jobs: int = 8
    store_bytes: int = 1024 * MIB
    # Table cells in one document. Larger tables print in browser passes sized
    # from memory_bytes, so this bounds export time rather than memory.
    table_cells: int = 100_000
    # Run an installed LibreOffice or Pandoc on uploaded or mounted DOC/ODT/RTF.
    # Off by default: LibreOffice is not a filesystem or network sandbox, and a
    # server's users are remote. Enable only inside an administered sandbox.
    legacy_conversion: bool = False

    def __post_init__(self):
        if type(self.legacy_conversion) is not bool:
            raise ValueError("legacy_conversion must be true or false")
        if any(
            type(getattr(self, f.name)) is not int or getattr(self, f.name) <= 0
            for f in fields(self)
            if f.name != "legacy_conversion"
        ):
            raise ValueError("Job limits must be positive integers")
        if self.owner_jobs > self.max_jobs or self.concurrent_jobs > self.max_jobs:
            raise ValueError("Per-owner/concurrency limits cannot exceed total job slots")

    @property
    def cells_per_pass(self) -> int:
        """Table cells one browser pass may lay out within ``memory_bytes``.

        Measured on the reference runner: an export's Python process takes
        about 256 MiB, and Chromium about 32 KiB per table cell of the pass it
        is printing. Two fifths of the limit are left as headroom. A limit too
        small for any useful pass still prints in small passes, and the
        guardian stops the export if it does not fit.
        """
        room = self.memory_bytes * 3 // 5 - 256 * MIB
        return max(_MIN_CELLS_PER_PASS, room // (32 * 1024))


@dataclass(frozen=True)
class Mount:
    path: Path
    owners: frozenset[str]


TERMINAL = frozenset({"completed", "completed_with_warnings", "failed", "cancelled", "interrupted"})
