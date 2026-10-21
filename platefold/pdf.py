"""Print HTML to PDF, then post-process page numbers and outline bookmarks."""

from __future__ import annotations

import contextlib
import html
import os
import re
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter

from . import chunked, htmlplan, pdfjoin
from .cdp import CDPError
from .config import Settings
from .font_fallback import (
    FALLBACK_ERRORS,
    attribute_scan,
    merge_scans,
    record_font_fallback,
    report_font_fallback,
    scan_text,
    summarize_fallback,
)
from .font_resolution import (
    font_resolution,
    record_font_resolution,
    report_font_resolution,
    representative_texts,
)
from .i18n import translator
from .limits import ResourceLimitError
from .pdf_finish import finish_file, outline_nodes
from .pdf_semantics import finish as finish_semantics
from .pdfobjects import PdfFile, PdfShapeError, Ref
from .placeholders import strip_marker_text
from .style import footer_template, header_template

# A generated PDF is a handoff to another application and often another
# person. Keep ordinary web, email and same-document navigation, but do not
# carry file access, script-like URLs or OS-registered custom protocols into
# that artifact. Running this against the browser DOM catches links from both
# Markdown and authored raw HTML, including SVG anchors.
_SANITIZE_LINKS = """() => {
    const allowed = new Set(['http:', 'https:', 'mailto:']);
    let neutralized = 0;
    for (const element of document.querySelectorAll('a, area')) {
        let changed = false;
        for (const attribute of ['href', 'xlink:href']) {
            if (!element.hasAttribute(attribute)) continue;
            const target = (element.getAttribute(attribute) || '').trim();
            let safe = target.startsWith('#');
            if (!safe && target) {
                try { safe = allowed.has(new URL(target).protocol); } catch (_) { safe = false; }
            }
            if (!safe) {
                element.removeAttribute(attribute);
                changed = true;
            }
        }
        if (changed) {
            element.setAttribute('data-pf-neutralized', '');
            neutralized += 1;
        }
    }
    return neutralized;
}"""

# An image Chromium cannot decode prints as a small broken-image icon while
# the export otherwise succeeds. Ask the browser itself, after forcing lazy
# images to load: a completed HTML image with no natural width failed to
# decode, which covers any format or corruption the loader did not anticipate.
# Do not call decode() again after the page load: on a memory-constrained
# browser that can force a second full decode of a large, already-renderable
# raster and kill the renderer. Other sources are
# blocked by the renderer's content policy by design and are not reported. A broken <img> becomes
# the same warning box as an image missing from the source folder; an SVG
# <image> cannot hold that box, so it is only reported. Names come from the
# data-pf-image attribute the document embedder sets on every <img>. The
# parentheses make Page.evaluate call the function rather than return it.
_REPLACE_UNDECODED_IMAGES = """(async () => {
    const images = [...document.querySelectorAll('img, svg image')];
    const source = image => image instanceof HTMLImageElement
        ? image.getAttribute('src') || image.getAttribute('srcset')
        : image.href && image.href.baseVal;
    // Only embedded images: the renderer's policy blocks every other source.
    const pending = images.filter(
        image => (source(image) || '').trim().toLowerCase().startsWith('data:')
    );
    await Promise.all(pending.map(image => {
        if (!(image instanceof HTMLImageElement) || image.complete) return Promise.resolve();
        return new Promise(resolve => {
            image.addEventListener('load', resolve, {once: true});
            image.addEventListener('error', resolve, {once: true});
            if (image.loading === 'lazy') image.loading = 'eager';
            if (image.complete) resolve();
        });
    }));
    const broken = pending.map(
        image => image instanceof HTMLImageElement && image.complete && !image.naturalWidth
    );
    const names = [];
    pending.forEach((image, index) => {
        if (!broken[index]) return;
        const name = image.getAttribute('data-pf-image') ||
            (image instanceof HTMLImageElement ? 'image' : 'image in an inline SVG');
        names.push(name);
        if (!(image instanceof HTMLImageElement)) return;
        const box = document.createElement('span');
        box.className = 'img-missing';
        const title = document.createElement('strong');
        title.textContent = 'Image could not be displayed: ';
        const code = document.createElement('code');
        code.textContent = name;
        box.append(title, image.getAttribute('alt') || '', document.createElement('br'), code);
        image.replaceWith(box);
    });
    return names;
})"""


