"""Turn any supported input file into the one shape the exporter renders.

Everything after this layer is format-agnostic: the figure planner, image
embedder, plate pagination, heading markers, contents page, theme CSS and PDF
assembly all operate on HTML. Markdown used to reach them through a single
method on `Document`; this package makes that method one implementation of an
interface so other formats can join without touching the exporter.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings
from ..placeholders import Placeholders
from ..theme import Theme


@dataclass(frozen=True)
class Ingested:
    """One source file, converted to the exporter's inputs and nothing more.

    `body_html` is semantic HTML in Plate|Fold's own vocabulary. It still
    carries a placeholder for each diagram the ingester found, spelled by
    `placeholders.figure(n)`: rendering those needs the Mermaid renderer, the
    resource budget and the image report, all of which belong to `Document`,
    not to a format reader. The placeholders' key never occurs in the source,
    so authored text is never taken for a diagram position.
    """

    body_html: str
    title: str | None = None
    language: str | None = None
    metadata: list[tuple[str, str]] = field(default_factory=list)
    # (level, name, anchor), the shape the contents page and bookmarks want.
    outline: list[tuple[int, str, str]] = field(default_factory=list)
    # Diagram sources, in placeholder order.
    figures: list[dict] = field(default_factory=list)
    # How this source spells its diagram placeholders; needed with figures.
    placeholders: Placeholders | None = None
    # Images carried inside the source file rather than beside it, keyed by the
    # `src` the body uses. A format like .docx keeps its images in a zip, so
    # they can never be resolved against the input folder.
    assets: dict[str, bytes] = field(default_factory=dict)
    # A theme the source document carries, for `--theme source`.
    source_theme: Theme | None = None
    # (code, detail) pairs, merged into the document's image report.
    warnings: list[tuple[str, str]] = field(default_factory=list)
    # Machine-readable provenance for a bridge to another ingester. Human
    # diagnostics remain in warnings so future catalogs do not change this.
    conversion: dict[str, Any] | None = None


class Ingester(Protocol):
    """Read one file into the exporter's inputs.

    Readers call `cancel_check` between units of work (archive members, 64 KiB
    chunks, rows, blocks) and let whatever it raises propagate unchanged, so a
    cancellation or deadline is never reported as a malformed document.
    """

    def __call__(
        self,
        path: str,
        settings: Settings,
        cancel_check: Callable[[], None] | None = None,
    ) -> Ingested: ...


def _markdown(
    path: str, settings: Settings, cancel_check: Callable[[], None] | None = None
) -> Ingested:
    # Imported lazily so a future ingester's dependency is only paid for by
    # the formats that need it.
    from .markdown import ingest

    return ingest(path, settings, cancel_check)


def _csv(path: str, settings: Settings, cancel_check: Callable[[], None] | None = None) -> Ingested:
    from .csv import ingest

    return ingest(path, settings, cancel_check)


def _docx(
    path: str, settings: Settings, cancel_check: Callable[[], None] | None = None
) -> Ingested:
    from .docx import ingest

    return ingest(path, settings, cancel_check)


def _converter(
    path: str, settings: Settings, cancel_check: Callable[[], None] | None = None
) -> Ingested:
    from .converter import ingest

    return ingest(path, settings, cancel_check)


def _xlsx(
    path: str, settings: Settings, cancel_check: Callable[[], None] | None = None
) -> Ingested:
    from .xlsx import ingest

    return ingest(path, settings, cancel_check)


# Suffix -> reader. Extended one entry per format as each phase lands.
INGESTERS: dict[str, Ingester] = {
    ".md": _markdown,
    ".csv": _csv,
    ".tsv": _csv,
    ".docx": _docx,
    ".doc": _converter,
    ".odt": _converter,
    ".rtf": _converter,
    ".xlsx": _xlsx,
}

SUPPORTED_SUFFIXES = frozenset(INGESTERS)


class UnsupportedFormatError(ValueError):
    """The input file's suffix has no reader."""


def ingester_for(path: str | Path) -> Ingester:
    suffix = Path(path).suffix.lower()
    try:
        return INGESTERS[suffix]
    except KeyError:
        raise UnsupportedFormatError(
            "%s: Plate|Fold cannot read %s files. Supported formats: %s"
            % (Path(path).name, suffix or "extensionless", ", ".join(sorted(SUPPORTED_SUFFIXES)))
        ) from None


def ingest(
    path: str,
    settings: Settings,
    cancel_check: Callable[[], None] | None = None,
) -> Ingested:
    return ingester_for(path)(path, settings, cancel_check)


def source_theme_for_path(
    path: str | Path, settings: Settings
) -> tuple[Theme | None, list[tuple[str, str]]]:
    """Read a source palette from a supported OOXML document."""
    suffix = Path(path).suffix.lower()
    if suffix == ".docx":
        from .docx import source_theme_for_path as read_theme
    elif suffix == ".xlsx":
        from .xlsx import source_theme_for_path as read_theme
    else:
        return None, []
    return read_theme(path, settings)
