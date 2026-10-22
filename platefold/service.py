"""Export application service shared by command-line and graphical clients.

The service neither parses arguments nor writes to process stdout. Each call owns
its browser and reports progress through the supplied callback. Callers must
serialize access to a shared cache/output directory or provide isolated ones.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import stat
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from platefold.browser import launch as browser_launch
from platefold.cdp import CancelledError
from platefold.chunked import PartCache
from platefold.config import Settings, make_settings, validate_settings
from platefold.document import Document, effective_settings, finish_html
from platefold.images import ImageLoader
from platefold.ingest import SUPPORTED_SUFFIXES, ingest
from platefold.limits import ResourceLimitError
from platefold.mermaid import MermaidRenderer
from platefold.paths import (
    app_dir,
    cache_dir,
    config_dir,
    redact_host_paths,
    resource_root,
)
from platefold.pdf import add_bookmarks, map_headings_and_count, page_count, print_pdf
from platefold.theme import save_user_theme, theme_provenance
from platefold.timing import Timings

Progress = Callable[[int, int, str, dict | None], None]


@dataclass(frozen=True)
class ExportRequest:
    input_path: str
    output_dir: str
    settings: Settings = field(default_factory=make_settings)
    browser: str = "auto"
    diagram_cache: str | None = None
    clear_cache: bool = False
    timings: bool = False
    save_theme: str | None = None


@dataclass(frozen=True)
class ExportResult:
    report: dict
    report_path: str
    output_dir: str
    total_documents: int
    cancelled: bool
    elapsed_seconds: float
    timings: Timings

    @property
    def exit_code(self) -> int:
        return 2 if self.cancelled else 0

    @property
    def status(self) -> str:
        if self.cancelled:
            return "cancelled"
        if any(
            d["missing_images"] or d["diagram_errors"] or d["warnings"]
            for d in self.report["documents"]
        ):
            return "completed_with_warnings"
        return "completed"

    def as_dict(self) -> dict:
        return {
            "schema_version": 1,
            "command": "export",
            "status": self.status,
            "exit_code": self.exit_code,
            "report_path": self.report_path,
            "output_dir": self.output_dir,
            "total_documents": self.total_documents,
            "elapsed_seconds": self.elapsed_seconds,
            "report": self.report,
        }


@dataclass(frozen=True)
class PlannedDocument:
    source: str
    relative: str
    destination: str


@dataclass(frozen=True)
class ExportPlan:
    source_root: str
    output_root: str
    report_path: str
    documents: list[PlannedDocument]


@dataclass
class RenderContext:
    browser: Any
    page: Any
    source_root: str
    output_root: str
    settings: Settings
    images: ImageLoader
    timings: Timings
    cancel_check: Callable[[], None]
    diagram_cache: str
    clear_cache: bool
    first_page_only: bool = False
    renderer: MermaidRenderer | None = None
    renderer_signature: str | None = None


@dataclass(frozen=True)
class PrintContext:
    page: Any
    destination: str
    relative: str
    settings: Settings
    timings: Timings
    cancel_check: Callable[[], None]
    info: dict
    first_page_only: bool = False
    # Parts of a document printed in several passes, kept for its second pass.
    parts: PartCache | None = None


def collect(target: str) -> tuple[list[str], str]:
    if os.path.isfile(target):
        return [target], os.path.dirname(target)
    root = Path(target).resolve()
    files = []
    # Do not follow directory links: an input tree can otherwise escape its
    # selected root or contain a recursive link that never finishes walking.
    for directory, _folders, names in os.walk(target, followlinks=False):
        for name in names:
            if Path(name).suffix.lower() not in SUPPORTED_SUFFIXES:
                continue
            path = Path(directory, name)
            if not path.resolve().is_relative_to(root):
                raise ValueError(f"Source file resolves outside the source folder: {path}")
            files.append(str(path))
    return sorted(files), target


def checked_output(root: str, relative: str) -> str:
    """Reject pre-existing links that redirect an export outside its destination.

    The destination is user-owned, not a sandbox against concurrent filesystem
    mutation. Server jobs must use private output directories.
    """
    boundary = Path(root).resolve()
    path = Path(root, relative)
    if not path.resolve().is_relative_to(boundary):
        raise ValueError(f"Output resolves outside the destination folder: {relative}")
    if path.is_symlink() or (path.exists() and path.stat().st_nlink > 1):
        raise ValueError(f"Output must not be a linked file: {relative}")
    return str(path)


def ignores_case(directory: str) -> bool:
    """Whether the filesystem holding ``directory`` treats names that differ only
    in case as one file.

    macOS and Windows volumes usually do, and Linux can too (a mounted FAT or
    SMB share, or a case-folding ext4 folder), so this is measured rather than
    inferred from the operating system. The nearest existing folder whose name
    has letters is looked up again with its case swapped. The destination may
    not exist yet, so the answer is for the volume it would be created on.
    """
    path = Path(os.path.abspath(directory))
    while not path.exists() and path != path.parent:
        path = path.parent
    for candidate in (path, *path.parents):
        swapped = candidate.with_name(candidate.name.swapcase()) if candidate.name else candidate
        if swapped != candidate:
            try:
                return os.path.samefile(candidate, swapped)
            except OSError:
                return False
    return os.path.normcase("A") == os.path.normcase("a")


def plan_outputs(files: list[str], source_root: str, output_root: str) -> list[PlannedDocument]:
    """Resolve every output before rendering and reject filename collisions.

    Where the destination ignores case, ``Notes.md`` and ``notes.csv`` would
    both write one ``notes.pdf``, the second replacing the first, so names are
    compared the way that filesystem compares them.
    """
    planned: list[PlannedDocument] = []
    destinations: dict[str, str] = {}
    fold = ignores_case(output_root)
    for source in files:
        relative = os.path.relpath(source, source_root)
        output_relative = os.path.splitext(relative)[0] + ".pdf"
        destination = checked_output(output_root, output_relative)
        key = os.path.normcase(os.path.abspath(destination))
        if fold:
            key = key.casefold()
        if previous := destinations.get(key):
            raise ValueError(f"Source files map to the same output PDF: {previous} and {relative}")
        destinations[key] = relative
        planned.append(PlannedDocument(source, relative, destination))
    return planned


def prepare_export(request: ExportRequest, max_documents: int | None) -> ExportPlan:
    """Validate a request and resolve all outputs before creating its directory."""
    validate_settings(request.settings)
    target = os.path.abspath(request.input_path)
    if not os.path.exists(target):
        raise ValueError(f"Input not found: {target}")
    files, source_root = collect(target)
    if max_documents is not None:
        if type(max_documents) is not int or max_documents < 1:
            raise ValueError("max_documents must be a positive integer")
        files = files[:max_documents]
    if not files:
        raise ValueError(
            "No readable source files found under %s. Supported formats: %s"
            % (target, ", ".join(sorted(SUPPORTED_SUFFIXES)))
        )
    output_root = os.path.abspath(request.output_dir)
    report_path = checked_output(output_root, "_export-report.json")
    documents = plan_outputs(files, source_root, output_root)
    return ExportPlan(source_root, output_root, report_path, documents)


@contextlib.contextmanager
def staged_output(destination: str):
    """Yield a same-directory temporary path and remove it unless committed."""
    directory = os.path.dirname(destination)
    fd, staged = tempfile.mkstemp(prefix=".platefold-", suffix=".tmp", dir=directory)
    os.close(fd)
    try:
        yield staged
    finally:
        with contextlib.suppress(OSError):
            os.remove(staged)


def replace_staged(staged: str, destination: str) -> None:
    """Atomically publish a completed output without changing its permissions."""
    try:
        mode = stat.S_IMODE(os.stat(destination).st_mode)
    except FileNotFoundError:
        # mkstemp's owner-only mode is safer than guessing the process umask.
        mode = stat.S_IMODE(os.stat(staged).st_mode)
    os.chmod(staged, mode)
    os.replace(staged, destination)


def validated_page_count(pdf_path: str) -> int:
    """Parse a staged PDF before it is allowed to replace an existing output."""
    count = page_count(pdf_path)
    if count < 1:
        raise ValueError("Rendered PDF contains no pages")
    return count


def _print_pass(context: PrintContext, html: str, destination: str | None = None) -> None:
    with context.timings.phase("print pdf"):
        arguments = (
            context.page,
            html,
            destination or context.destination,
            context.info,
            context.relative,
            context.settings,
        )
        if context.first_page_only:
            print_pdf(*arguments, first_page_only=True)
        else:
            print_pdf(*arguments, parts=context.parts, cancel_check=context.cancel_check)
    context.cancel_check()


def _map_printed_headings(context: PrintContext, path: str) -> tuple[dict[str, int], int]:
    with context.timings.phase("map headings"):
        pages, count = map_headings_and_count(path, context.info)
    context.cancel_check()
    return pages, count


def _write_bookmarks(context: PrintContext, pages: dict[str, int]) -> None:
    # The rewrite also carries the accessibility repairs, so it runs even
    # when no outline is wanted.
    phase = "write bookmarks" if context.settings.bookmarks else "finish pdf"
    with context.timings.phase(phase):
        warning = add_bookmarks(
            context.destination, context.info, pages, outline=context.settings.bookmarks
        )
    if warning:
        context.info.setdefault("warnings", []).append(warning)


def _export_single_pass(context: PrintContext, template: str, *, map_pages: bool) -> dict:
    _print_pass(context, finish_html(template))
    if not map_pages:
        _write_bookmarks(context, {})
        return context.info | {"pages_map": {}, "page_count": None}
    pages, count = _map_printed_headings(context, context.destination)
    _write_bookmarks(context, pages)
    return context.info | {"pages_map": pages, "page_count": count}


def _export_with_toc(context: PrintContext, template: str) -> dict:
    """Measure heading pages, then print once with final contents numbers."""
    fd, measuring_pdf = tempfile.mkstemp(suffix=".pdf", prefix="platefold-measure-")
    os.close(fd)
    try:
        _print_pass(context, finish_html(template, markers=True), measuring_pdf)
        pages, count = _map_printed_headings(context, measuring_pdf)
    finally:
        with contextlib.suppress(OSError):
            os.remove(measuring_pdf)

    _print_pass(context, finish_html(template, toc_pages=pages))
    _write_bookmarks(context, pages)
    return context.info | {"pages_map": pages, "page_count": count}


def export_one(
    page,
    doc: Document,
    out_pdf: str,
    rel: str,
    settings: Settings,
    timings: Timings,
    cancel_check: Callable[[], None] | None = None,
    *,
    first_page_only: bool = False,
) -> dict:
    """Render one document and print it.

    A second print is only needed to fill real page numbers into a contents
    page. Everything else — the heading-to-page mapping for bookmarks — comes
    from the named destinations in whatever PDF we just printed, so a document
    without a contents page is printed exactly once.
    """
    cancel_check = cancel_check or (lambda: None)
    cancel_check()
    with timings.phase("render html"):
        template, info = doc.render()
    cancel_check()
    if first_page_only:
        context = PrintContext(page, out_pdf, rel, settings, timings, cancel_check, info, True)
        _print_pass(context, finish_html(template))
        return context.info | {"pages_map": {}, "page_count": None}

    parts = PartCache()
    context = PrintContext(
        page, out_pdf, rel, settings, timings, cancel_check, info, first_page_only, parts
    )
    try:
        if not (settings.bookmarks or settings.toc):
            return _export_single_pass(context, template, map_pages=False)

        if not info["has_toc"]:
            # Single pass: the delivered PDF is also the one we read the mapping
            # from. The injected anchors are empty and out of flow, so they leave
            # no visible or selectable trace.
            return _export_single_pass(context, template, map_pages=True)

        # Both passes paginate identically by construction; the measuring pass
        # supplies page numbers and count for the delivered document.
        return _export_with_toc(context, template)
    finally:
        parts.close()


def render_document(item: PlannedDocument, context: RenderContext) -> dict:
    """Render, validate, and atomically publish one planned document."""
    with context.timings.phase("read source"):
        source = ingest(item.source, context.settings, context.cancel_check)
    settings, theme_warnings = effective_settings(context.settings, source)
    if theme_warnings:
        source = replace(source, warnings=[*source.warnings, *theme_warnings])
    signature = settings.diagram_signature()
    if source.figures and (context.renderer is None or context.renderer_signature != signature):
        if context.renderer is not None:
            _close_renderer(context.renderer)
        with context.timings.phase("init mermaid"):
            context.renderer = MermaidRenderer(
                context.browser,
                resource_root(),
                context.diagram_cache,
                settings,
                timings=context.timings,
                cancel_check=context.cancel_check,
            )
        context.renderer_signature = signature
        if context.clear_cache:
            context.renderer.index.clear()
            context.clear_cache = False
    os.makedirs(os.path.dirname(item.destination), exist_ok=True)
    doc = Document(
        item.source,
        context.source_root,
        settings,
        context.renderer,
        context.images,
        cancel_check=context.cancel_check,
        source=source,
    )
    with staged_output(item.destination) as staged_pdf:
        info = export_one(
            context.page,
            doc,
            staged_pdf,
            item.relative,
            settings,
            context.timings,
            cancel_check=context.cancel_check,
            first_page_only=context.first_page_only,
        )
        context.cancel_check()
        size = os.path.getsize(staged_pdf)
        if size > context.settings.limits.max_pdf_bytes:
            raise ResourceLimitError(
                f"Rendered PDF exceeds the {context.settings.limits.max_pdf_bytes}-byte limit"
            )
        with context.timings.phase("validate pdf"):
            pages = validated_page_count(staged_pdf)
        context.cancel_check()
        # Catch a link introduced while the comparatively slow render ran.
        destination = checked_output(
            context.output_root, os.path.splitext(item.relative)[0] + ".pdf"
        )
        replace_staged(staged_pdf, destination)
    return {
        "file": item.relative,
        "pages": pages,
        "figures": info["figures"],
        "tiled_figures": info.get("tiled_figures", []),
        "missing_images": [source for status, source in info["images"] if status == "MISSING"],
        "diagram_errors": [
            source for status, source in info["images"] if status == "DIAGRAM_ERROR"
        ],
        "image_bytes_saved": info.get("bytes_saved", 0),
        "embedded_bytes": info.get("embedded_bytes", 0),
        "warnings": info.get("warnings", []),
        "conversion": info.get("conversion"),
        "theme": info.get("theme", settings.theme.name),
        "theme_source": info.get("theme_source")
        or theme_provenance(settings.theme, context.source_root, item.source),
        "font_set": info.get("font_set", settings.fonts.name),
        "font_resolution": info.get("font_resolution", []),
        "font_fallback": info.get(
            "font_fallback", {"status": "unverified", "characters_checked": 0, "fallbacks": []}
        ),
        "font_stacks": info.get(
            "font_stacks",
            {"body": settings.body_font, "head": settings.head_font, "mono": settings.mono_font},
        ),
    }


def _close_renderer(renderer) -> None:
    close = getattr(renderer, "close", None)
    if close is not None:
        close()
    else:
        renderer.save()


def report_for(engine: str, settings: Settings, documents: list[dict]) -> dict:
    return {
        "engine": engine,
        "platform": platform.system(),
        "settings": {
            "theme": "source" if settings.source_theme else settings.theme.name,
            "theme_mode": settings.theme.mode,
            "font_set": settings.fonts.name,
            "font_stacks": {
                "body": settings.body_font,
                "head": settings.head_font,
                "mono": settings.mono_font,
            },
            "page_width_mm": settings.page_width,
            "page_height_mm": settings.page_height,
            "output_mode": settings.output_mode,
            "margin_mode": settings.margin_mode,
            "page_margins_mm": list(settings.page_margins),
            "image_dpi": settings.image_dpi,
            "csv_header": settings.csv_header,
            "document_language": settings.document_language,
            "xlsx_sheets": list(settings.xlsx_sheets),
            "exact_margins": settings.margin_edges is not None,
            "inline_fit": settings.inline_fit,
            "plate_fit": settings.plate_fit,
            "max_custom_width_mm": settings.max_custom_width,
            "caption_px": settings.caption_px,
            "tile_overlap_mm": settings.tile_overlap,
            "body_size_pt": settings.body_size_pt,
            "mermaid_font_px": settings.mermaid_font_px,
            "mermaid_wrap_px": settings.mermaid_wrap_px,
            "resource_limits": asdict(settings.limits),
        },
        "documents": documents,
    }


def _host_roots(request: ExportRequest, plan: ExportPlan) -> list[tuple[str, str]]:
    """Folders a report names by label instead of by host path.

    Structured fields are already relative or provenance labels. Free-text
    diagnostics (an operating-system error, a converter's message) can still
    quote a path, so these folders are replaced wherever they appear.
    """
    return [
        (plan.output_root, "<output>"),
        (plan.source_root, "<source>"),
        (request.diagram_cache or cache_dir(), "<cache>"),
        (config_dir(), "<config>"),
        (resource_root(), "<application>"),
        (app_dir(), "<application>"),
        (tempfile.gettempdir(), "<temporary>"),
        (str(Path.home()), "~"),
    ]


def publish_report(plan: ExportPlan, report: dict) -> str:
    """Atomically write the report after rechecking its destination."""
    report_path = plan.report_path
    with staged_output(report_path) as staged_report:
        with open(staged_report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        report_path = checked_output(plan.output_root, "_export-report.json")
        replace_staged(staged_report, report_path)
    return report_path


def _make_render_context(
    browser,
    plan: ExportPlan,
    request: ExportRequest,
    timings: Timings,
    cancel_check: Callable[[], None],
    first_page_only: bool,
) -> RenderContext:
    page = browser.new_page(viewport={"width": 1200, "height": 900})
    page.emulate_media(media="print")
    return RenderContext(
        browser,
        page,
        plan.source_root,
        plan.output_root,
        request.settings,
        ImageLoader(request.settings.image_dpi, limits=request.settings.limits),
        timings,
        cancel_check,
        request.diagram_cache or cache_dir(),
        request.clear_cache,
        first_page_only,
    )


def _save_requested_theme(
    request: ExportRequest,
    plan: ExportPlan,
    cancel_check: Callable[[], None] | None = None,
) -> dict | None:
    if request.save_theme is None:
        return None
    if not request.settings.source_theme:
        raise ValueError("--save-theme requires --theme source")
    suffix = Path(plan.documents[0].source).suffix.lower() if len(plan.documents) == 1 else ""
    if suffix not in {".docx", ".xlsx"}:
        raise ValueError("--save-theme requires one DOCX or XLSX input, not a mixed folder")
    source = ingest(plan.documents[0].source, request.settings, cancel_check)
    if source.source_theme is None:
        raise ValueError("The selected OOXML document has no complete source theme to save")
    theme = source.source_theme.merged(**request.settings.source_theme_colors)
    path = save_user_theme(
        theme,
        request.save_theme,
        f"Source palette and requested fonts imported from {suffix[1:].upper()} by Plate|Fold.",
    )
    return {"name": request.save_theme, "path": str(path)}


def _render_documents(
    plan: ExportPlan,
    context: RenderContext,
    on_progress: Progress | None,
    summary: list[dict],
) -> None:
    """Render a plan in order, retaining completed records on cancellation."""
    total = len(plan.documents)
    for index, item in enumerate(plan.documents):
        context.cancel_check()
        if on_progress:
            on_progress(index, total, item.relative, None)
        record = render_document(item, context)
        summary.append(record)
        if on_progress:
            on_progress(index + 1, total, item.relative, record)


def _launch_export_browser(request: ExportRequest, cancel_check: Callable[[], None]):
    if request.browser == "system":
        return browser_launch(request.browser, cancel_check=cancel_check)
    return browser_launch(request.browser)


def _close_export_browser(browser, context: RenderContext | None) -> None:
    if context is not None and context.renderer is not None:
        with contextlib.suppress(Exception):
            _close_renderer(context.renderer)
    if browser is not None:
        browser.close()


def export(
    request: ExportRequest,
    on_progress: Progress | None = None,
    should_cancel: Callable[[], bool] | None = None,
    *,
    max_documents: int | None = None,
    first_page_only: bool = False,
    on_activity: Callable[[str], None] | None = None,
) -> ExportResult:
    """Export local documents with cancellation between major render operations.

    Input errors raise ValueError. Runtime/I/O errors propagate to the adapter.
    Completed exports and cooperative cancellation return a report. The service
    is synchronous; server adapters should run it in isolated worker processes.
    max_documents bounds preview work without changing the selected resource root.
    first_page_only limits Chromium output to page 1 and skips pagination metadata.
    """
    if on_activity:
        on_activity("prepare")
    plan = prepare_export(request, max_documents)
    started = time.monotonic()
    deadline = started + request.settings.limits.max_export_seconds
    summary: list[dict] = []
    cancelled = False
    saved_theme = None
    total = len(plan.documents)
    timings = Timings(enabled=request.timings, on_phase=on_activity)
    context: RenderContext | None = None

    def cancel_check() -> None:
        if should_cancel and should_cancel():
            raise CancelledError("Export cancelled")
        if time.monotonic() > deadline:
            raise ResourceLimitError(
                f"Export exceeded the {request.settings.limits.max_export_seconds:g}-second limit"
            )

    def browser_cancel_check() -> bool:
        # The CDP client calls this between 100 ms receive polls, so deadlines
        # and cancellation also interrupt a long-running browser command.
        cancel_check()
        return False

    browser = None
    engine = "not started"
    try:
        # Saving a source theme imports the whole document, so it runs under
        # the same cancel check and deadline as the export itself.
        saved_theme = _save_requested_theme(request, plan, cancel_check)
        os.makedirs(plan.output_root, exist_ok=True)
        cancel_check()
        with timings.phase("launch browser"):
            browser, engine = _launch_export_browser(request, cancel_check)
        if hasattr(browser, "set_cancel_callback"):
            browser.set_cancel_callback(browser_cancel_check)
        context = _make_render_context(
            browser, plan, request, timings, cancel_check, first_page_only
        )
        _render_documents(plan, context, on_progress, summary)
    except CancelledError:
        cancelled = True
    finally:
        _close_export_browser(browser, context)

    report = report_for(engine, request.settings, summary)
    if saved_theme is not None:
        report["saved_theme"] = {
            "name": saved_theme["name"],
            "theme_source": f"user:{saved_theme['name']}",
        }
    report = redact_host_paths(report, _host_roots(request, plan))
    # A cancellation before the first document commits has not changed the
    # destination. Keep its last successful report alongside its still-current
    # PDFs instead of replacing it with an empty report for the aborted run.
    # The returned result still describes this cancellation, and an empty path
    # makes clear that no report was published for it.
    report_path = publish_report(plan, report) if summary or not cancelled else ""
    return ExportResult(
        report,
        report_path,
        plan.output_root,
        total,
        cancelled,
        time.monotonic() - started,
        timings,
    )