def replace_undecoded_images(page) -> list[str]:
    """Swap images the browser failed to decode for a warning box; name them."""
    return list(page.evaluate(_REPLACE_UNDECODED_IMAGES) or [])


def _record_undecoded(info: dict, names: list[str]) -> None:
    """Report each undecodable image once, however many passes printed it."""
    reported = info.setdefault("undecoded_images", [])
    for name in names:
        if name in reported:
            continue
        reported.append(name)
        info.setdefault("images", []).append(("MISSING", name))
        _warn(
            info,
            f"IMAGE_UNDECODABLE: The renderer could not decode {name}, so the PDF "
            "shows a warning box in its place. Convert it to PNG or JPEG.",
        )


# Generic inline spans are ignored by Chromium's accessibility tree even
# when their language differs. A semantic group keeps that boundary without
# changing layout. Do not replace an author's existing role.
_PREPARE_SEMANTICS = """() => {
    for (const span of document.querySelectorAll('span[lang]:not([role])')) {
        span.setAttribute('role', 'group');
    }
    let undescribedSVG = 0;
    for (const svg of document.querySelectorAll('svg')) {
        if (svg.closest('[aria-hidden="true"]')) continue;
        const label = svg.getAttribute('aria-label') ||
            (svg.getAttribute('aria-labelledby') || '').split(/\\s+/).map(
                id => document.getElementById(id)?.textContent || ''
            ).join(' ').trim() ||
            [...svg.querySelectorAll(':scope > title, :scope > desc')].map(
                node => node.textContent
            ).join(' ').trim();
        if (label) {
            svg.setAttribute('role', 'img');
            svg.setAttribute('aria-label', label);
        } else {
            svg.setAttribute('data-pf-undescribed', '');
            undescribedSVG += 1;
        }
    }
    const headerless = [...document.querySelectorAll('table')].filter(
        table => !table.querySelector('th')
    );
    headerless.forEach(table => table.setAttribute('data-pf-headerless', ''));
    return {headerless: headerless.length, undescribedSVG};
}"""


# A dense table's minimum-content width can exceed the chosen print sheet even
# with emergency wrapping. Fit the measured semantic table, preserving all cells
# and allowing vertical pagination; clipping would lose content and PDF tags.
_FIT_PRINT_TABLES = """() => {
    let fitted = 0;
    for (const section of document.querySelectorAll('.data-table[data-print-width]')) {
        const available = Number(section.dataset.printWidth) - 1;
        if (!Number.isFinite(available) || available <= 0) continue;
        section.style.width = available + 'px';
        for (const table of section.querySelectorAll('table')) {
            const measured = table.getBoundingClientRect().width;
            if (measured <= available + 0.1) continue;
            table.style.width = measured + 'px';
            table.style.zoom = available / measured;
            table.setAttribute('data-pf-fitted', '');
            fitted += 1;
        }
    }
    return fitted;
}"""


# A document printed in parts counts what the single-pass steps above mark,
# leaving out what another part prints: a continued table's own properties and
# the rows the next part prints again.
_COUNT_MARKS = """({repeated}) => {
    const table = [...document.querySelectorAll('table')].pop();
    const body = table && [...table.tBodies].find(body => !body.hasAttribute('data-pf-sizer'));
    if (repeated !== null && body) {
        [...body.rows].filter(row => !row.hasAttribute('data-pf-spacer'))
            .slice(repeated).forEach(row => row.setAttribute('data-pf-repeated', ''));
    }
    const kept = element => !element.closest('[data-pf-repeated], [data-pf-sizer]');
    const own = element => kept(element) && !element.matches('[data-pf-continued]');
    const count = (selector, test) => [...document.querySelectorAll(selector)].filter(test).length;
    return {
        fitted: count('[data-pf-fitted]', own),
        headerless: count('[data-pf-headerless]', own),
        undescribedSVG: count('[data-pf-undescribed]', kept),
        neutralized: count('[data-pf-neutralized]', kept),
    };
}"""

# Content another part prints: rows printed again, hidden copies of rows that
# size a split table's columns, a continued table's header.
_ELSEWHERE = "[data-pf-repeated], [data-pf-sizer], table[data-pf-continued] > thead, .pgmark"


