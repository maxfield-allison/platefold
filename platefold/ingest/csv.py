"""Read delimiter-separated text into a bounded semantic HTML table."""

from __future__ import annotations

import csv
import html
import io
import sys
import threading
import unicodedata
from pathlib import Path

from ..cancellation import CancelCheck, checked, never_cancelled
from ..config import Settings
from ..limits import ResourceLimitError, ResourceLimits, read_limited_text
from . import Ingested

_SNIFF_BYTES = 64 * 1024
_DELIMITERS = ",\t;|"
_FIELD_LIMIT_LOCK = threading.Lock()


def _dialect(text: str, suffix: str) -> type[csv.Dialect]:
    """Sniff a familiar text dialect, with a suffix-specific safe fallback."""
    sample = text[:_SNIFF_BYTES]
    try:
        return csv.Sniffer().sniff(sample, delimiters=_DELIMITERS)
    except csv.Error:
        return csv.excel_tab if suffix == ".tsv" else csv.excel


def _display_width(value: str) -> int:
    """Approximate rendered character width without depending on a font engine."""
    widest = 0
    for line in value.splitlines() or [""]:
        width = sum(
            0
            if unicodedata.combining(character)
            else 2
            if unicodedata.east_asian_width(character) in {"W", "F"}
            else 1
            for character in line.expandtabs(4)
        )
        widest = max(widest, width)
    return widest


def _cell(value: str, tag: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    scope = ' scope="col"' if tag == "th" else ""
    return f"<{tag}{scope}>" + html.escape(value).replace("\n", "<br>") + f"</{tag}>"


def _append_row(rows: list[list[str]], row: list[str], cells: int, limits: ResourceLimits) -> int:
    logical_row = len(rows) + 1
    if logical_row > limits.max_csv_rows:
        raise ResourceLimitError(f"Delimited table exceeds the {limits.max_csv_rows}-row limit")
    if len(row) > limits.max_csv_columns:
        raise ResourceLimitError(
            f"Delimited table exceeds the {limits.max_csv_columns}-column limit"
        )
    if rows and len(row) != len(rows[0]):
        raise ValueError(
            f"Delimited table row {logical_row} has {len(row)} fields; expected {len(rows[0])}"
        )
    cells += len(row)
    if cells > limits.max_csv_cells:
        raise ResourceLimitError(
            f"Delimited table exceeds the {limits.max_csv_cells}-cell limit; split the file"
        )
    rows.append(row)
    return cells


def _parse(
    text: str, settings: Settings, suffix: str, cancel_check: CancelCheck = never_cancelled
) -> list[list[str]]:
    limits = settings.limits
    # The stdlib's field ceiling is process-global. Hold a small module lock so
    # concurrent thread-level callers cannot observe or overwrite our value.
    with _FIELD_LIMIT_LOCK:
        previous_limit = csv.field_size_limit()
        csv.field_size_limit(min(limits.max_csv_field_characters, sys.maxsize))
        try:
            dialect = _dialect(text, suffix)
            reader = csv.reader(
                io.StringIO(text, newline=""),
                delimiter=dialect.delimiter,
                # Sniffer can misclassify doubled quotes when the quoted field
                # spans lines. The literal pair is unambiguous CSV evidence.
                doublequote=dialect.doublequote
                or bool(dialect.quotechar and dialect.quotechar * 2 in text[:_SNIFF_BYTES]),
                escapechar=dialect.escapechar,
                quotechar=dialect.quotechar,
                quoting=dialect.quoting,
                skipinitialspace=dialect.skipinitialspace,
                strict=True,
            )
            rows: list[list[str]] = []
            cells = 0
            for row in checked(reader, cancel_check):
                # The reader returns [] only for an empty physical line, which
                # is skipped. A quoted empty field ("") is [''], a real record
                # holding one empty value, and is kept.
                if not row:
                    continue
                cells = _append_row(rows, row, cells, limits)
        except csv.Error as exc:
            message = str(exc) or "invalid quoting or escaping"
            if "field larger than field limit" in message:
                raise ResourceLimitError(
                    "Delimited table field exceeds the "
                    f"{limits.max_csv_field_characters}-character limit"
                ) from None
            raise ValueError(f"Malformed delimited table: {message}") from None
        finally:
            csv.field_size_limit(previous_limit)
    if not rows:
        raise ValueError("Delimited table contains no rows")
    return rows


def ingest(path: str, settings: Settings, cancel_check: CancelCheck | None = None) -> Ingested:
    source = Path(path)
    cancel_check = cancel_check or never_cancelled
    try:
        text = read_limited_text(
            source,
            settings.limits.max_csv_bytes,
            label="Delimited table",
            encoding="utf-8-sig",
        )
    except UnicodeDecodeError as exc:
        raise ValueError(f"{source}: delimited tables must be UTF-8 text") from exc
    if any(character in text for character in ("\x00", "\x0b", "\x0c")):
        raise ValueError(f"{source}: delimited table contains unsupported control characters")

    rows = _parse(text, settings, source.suffix.lower(), cancel_check)
    widths = [0] * len(rows[0])
    for row in checked(rows, cancel_check):
        for index, value in enumerate(row):
            widths[index] = max(widths[index], _display_width(value))
    # Keep long values wrappable while ensuring many short columns still route
    # to a wide page. Document uses this width only for the page ladder; rows
    # remain normal table flow and can continue across pages.
    natural_width = sum(min(28, max(8, width)) * 7 + 16 for width in widths)

    header = rows[0] if settings.csv_header else None
    body_rows = rows[1:] if header is not None else rows
    thead = (
        "<thead><tr>" + "".join(_cell(value, "th") for value in header) + "</tr></thead>"
        if header is not None
        else ""
    )
    tbody = (
        "<tbody>"
        + "".join(
            "<tr>" + "".join(_cell(value, "td") for value in row) + "</tr>"
            for row in checked(body_rows, cancel_check)
        )
        + "</tbody>"
    )
    title = source.stem
    return Ingested(
        body_html=(
            '<section class="pf-data-section">'
            f'<h1 id="table">{html.escape(title)}</h1>'
            f'<table class="pf-data-table" data-natural-width="{natural_width}">'
            f"{thead}{tbody}</table></section>"
        ),
        outline=[(1, title, "table")],
    )
