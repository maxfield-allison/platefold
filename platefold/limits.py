"""Resource envelopes for accidentally pathological or hostile documents."""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from pathlib import Path

MIB = 1024 * 1024

# A configuration file cannot safely raise the limit used to read itself.
MAX_CONFIG_BYTES = 1 * MIB


class ResourceLimitError(ValueError):
    """An input or output exceeded the configured resource envelope."""


@dataclass(frozen=True)
class ResourceLimits:
    """Generous defaults that bound work without constraining normal documents."""

    max_markdown_bytes: int = 64 * MIB
    max_html_tag_search_bytes: int = 16 * MIB
    max_paragraph_rebuild_bytes: int = 2048 * MIB
    max_csv_bytes: int = 64 * MIB
    max_csv_rows: int = 100_000
    max_csv_columns: int = 512
    # Tables above max_cells_per_pass print in parts (see chunked), so these
    # bound work and time rather than browser memory.
    max_csv_cells: int = 1_000_000
    max_csv_field_characters: int = 1 * MIB
    max_archive_entries: int = 4096
    max_archive_expanded_bytes: int = 512 * MIB
    max_docx_xml_nodes: int = 1_000_000
    max_xlsx_xml_nodes: int = 6_000_000
    max_sheet_cells: int = 1_000_000
    max_sheet_count: int = 128
    max_workbook_cells: int = 1_000_000
    max_document_cells: int = 1_000_000
    # Chromium lays out a whole document before printing any page, at about
    # 30 KB per table cell, so a document with more cells prints in parts.
    max_cells_per_pass: int = 25_000
    max_image_bytes: int = 128 * MIB
    max_image_pixels: int = 64_000_000
    max_gif_frames: int = 500
    max_embedded_bytes: int = 512 * MIB
    max_mermaid_source_bytes: int = 8 * MIB
    max_mermaid_svg_bytes: int = 64 * MIB
    max_pdf_bytes: int = 1024 * MIB
    max_export_seconds: float = 3600.0

    def validate(self) -> None:
        for item in fields(self):
            value = getattr(self, item.name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"limits.{item.name}: expected a positive number")
            if item.name != "max_export_seconds" and not isinstance(value, int):
                raise ValueError(f"limits.{item.name}: expected an integer")


def read_limited_text(
    path: str | Path,
    max_bytes: int,
    *,
    label: str,
    encoding: str = "utf-8",
) -> str:
    """Read one text file without allocating beyond its declared byte limit."""
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > max_bytes:
        raise ResourceLimitError(f"{label} exceeds the {max_bytes}-byte limit: {path}")
    with path.open("rb") as handle:
        raw = handle.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ResourceLimitError(f"{label} exceeds the {max_bytes}-byte limit: {path}")
    return raw.decode(encoding)