# Two-column tables get a readable minimum cell width (see style.py). Marking
# them here, once, replaces a :has() selector whose invalidation made loading
# and printing a large table quadratic in its row count.
_MARK_TWO_COLUMN_TABLES = """() => {
    for (const table of document.querySelectorAll('table')) {
        if (table.querySelector(':scope > thead > tr > th:nth-child(2):last-child')) {
            table.classList.add('pf-two-column');
        }
    }
}"""


_TRIM_PREVIEW_TABLES = """() => {
    // A first-page preview does not need thousands of rows in print layout.
    // Keep enough for the tallest supported sheet and remove only semantic
    // source tables; ordinary authored HTML remains untouched.
    for (const body of document.querySelectorAll('.pf-data-table > tbody')) {
        const rows = Array.from(body.rows);
        const regular = rows.filter(row => Array.from(row.cells).every(
            cell => cell.colSpan === 1 && cell.rowSpan === 1
        ));
        const columns = Math.max(0, ...regular.map(row => row.cells.length));
        if (columns) {
            const sizing = document.createElement('tr');
            sizing.setAttribute('aria-hidden', 'true');
            sizing.style.visibility = 'collapse';
            for (let column = 0; column < columns; column += 1) {
                const widest = regular.reduce((best, row) => {
                    const cell = row.cells[column];
                    if (!cell) return best;
                    return !best || cell.textContent.length > best.textContent.length
                        ? cell : best;
                }, null);
                sizing.append(widest ? widest.cloneNode(true) : document.createElement('td'));
            }
            body.append(sizing);
        }
        rows.slice(128).forEach(row => row.remove());
    }
}"""


_BOUND_PREVIEW_LAYOUT = """height => {
    // Preview never needs pagination beyond the first sheet. Keep one spare
    // sheet in the layout box so content at the first break has the same flow,
    // while preventing a long table or document from fragmenting to the end.
    const limit = Math.ceil(height * 2);
    const bounded = limit + 'px';
    for (const element of Array.from(document.body.children)) {
        const rect = element.getBoundingClientRect();
        if (rect.top > limit) element.remove();
        else if (rect.bottom > limit) {
            element.style.maxHeight = Math.max(1, limit - rect.top) + 'px';
            element.style.overflow = 'hidden';
        }
    }
    for (const element of [document.documentElement, document.body]) {
        element.style.height = bounded;
        element.style.maxHeight = bounded;
        element.style.overflow = 'hidden';
    }
}"""


def neutralize_unsafe_links(page) -> int:
    """Remove link targets whose URI schemes are unsafe in a delivered PDF."""
    value = page.evaluate(_SANITIZE_LINKS)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def prepare_print_layout(page, s: Settings, first_page_only: bool) -> int:
    """Fit authored tables, bounding semantic source tables for preview."""
    if first_page_only:
        page.evaluate(_TRIM_PREVIEW_TABLES)
    page.evaluate(_MARK_TWO_COLUMN_TABLES)
    fitted = page.evaluate(_FIT_PRINT_TABLES)
    if first_page_only:
        page.evaluate(_BOUND_PREVIEW_LAYOUT, s.page_height * 96 / 25.4)
    return fitted if isinstance(fitted, int) and not isinstance(fitted, bool) else 0


def _running(s: Settings) -> bool:
    """Whether pages carry a running header and footer."""
    return s.margin_mode == "full" and s.margin_edges is None and s.output_mode != "tiled"


def _templates(
    info: dict, footer_label: str, s: Settings, first_page_only: bool
) -> tuple[str, str]:
    header = header_template(s).format(
        title=html.escape(info["title"]),
        version=html.escape("v" + info["version"] if info["version"] else ""),
    )
    footer = footer_template(s, include_total=not first_page_only).format(
        sub=html.escape(footer_label)
    )
    return header, footer


def _running_text(info: dict, footer_label: str, s: Settings) -> list[tuple[str, str]]:
    # Header and footer text, set in the heading font; page numbers are digits.
    if not _running(s):
        return []
    return [(s.head_font, f"{info['title']} v{info['version']} {footer_label} Page 0123456789 of")]


def _warn(info: dict, warning: str) -> None:
    if warning not in info.setdefault("warnings", []):
        info["warnings"].append(warning)


def _record_counts(info: dict, counts: dict) -> None:
    """Warnings for what the in-page steps fitted, found or neutralized."""
    if counts.get("fitted"):
        _warn(info, translator("en").plural("a11y.tables_fitted", counts["fitted"]))
    for key, message in (
        (
            "headerless",
            "table(s) have no authored headers; header relationships cannot be supplied.",
        ),
        (
            "undescribedSVG",
            "inline SVG(s) have no authored alternative; add a title and description.",
        ),
    ):
        if counts.get(key):
            _warn(info, f"{counts[key]} {message}")
    if counts.get("neutralized"):
        _warn(info, f"Neutralized {counts['neutralized']} link(s) with unsupported URI schemes")


def print_pdf(
    page,
    html_src: str,
    out_path: str,
    info: dict,
    footer_label: str,
    s: Settings,
    *,
    first_page_only: bool = False,
    parts: chunked.PartCache | None = None,
    cancel_check: Callable[[], None] | None = None,
):
    """Render one HTML document to a PDF file.

    With a ``parts`` cache, a document holding more table cells than one pass
    may lay out is printed in parts and joined (see chunked); the cache keeps
    the parts for a second pass over the same document.
    """
    if not first_page_only and parts is not None:
        layout = parts_layout(html_src, s)
        if layout is not None:
            _print_in_parts(page, layout, out_path, info, footer_label, s, parts, cancel_check)
            return
    # set_content waits for parsing and font loading together; print media is
    # set once when the page is created, since this page only ever prints.
    page.set_content(html_src, wait_until="load")
    _record_undecoded(info, replace_undecoded_images(page))
    report_font_resolution(page, s, info)
    report_font_fallback(page, info, _running_text(info, footer_label, s))
    counts = {"fitted": prepare_print_layout(page, s, first_page_only)}
    semantics = page.evaluate(_PREPARE_SEMANTICS)
    if isinstance(semantics, dict):
        counts.update(semantics)
    counts["neutralized"] = neutralize_unsafe_links(page)
    _record_counts(info, counts)
    header, footer = _templates(info, footer_label, s, first_page_only)
    page.pdf(
        path=out_path,
        prefer_css_page_size=True,  # required for mixed @page sizes
        print_background=True,
        display_header_footer=_running(s),
        header_template=header,
        footer_template=footer,
        max_bytes=s.limits.max_pdf_bytes,
        page_ranges="1" if first_page_only else None,
    )


_CELL_TAG = re.compile(r"<t[dh][\s>]")


def parts_layout(html_src: str, s: Settings) -> htmlplan.Layout | None:
    """The document indexed for printing in parts, or None for one pass."""
    budget = s.limits.max_cells_per_pass
    cells = len(_CELL_TAG.findall(html_src))
    if cells > s.limits.max_document_cells:
        raise ResourceLimitError(
            f"The document has {cells:,} table cells, more than the "
            f"{s.limits.max_document_cells}-cell limit; split the document or its tables"
        )
    if cells <= budget:
        return None
    try:
        # The measuring pass's text markers are a fallback for heading mapping;
        # a document printed in parts maps headings by named destinations
        # alone, so that both passes print the same parts and the second
        # reuses them.
        layout = htmlplan.scan(strip_marker_text(html_src))
        chunked.page_rules(layout.html)
    except htmlplan.Unsplittable:
        return None  # printed whole, as before; the ingest limits bound it
    if chunked.table_cells(layout) <= budget:
        return None
    chunked.check_unsplittable(layout, budget)
    return layout


def _print_in_parts(
    page,
    layout: htmlplan.Layout,
    out_path: str,
    info: dict,
    footer_label: str,
    s: Settings,
    cache: chunked.PartCache,
    cancel_check: Callable[[], None] | None,
) -> None:
    evidence = _PartEvidence(page, s, info, footer_label, layout.head)
    printer = chunked.Printer(
        print_part=evidence.print_part,
        printed=evidence.printed,
        load=evidence.load,
        evaluate=page.evaluate,
    )
    parts = chunked.print_parts(layout, s.limits.max_cells_per_pass, printer, cache)
    evidence.record()
    overlay = None
    if _running(s):
        overlay = cache.temporary("running.pdf")
        header, footer = _templates(info, footer_label, s, False)
        page.renew()
        sizes = pdfjoin.page_sizes(parts)
        rules = chunked.page_rules(layout.html)
        running_text = _running_text(info, footer_label, s)
        page.set_content(chunked.overlay_html(sizes, rules, layout.head, running_text))
        page.pdf(
            path=overlay,
            prefer_css_page_size=True,
            # The page background is the body's; the overlay draws only text.
            print_background=False,
            display_header_footer=True,
            header_template=header,
            footer_template=footer,
        )
    # The last tab holds the running-header pass; release its renderer's
    # memory before joining, which the export's Python process does alone.
    page.renew()
    try:
        pdfjoin.join(parts, out_path, overlay=overlay, cancel_check=cancel_check)
    finally:
        pdfjoin.forget_parts()
    if os.path.getsize(out_path) > s.limits.max_pdf_bytes:
        raise ResourceLimitError(f"Rendered PDF exceeds the {s.limits.max_pdf_bytes}-byte limit")


class _PartEvidence:
    """Print each part, and gather what one pass would have reported.

    Each part prints in a fresh tab, since a renderer keeps much of the
    memory a large layout took. Warnings are counted on each part's kept
    content. Font evidence is gathered from each part's text and checked once
    at the end, on a page with only the document's styles, so the checks do
    not lay out or capture a large page again.
    """

    def __init__(self, page, s: Settings, info: dict, footer_label: str, head: str):
        self.page = page
        self.s = s
        self.info = info
        self.head = head
        self.running_text = _running_text(info, footer_label, s)
        self.counts: dict[str, int] = {}
        self.texts: dict[str, str] = {}
        self.scans: list[dict] = []
        self.undecoded: list[str] = []
        self.fresh = True  # every part printed now, none reused from an earlier pass
        self.loaded = False
        self.used = False

    def _fresh_page(self) -> None:
        if self.used:
            self.page.renew()
        self.used = True

    def load(self, text: str) -> None:
        # Measured as it will print: an undecodable image as its warning box.
        self._fresh_page()
        self.page.set_content(text, wait_until="load")
        replace_undecoded_images(self.page)
        self.loaded = False

    def print_part(self, text: str, path: str) -> None:
        page = self.page
        self._fresh_page()
        page.set_content(text, wait_until="load")
        self.undecoded += replace_undecoded_images(page)
        prepare_print_layout(page, self.s, False)
        page.evaluate(_PREPARE_SEMANTICS)
        neutralize_unsafe_links(page)
        page.pdf(
            path=path,
            prefer_css_page_size=True,
            print_background=True,
            max_bytes=self.s.limits.max_pdf_bytes,
        )
        self.loaded = True

    def printed(self, cut_row: int | None) -> None:
        """A part is final: read its kept content while its page is still loaded."""
        if not self.loaded:
            self.fresh = False  # reused from the measuring pass
            return
        self.loaded = False
        counts = self.page.evaluate(_COUNT_MARKS, {"repeated": cut_row})
        for key, value in (counts or {}).items():
            self.counts[key] = self.counts.get(key, 0) + int(value)
        if len(self.texts) < 3:
            for role, text in representative_texts(self.page, _ELSEWHERE).items():
                self.texts.setdefault(role, text)
        running = [] if self.scans else self.running_text
        self.scans.append(scan_text(self.page, running, _ELSEWHERE))

    def record(self) -> None:
        """Warnings and font evidence, once every part has printed in this pass."""
        if not self.fresh:
            return  # the pass that printed the parts recorded them
        _record_counts(self.info, self.counts)
        _record_undecoded(self.info, self.undecoded)
        need_resolution = "font_resolution" not in self.info
        need_fallback = "font_fallback" not in self.info
        if not (need_resolution or need_fallback):
            return
        self.load(self.head + "</body></html>")
        if need_resolution:
            try:
                resolved = font_resolution(self.page, self.s, texts=self.texts)
            except (CDPError, KeyError, AttributeError, TypeError):
                resolved = None
            record_font_resolution(self.info, resolved)
        if need_fallback:
            try:
                attribution = attribute_scan(self.page, merge_scans(self.scans), preload=True)
                result = summarize_fallback([attribution])
            except FALLBACK_ERRORS:
                result = None
            record_font_fallback(self.info, result)


def _loose(token: str) -> re.Pattern:
    """Match a marker even if extraction sprinkles whitespace between glyphs."""
    return re.compile(r"\s*".join(re.escape(c) for c in token), re.I)


def _map_by_destinations(reader: PdfReader, info: dict) -> dict[str, int]:
    """Read heading pages straight out of the PDF's named destinations.

    Chromium names a destination after the HTML element id it points at, so
    this is an exact anchor -> page lookup: no text extraction, no prose
    matching, and repeated headings are unambiguous because ids are unique.

    Extracting text from every page instead used to account for roughly two
    thirds of a full export's runtime, which is why this path exists.
    """
    pages: dict[str, int] = {}
    try:
        named = reader.named_destinations
    except Exception:
        return pages
    if not named:
        return pages

    # pypdf keys these with a leading slash on some documents and not others.
    lookup = {str(name).lstrip("/"): dest for name, dest in named.items()}
    for anchor in info["order"]:
        dest = lookup.get(anchor)
        if dest is None:
            continue
        try:
            page = reader.get_destination_page_number(dest)
        except Exception:
            continue
        if page is not None:
            pages[anchor] = page + 1
    return pages


def page_count(pdf_path: str) -> int:
    """Pages in a PDF, read from its object index when Chromium wrote it."""
    try:
        return len(PdfFile(pdf_path).pages())
    except PdfShapeError:
        with PdfReader(pdf_path) as reader:
            return len(reader.pages)


def map_headings_and_count(pdf_path: str, info: dict) -> tuple[dict[str, int], int]:
    """Heading pages and page count, reading Chromium's output without pypdf.

    pypdf parses a whole large file before answering; the named destinations
    and the page list are a few objects. Any other file goes through pypdf.
    """
    try:
        pdf = PdfFile(pdf_path)
        found = _destination_pages(pdf)
        count = len(pdf.pages())
    except PdfShapeError:
        found, count = None, 0
    if found is not None:
        pages = {anchor: found[anchor] for anchor in info["order"] if anchor in found}
        if len(pages) == len(info["order"]):
            return pages, count
    with PdfReader(pdf_path) as reader:
        return map_headings(pdf_path, info, reader=reader), len(reader.pages)


def _destination_pages(pdf: PdfFile) -> dict[str, int] | None:
    """Named destination -> 1-based page, from /Dests or the /Names tree."""
    index = {num: position + 1 for position, num in enumerate(pdf.pages())}
    root = pdf.root
    entries: list[tuple[str, object]] = []
    if b"/Dests" in root:
        entries = [(_pdf_name(name), value) for name, value in pdf.get(root[b"/Dests"]).items()]
    elif b"/Dests" in pdf.get(root.get(b"/Names", {})):
        tree = pdf.get(root[b"/Names"])[b"/Dests"]
        entries = [(key.decode("utf-8", "replace"), value) for key, value in pdf.name_tree(tree)]
    else:
        return None
    pages: dict[str, int] = {}
    for name, value in entries:
        destination = pdf.get(value)
        if isinstance(destination, dict):
            destination = destination.get(b"/D")
        if isinstance(destination, list) and destination and isinstance(destination[0], Ref):
            page = index.get(destination[0].num)
            if page is not None:
                pages[name] = page
    return pages


def _pdf_name(name: bytes) -> str:
    raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), bytes(name)[1:])
    return raw.decode("utf-8", errors="replace")


def map_headings(pdf_path: str, info: dict, reader: PdfReader | None = None) -> dict[str, int]:
    """First page each heading lands on.

    Three strategies, cheapest first: the named destinations Chromium emits
    for linked heading ids, then the invisible per-heading text marker, then
    matching the heading prose. Only the last is ambiguous when a document
    repeats a heading, and it exists purely so that a browser change degrades
    to the old behaviour rather than losing the mapping entirely.

    An unmatched heading is simply dropped from the TOC and bookmarks. Pass
    `reader` to reuse an already-open PDF rather than parsing it twice.
    """
    try:
        if reader is None:
            reader = PdfReader(pdf_path)
    except Exception:
        return {}

    pages = _map_by_destinations(reader, info)
    if len(pages) == len(info["order"]):
        return pages

    # Fallback: only now pay for text extraction.
    try:
        texts = [p.extract_text() or "" for p in reader.pages]
    except Exception:
        return pages

    for anchor, token in (info.get("markers") or {}).items():
        if anchor in pages:
            continue
        pattern = _loose(token)
        for i, text in enumerate(texts):
            if pattern.search(text):
                pages[anchor] = i + 1
                break
    if len(pages) == len(info["order"]) and pages:
        return pages

    return _map_by_text(texts, info, pages)


def _map_by_text(texts: list[str], info: dict, pages: dict[str, int]) -> dict[str, int]:
    """Legacy fallback: locate headings by their rendered prose."""
    flat = [re.sub(r"\s+", " ", t).lower() for t in texts]
    start = 1 if info["has_toc"] else 0  # skip the contents page itself
    cursor = start
    for anchor in info["order"]:
        if anchor in pages:
            cursor = max(start, pages[anchor] - 1)
            continue
        probe = re.sub(r"\s+", " ", info["names"][anchor]).lower().strip()[:60]
        if not probe:
            continue
        found = next((i + 1 for i in range(cursor, len(flat)) if probe in flat[i]), None)
        if found is None:
            found = next((i + 1 for i in range(start, len(flat)) if probe in flat[i]), None)
        if found:
            pages[anchor] = found
            cursor = max(start, found - 1)
    return pages


def add_bookmarks(
    pdf_path: str, info: dict, pages: dict[str, int], *, outline: bool = True
) -> str | None:
    """Write an outline, properties and structure repairs; warn if skipped.

    With ``outline`` false the file is still rewritten for its properties and
    accessibility repairs (see pdf_semantics), without bookmarks. Chromium's
    output is finished in one streaming pass (see pdf_finish); any other file
    is finished through pypdf, which loads the whole document.
    """
    path = Path(pdf_path)
    staged = None
    try:
        fd, staged = tempfile.mkstemp(prefix=".platefold-", suffix=".tmp", dir=path.parent)
        os.close(fd)
        properties = _document_properties(info)
        try:
            count = len(PdfFile(pdf_path).pages())
            nodes = outline_nodes(info, pages, count) if outline else []
            finish_file(pdf_path, staged, info, properties, nodes)
        except PdfShapeError:
            _finish_with_pypdf(pdf_path, staged, info, pages, properties, outline=outline)

        # Prove the new file is readable before it replaces the rendered PDF.
        # A failed nicety must never damage the rendered PDF.
        if page_count(staged) < 1:
            raise ValueError("bookmark output contains no pages")
        os.chmod(staged, stat.S_IMODE(path.stat().st_mode))
        os.replace(staged, pdf_path)
    except Exception as exc:  # a nicety, never fatal: the printed PDF stays
        return ("Bookmarks skipped: %s" if outline else "PDF finishing skipped: %s") % exc
    finally:
        if staged is not None:
            with contextlib.suppress(OSError):
                os.remove(staged)
    return None


def _finish_with_pypdf(
    pdf_path: str,
    staged: str,
    info: dict,
    pages: dict[str, int],
    properties: dict[str, str],
    *,
    outline: bool,
) -> None:
    """Finish any PDF by loading it whole, for files pdf_finish cannot stream."""
    with PdfReader(pdf_path) as reader, PdfWriter() as writer:
        writer.clone_document_from_reader(reader)
        parents: dict[int, Any] = {}
        for anchor in info["order"] if outline else ():
            if anchor not in pages:
                continue
            lvl = info["levels"][anchor]
            idx = min(pages[anchor] - 1, len(reader.pages) - 1)
            parent = parents.get(lvl - 1) if lvl > 1 else None
            parents[lvl] = writer.add_outline_item(info["names"][anchor][:110], idx, parent=parent)
        writer.add_metadata(properties)
        finish_semantics(writer, info, properties)
        with open(staged, "wb") as fh:
            writer.write(fh)


def _document_properties(info: dict) -> dict[str, str]:
    """PDF document properties, from the `Key: value` metadata block."""
    props = {"/Title": info["title"], "/Producer": "platefold", "/Creator": "platefold"}
    if info.get("author"):
        props["/Author"] = info["author"]
    if info.get("subject"):
        props["/Subject"] = info["subject"]
    if info.get("version"):
        props["/Keywords"] = "version " + info["version"]
    return props
