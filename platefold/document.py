"""Turn one ingested source file into a print-ready HTML document."""

from __future__ import annotations

import html
import math
import os
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from . import table_spans
from .accessibility import diagram_alternative, document_language, fill_empty_cells
from .config import Settings
from .contrast import describe_failure
from .html_images import ImageTags
from .html_resources import preflight
from .images import ImageLoader
from .ingest import Ingested, ingest
from .layout import IMAGE_FRAME_PX, plan_figure
from .limits import ResourceLimitError
from .mermaid import digest
from .placeholders import Placeholders, strip_marker_text
from .style import build_css
from .theme import theme_provenance, warn_on_low_contrast
from .tiles import assembly_note, label_html, marks_svg

# Every embedded <img> names its source here, so the render check in pdf.py
# can report an image Chromium failed to decode by the name the author used.
IMAGE_SOURCE = "data-pf-image"
INLINE_IMAGE = "inline data image"


def _is_svg_data(uri: str) -> bool:
    return uri[5:].split(",", 1)[0].split(";", 1)[0].strip().lower() == "image/svg+xml"


HEADING_TAG = re.compile(r'(<h([1-6])\s+id="([^"]+)"[^>]*>)')


class Template(str):
    """One rendered document, still carrying its render's placeholders.

    Figure numbers are filled in once the whole document is assembled, so
    numbering follows reading order rather than the order the figure kinds are
    processed in. Contents page numbers are filled in per print pass by
    `finish_html`. Both use the render's own `Placeholders`, whose key
    authored text cannot contain.
    """

    placeholders: Placeholders

    def __new__(cls, html: str, placeholders: Placeholders) -> Template:
        template = super().__new__(cls, html)
        template.placeholders = placeholders
        return template


def finish_html(
    template: str, toc_pages: dict[str, int] | None = None, markers: bool = False
) -> str:
    """Turn one rendered template into a specific print pass.

    The two passes differ only in whether the invisible text markers are
    present and whether the contents page carries real page numbers — both
    localised substitutions. Rendering the document once and deriving both
    from it avoids converting the markdown, rendering the figures and
    re-encoding every image a second time.

    Neither substitution changes layout: the markers are out of flow, and the
    number column is fixed-width.
    """
    pages = toc_pages or {}
    out = str(template)
    # A plain string carries no generated placeholders to fill in.
    placeholders = getattr(template, "placeholders", None)
    if placeholders is not None:
        out = placeholders.pages.sub(lambda m: str(pages.get(m.group(1), "")), out)
    if not markers:
        # The delivered PDF drops the marker text so it can never turn up in a
        # copy-paste.
        out = strip_marker_text(out)
    return out


def marker_for(anchor: str) -> str:
    """A short, alphanumeric, extraction-safe token identifying one heading.

    Only used by the fallback path in `pdf.py`. The primary mechanism is the
    named destination Chromium emits for any linked heading id.
    """
    return "Zq" + digest(anchor)[:8]


class Document:
    """One source file, rendered to HTML plus the metadata the PDF step needs."""

    def __init__(
        self,
        source_path: str,
        root: str,
        settings: Settings,
        renderer,
        images: ImageLoader | None = None,
        cancel_check: Callable[[], None] | None = None,
        source: Ingested | None = None,
    ):
        self.path = source_path
        self.root = root
        self.s = settings
        self.renderer = renderer
        self.images = (
            images
            if images is not None
            else ImageLoader(settings.image_dpi, limits=settings.limits)
        )
        self.cancel_check = cancel_check or (lambda: None)
        self.source = source
        self.slug = re.sub(r"\W+", "", os.path.basename(source_path))[-12:] or "doc"

    # -- per-render state -------------------------------------------------
    def _reset(self) -> None:
        self.custom_pages: dict[str, tuple[float, float, float]] = {}
        self.image_report: list[tuple[str, str]] = []
        self.fig_count = 0
        self.tiled_figures: list[dict] = []
        self.bytes_saved = 0
        self.embedded_bytes = 0
        self._plate_seq = 0
        self.placeholders = Placeholders.unused_in(())

    def _reserve_embedded(self, amount: int, source: str) -> None:
        total = self.embedded_bytes + amount
        if total > self.s.limits.max_embedded_bytes:
            raise ResourceLimitError(
                f"Embedded assets exceed the {self.s.limits.max_embedded_bytes}-byte "
                f"document limit at {source}"
            )
        self.embedded_bytes = total

    # -- figures ----------------------------------------------------------
    def _mermaid_figure(self, fig: dict, idx: int) -> str:
        self.cancel_check()
        rec = self.renderer.render(fig["code"])
        self.cancel_check()
        if "error" in rec:
            self.image_report.append(("DIAGRAM_ERROR", rec["error"]))
            return (
                '<div class="fig-missing"><strong>Diagram could not be '
                "rendered:</strong> %s</div>" % html.escape(rec["error"])
            )
        svg = self.renderer.svg_text(rec)
        self._reserve_embedded(len(svg.encode("utf-8")), "Mermaid diagram")
        svg = re.sub(r'viewBox="[^"]*"', 'viewBox="%s"' % rec["viewBox"], svg, count=1)
        # Give each occurrence a unique root id so mermaid's scoped <style>
        # blocks don't collide when a diagram appears twice in one document.
        old = re.search(r'<svg id="([^"]+)"', svg)
        if old:
            svg = re.sub(r"\b%s\b" % re.escape(old.group(1)), "fig%s%d" % (self.slug, idx), svg)
        plan = plan_figure(rec["w"], rec["h"], self.s)
        svg = re.sub(r'^<svg([^>]*?)\swidth="[^"]*"', r"<svg\1", svg, count=1)
        svg = re.sub(r'^<svg([^>]*?)\sheight="[^"]*"', r"<svg\1", svg, count=1)
        width, height = rec["w"] * plan["scale"], rec["h"] * plan["scale"]
        if plan["mode"] == "tiled":
            size = f'width="{width:.4f}" height="{height:.4f}"'
        else:
            # Chromium paints the SVG box on whole pixels. A height rounded down
            # would make the diagram's aspect-ratio fit shrink its width, so the
            # height is rounded up and the planned width is drawn exactly.
            size = f'width="{width:.4f}" height="{math.ceil(height - 1e-6)}"'
        svg = svg.replace("<svg", f"<svg {size}", 1)
        alternative = diagram_alternative(svg)
        if not alternative:
            self.image_report.append(
                (
                    "DIAGRAM_ALTERNATIVE",
                    "Diagram has no authored alternative. Add Mermaid accTitle and accDescr.",
                )
            )
        return self._wrap(
            svg, plan, fig["head"], alternative=alternative or "Diagram: author description missing"
        )

    def _wrap(
        self,
        inner: str,
        plan: dict,
        label: str,
        *,
        alternative: str | None = None,
        decorative: bool = False,
    ) -> str:
        """Wrap figure content, leaving the figure number to be filled in later."""
        semantic = ' role="presentation"'
        if decorative:
            semantic = ' aria-hidden="true"'
        elif alternative is not None:
            semantic = ' role="img" aria-label="%s"' % html.escape(alternative, quote=True)
            inner = re.sub(r"<(svg|img)\b", r'<\1 aria-hidden="true"', inner, count=1)
        cap = html.escape(label or "")
        caption = "<figcaption>Figure %s%s</figcaption>" % (
            self.placeholders.fignum,
            " &middot; " + cap if cap else "",
        )
        if plan["mode"] == "inline":
            return '<figure class="fig"%s>%s%s</figure>' % (semantic, inner, caption)
        if plan["mode"] == "tiled":
            return self._tiles(inner, plan, caption, semantic, cap)
        cls = plan.get("page")
        if cls is None:
            cls = "plate-%s-%d" % (self.slug, self._plate_seq)
            self._plate_seq += 1
            self.custom_pages[cls] = plan["custom"]
        return '<figure class="fig plate %s"%s>%s%s</figure>' % (cls, semantic, inner, caption)

    def _tiles(self, inner: str, plan: dict, caption: str, semantic: str, name: str) -> str:
        """Clip full-size vector/image copies in row order without rescaling.

        Tile (row, column) shows the figure from (column * step, row * step),
        where step is the window size less the overlap, so neighbouring sheets
        repeat the overlap strip. Each sheet carries its own label and marks.
        """
        width, height, overlap = plan["width"], plan["height"], plan["overlap"]
        step_x, step_y = width - overlap, height - overlap
        tiles = []
        ids = sorted(set(re.findall(r'\bid="([^"]+)"', inner)), key=len, reverse=True)
        references = (
            re.compile(r"(?<![\w-])(?:" + "|".join(re.escape(i) for i in ids) + r")(?![\w-])")
            if ids
            else None
        )
        for row in range(plan["rows"]):
            for column in range(plan["columns"]):
                self.cancel_check()
                fragment = inner
                if references:
                    # Only markup and style rules contain references. A label
                    # that happens to spell an element ID is still user text.
                    rewrite = references.sub
                    fragment = re.sub(
                        r"<style\b[^>]*>.*?</style>|<[^>]+>",
                        lambda m, rewrite=rewrite, suffix=f"-tile-{row}-{column}": rewrite(
                            r"\g<0>" + suffix, m[0]
                        ),
                        fragment,
                        flags=re.S,
                    )
                self._reserve_embedded(len(fragment.encode("utf-8")) + 2048, "diagram tiles")
                tiles.append(
                    f'<div class="tile-sheet"><div class="tile-window" '
                    f'style="left:{plan["left"]}px;top:{plan["top"]}px;'
                    f'width:{width}px;height:{height}px">'
                    f'<div class="tile-art" style="left:{-column * step_x}px;'
                    f'top:{-row * step_y}px">{fragment}</div></div>'
                    + marks_svg(plan, self.s, row, column)
                    + label_html(plan, row, column, self.placeholders.figref, name)
                    + "</div>"
                )
        self.tiled_figures.append(
            {
                "figure": None,
                "columns": plan["columns"],
                "rows": plan["rows"],
                "sheets": plan["columns"] * plan["rows"],
                "scale": plan["scale"],
            }
        )
        fignum = self.placeholders.fignum
        marker = self.placeholders.tileset(len(self.tiled_figures) - 1)
        return (
            '<figure class="tile-set"'
            + semantic
            + ">"
            + caption.replace(fignum, fignum + marker, 1)
            + assembly_note(plan, self.s.tile_overlap)
            + "".join(tiles)
            + "</figure>"
        )

    # -- images ------------------------------------------------------------
    def _image_semantics(self, attrs: dict, src: str) -> tuple[dict, bool, str | None]:
        decorative = attrs.get("alt") == "" or attrs.get("role") in {"none", "presentation"}
        if decorative:
            attrs = attrs | {"alt": "", "aria-hidden": "true"}
        elif not (attrs.get("alt") or "").strip():
            self.image_report.append(
                ("IMAGE_ALTERNATIVE", "Image has no authored alternative: " + src[:160])
            )
        alternative = None
        if self.s.output_mode == "tiled" and not decorative:
            alternative = attrs.get("alt") or "Image: author description missing"
        return attrs, decorative, alternative

    def _embed_images(self, body: str, assets: dict[str, bytes]) -> str:
        base_dir = os.path.dirname(self.path)

        def repl(attrs: dict[str, str | None]) -> str:
            self.cancel_check()
            src = (attrs["src"] or "").strip()
            attrs, decorative, alternative = self._image_semantics(attrs, src)
            if src.lower().startswith("data:"):
                return self._inline_image(attrs, src)
            label = attrs.get("alt") or ""
            if src in assets:
                raw = assets[src]
                measure = (
                    self.images.natural_size_bytes
                    if self.s.output_mode == "online"
                    else self.images.print_size_bytes
                )
                nat_w, nat_h = measure(src, raw)
                if not nat_w:
                    nat_w, nat_h = int(self.s.content_w_px), 0
                plan = plan_figure(
                    float(nat_w), float(nat_h or 1), self.s, frame=IMAGE_FRAME_PX, whole_px=True
                )
                placed_px = nat_w * plan["scale"]
                loaded = self.images.load_bytes(src, raw, placed_px)
                self._reserve_embedded(len(loaded.data_uri), src)
                if src.lower().endswith(".svg"):
                    self._validate_data_image(loaded.data_uri)
                self.bytes_saved += max(0, loaded.original_bytes - loaded.encoded_bytes)
                self.image_report.append(("OK", src))
                image = self._image_tag(
                    attrs
                    | {
                        "src": loaded.data_uri,
                        "style": (attrs.get("style") or "")
                        + self._placed_size(plan, placed_px, nat_w, nat_h, src),
                        IMAGE_SOURCE: src,
                    }
                )
                return self._wrap(
                    image, plan, label, alternative=alternative, decorative=decorative
                )
            # The selected source folder is the filesystem boundary, including
            # symlinks. URL-looking paths must not trigger a UNC/network read.
            decoded = urllib.parse.unquote(src)
            try:
                parsed = urllib.parse.urlsplit(decoded)
                allowed = (
                    not parsed.scheme and not parsed.netloc and not decoded.startswith(("\\", "/"))
                )
                path = Path(base_dir, decoded).resolve() if allowed else Path(self.root)
                allowed = allowed and path.is_relative_to(Path(self.root).resolve())
            except (ValueError, OSError):
                allowed = False
                path = Path(self.root)
            if not allowed or not path.is_file():
                self.image_report.append(("MISSING", src))
                name = html.escape(os.path.basename(urllib.parse.unquote(src)))
                return (
                    '<span class="img-missing"><strong>Image not found in the '
                    "source folder:</strong> %s<br><code>%s</code></span>"
                    % (html.escape(label) or name, name)
                )

            path = str(path)
            measure = (
                self.images.natural_size
                if self.s.output_mode == "online"
                else self.images.print_size
            )
            nat_w, nat_h = measure(path)
            if not nat_w:
                nat_w, nat_h = int(self.s.content_w_px), 0
            plan = plan_figure(
                float(nat_w), float(nat_h or 1), self.s, frame=IMAGE_FRAME_PX, whole_px=True
            )
            placed_px = nat_w * plan["scale"]
            loaded = self.images.load(path, placed_px)
            self._reserve_embedded(len(loaded.data_uri), src)
            if path.lower().endswith(".svg"):
                self._validate_data_image(loaded.data_uri)
            self.cancel_check()
            self.bytes_saved += max(0, loaded.original_bytes - loaded.encoded_bytes)
            self.image_report.append(("OK", src))
            img = self._image_tag(
                attrs
                | {
                    "src": loaded.data_uri,
                    "style": (attrs.get("style") or "")
                    + self._placed_size(plan, placed_px, nat_w, nat_h, path),
                    IMAGE_SOURCE: src,
                }
            )
            return self._wrap(img, plan, label, alternative=alternative, decorative=decorative)

        return ImageTags(body, repl).rewrite()

    def _inline_image(self, attrs: dict[str, str | None], src: str) -> str:
        self._reserve_embedded(len(src.encode("utf-8")), INLINE_IMAGE)
        # A top-level raster Chromium cannot draw is re-encoded, not reported.
        self._validate_data_image(src, convertible=not _is_svg_data(src))
        shown = self.images.displayable_data_uri(src)
        if shown is not src:
            self._reserve_embedded(max(0, len(shown) - len(src)), INLINE_IMAGE)
        return self._image_tag(attrs | {"src": shown, IMAGE_SOURCE: INLINE_IMAGE})

    @staticmethod
    def _placed_size(plan: dict, placed_px: float, nat_w: float, nat_h: float, name: str) -> str:
        """CSS size for a placed image.

        The planner gives whole-pixel widths, which Chromium paints exactly.
        An SVG file also gets its height rounded up to a whole pixel: Chromium
        paints the box on whole pixels, and a box rounded down would make the
        SVG's own aspect-ratio fit shrink its width to match. Tiles keep the
        exact natural width.
        """
        if plan["mode"] == "tiled":
            return f";width:{placed_px:.4f}px"
        width = round(placed_px)
        size = f";width:{width}px"
        if nat_h and name.lower().endswith(".svg"):
            size += f";height:{math.ceil(width * nat_h / nat_w - 1e-6)}px"
        return size

    def _validate_data_image(self, uri: str, *, convertible: bool = False) -> None:
        """Validate an inline image; report rasters Chromium cannot draw.

        A top-level ``<img>`` raster in such a format is ``convertible``: the
        caller re-encodes it. One nested in an SVG, a stylesheet or a srcset
        cannot be, and would print as nothing, so it becomes a coded warning.
        """

        def undecodable(image_format: str) -> None:
            if convertible:
                return
            warning = (
                f"An embedded {image_format} image cannot be displayed by the renderer "
                "and is missing from the PDF. Convert it to PNG or JPEG."
            )
            if ("IMAGE_UNDECODABLE", warning) not in self.image_report:
                self.image_report.append(("IMAGE_UNDECODABLE", warning))

        self.images.validate_data_uri(uri, reserve=self._reserve_embedded, undecodable=undecodable)

    @staticmethod
    def _image_tag(attrs: dict[str, str | None]) -> str:
        return (
            "<img "
            + " ".join(
                key if value is None else f'{key}="{html.escape(value, quote=True)}"'
                for key, value in attrs.items()
            )
            + ">"
        )

    # -- headings ----------------------------------------------------------
    def _inject_markers(self, body: str, anchors: set[str]) -> str:
        """Make every mapped heading addressable in the printed PDF.

        Chromium emits a PDF *named destination* for any element id that is
        the target of an internal link, keyed by the id itself. Injecting a
        link to each heading therefore hands `pdf.py` an exact anchor -> page
        mapping to read straight out of the PDF, with no text extraction and
        no ambiguity between repeated headings.

        Documents with a contents page already link every heading; this makes
        the guarantee unconditional, so short documents map just as precisely.

        Both elements are absolutely positioned and transparent: out of flow,
        so they cannot shift pagination, and invisible in the measuring pass
        that is thrown away. The text marker is carried too, purely as a
        fallback for a browser that stops emitting destinations.
        """

        def repl(m: re.Match) -> str:
            tag, anchor = m.group(1), m.group(3)
            if anchor not in anchors:
                return tag
            return tag + '<a class="pgmark" aria-hidden="true" tabindex="-1" href="#%s">%s</a>' % (
                anchor,
                marker_for(anchor),
            )

        return HEADING_TAG.sub(repl, body)

    # -- main --------------------------------------------------------------
    def render_html(
        self, toc_pages: dict[str, int] | None = None, markers: bool = False
    ) -> tuple[str, dict]:
        """Render straight to one finished print pass.

        Convenience wrapper around `render()` + `finish_html()`; the exporter
        renders once and derives both passes instead.
        """
        template, info = self.render()
        return finish_html(template, toc_pages, markers), info

    def _prepare_body(self, source: Ingested) -> str:
        """Validate the ingested HTML and turn its figure placeholders real."""
        preflight(source.body_html, self._validate_data_image, self._reserve_embedded)
        return self._render_figures(source.body_html, source.figures, source.placeholders)

    def _render_figures(
        self, body: str, figures: list[dict], placeholders: Placeholders | None
    ) -> str:
        """Replace the ingester's diagram placeholders in one pass.

        Only the ingester's own spelling is replaced, so a document that
        mentions a placeholder, or a diagram whose labels spell one, keeps
        that text.
        """
        if not figures:
            return body
        if placeholders is None:
            raise ValueError("An ingested source with diagrams must name its placeholders")
        fragments = [self._mermaid_figure(figure, index) for index, figure in enumerate(figures)]

        def place(match: re.Match) -> str:
            index = int(match[1] or match[2])
            return fragments[index] if index < len(fragments) else match[0]

        return placeholders.figures.sub(place, body)

    def _format_blocks(self, body: str) -> str:
        """Number figures and label blocks that need special pagination CSS."""
        body = re.sub(r"<p>\s*(<figure[^>]*>.*?</figure>)\s*</p>", r"\1", body, flags=re.S)
        counter = 0

        def number(match: re.Match) -> str:
            nonlocal counter
            if match[2] is not None:
                self.tiled_figures[int(match[2])]["figure"] = counter
                return ""
            if match[1] == "FIGNUM":
                counter += 1
            return str(counter)

        body = self.placeholders.numbers.sub(number, body)
        self.fig_count = counter
        body = self._format_data_tables(body)
        for table in re.findall(r"<table>.*?</table>", body, re.S):
            widest = max((len(row) for row in re.findall(r"<tr>.*?</tr>", table, re.S)), default=0)
            if table.count("<th") < 6 and widest <= 420:
                continue
            body = body.replace(table, '<div class="wide-table">%s</div>' % table, 1)
        for block in re.findall(r"<pre>.*?</pre>", body, re.S):
            if block.count("\n") <= 26:
                continue
            body = body.replace(block, block.replace("<pre>", '<pre class="long">', 1), 1)
        return body

    def _format_data_tables(self, body: str) -> str:
        """Route ingested tables through the figure page-width ladder.

        Height is deliberately representative rather than row-derived: a long
        narrow table belongs on normal body pages and must be free to paginate.
        """
        pattern = re.compile(
            r'<section class="pf-data-section">(?P<section>.*?'
            r'<table class="pf-data-table" data-natural-width="(?P<width>\d+)">'
            r".*?</table>)</section>",
            re.S,
        )

        def place(match: re.Match) -> str:
            width = float(match.group("width"))
            representative_height = min(600.0, max(1.0, self.s.content_h_px * 0.65))
            plan = plan_figure(
                width,
                representative_height,
                replace(self.s, output_mode="print") if self.s.output_mode == "tiled" else self.s,
            )
            section = match.group("section")
            if self.s.output_mode != "online":
                # Constrain the DOM to the real printable width before measuring
                # table overflow; the browser viewport is wider than a print sheet.
                width_attr = f' data-print-width="{self.s.content_w_px:.3f}"'
            else:
                width_attr = ""
            if plan["mode"] == "inline":
                return f'<section class="data-table inline-table"{width_attr}>{section}</section>'
            cls = plan.get("page")
            if cls is None:
                cls = "table-%s-%d" % (self.slug, self._plate_seq)
                self._plate_seq += 1
                self.custom_pages[cls] = plan["custom"]
            return f'<section class="data-table table-plate {cls}"{width_attr}>{section}</section>'

        return pattern.sub(place, body)

    def _toc_html(self, outline: list[tuple[int, str, str]], heading: str = "h2") -> str:
        """The contents page; ``heading`` is h1 when no title heading precedes it."""
        if not self.s.toc or len(outline) < self.s.toc_min_headings:
            return ""
        rows = "".join(
            '<li class="lvl%d"><a href="#%s"><span class="t">%s</span>'
            '<span class="dots"></span><span class="pg">%s</span></a></li>'
            % (level, anchor, html.escape(name), self.placeholders.page(anchor))
            for level, name, anchor in outline
        )
        return '<nav class="toc"><%s>Contents</%s><ol>%s</ol></nav>' % (heading, heading, rows)

    def _document_header(self, title: str | None, metadata: list[tuple[str, str]] | None) -> str:
        if not title:
            return ""
        section = os.path.dirname(os.path.relpath(self.path, self.root))
        section = section.replace(os.sep, " / ") or "Documents"
        metadata_html = ""
        if metadata:
            metadata_html = '<dl class="meta">%s</dl>' % "".join(
                "<dt>%s</dt><dd>%s</dd>" % (html.escape(key), html.escape(value))
                for key, value in metadata
            )
        return '<header class="doc-head"><div class="eyebrow">%s</div><h1>%s</h1>%s</header>' % (
            html.escape(section),
            html.escape(title),
            metadata_html,
        )

    def _assemble_page(self, title: str | None, header: str, toc: str, body: str) -> str:
        css = build_css(self.custom_pages, self.s)
        return (
            "<!doctype html><html lang=\"%s\"><head><meta charset='utf-8'>"
            '<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; '
            "img-src data:; style-src &#39;unsafe-inline&#39;; font-src data:; "
            'base-uri &#39;none&#39;; form-action &#39;none&#39;">'
            "<title>%s</title>"
            "<style>%s</style></head><body>%s%s%s</body></html>"
            % (
                html.escape(self.language, quote=True),
                html.escape(title or os.path.basename(self.path)),
                css,
                header,
                toc,
                body,
            )
        )

    def _render_info(
        self,
        title: str | None,
        metadata: list[tuple[str, str]] | None,
        outline: list[tuple[int, str, str]],
        has_toc: bool,
        conversion: dict | None,
    ) -> dict:
        metadata = metadata or []
        return {
            "title": title or os.path.splitext(os.path.basename(self.path))[0],
            "version": next((value for key, value in metadata if key.lower() == "version"), ""),
            "author": next(
                (value for key, value in metadata if key.lower() in ("owner", "author")), ""
            ),
            "subject": next((value for key, value in metadata if key.lower() == "document"), ""),
            "order": [entry[2] for entry in outline],
            "names": {entry[2]: entry[1] for entry in outline},
            "levels": {entry[2]: entry[0] for entry in outline},
            "markers": {entry[2]: marker_for(entry[2]) for entry in outline},
            "has_toc": has_toc,
            "figures": self.fig_count,
            "tiled_figures": self.tiled_figures,
            "images": self.image_report,
            "bytes_saved": self.bytes_saved,
            "embedded_bytes": self.embedded_bytes,
            "theme": self.s.theme.name,
            "theme_source": theme_provenance(self.s.theme, self.root, self.path),
            "font_set": self.s.fonts.name,
            "font_stacks": {
                "body": self.s.body_font,
                "head": self.s.head_font,
                "mono": self.s.mono_font,
            },
            "warnings": [
                f"{status}: {detail}"
                for status, detail in self.image_report
                if status not in {"OK", "MISSING", "DIAGRAM_ERROR"}
            ],
            "conversion": conversion,
        }

    @staticmethod
    def effective_settings(
        settings: Settings, source: Ingested
    ) -> tuple[Settings, list[tuple[str, str]]]:
        return effective_settings(settings, source)

    def render(self) -> tuple[Template, dict]:
        """Render the document once, as a template both passes derive from.

        The result still carries placeholders for contents page numbers and
        the text half of each heading marker; `finish_html` resolves those per
        pass.
        """
        self.cancel_check()
        self._reset()
        source = self.source or ingest(self.path, self.s, self.cancel_check)
        self.placeholders = Placeholders.unused_in(_authored_text(source, self.path))
        self.s, theme_warnings = self.effective_settings(self.s, source)
        self.language, language_warnings = document_language(
            self.s.document_language, source.language
        )
        self.image_report.extend(language_warnings)
        self.image_report.extend(source.warnings)
        self.image_report.extend(theme_warnings)
        self.cancel_check()
        body = self._prepare_body(source)
        self.cancel_check()
        body = self._embed_images(body, source.assets)
        self.cancel_check()
        body = fill_empty_cells(self._format_blocks(body))
        # Before printing and before any split into parts: spans as laid out (see table_spans).
        body = table_spans.normalize(
            body,
            self.cancel_check,
            warn=lambda code, detail: self.image_report.append((code, detail)),
            max_cells=self.s.limits.max_document_cells,
        )
        outline = source.outline
        body = self._inject_markers(body, {entry[2] for entry in outline})
        header = self._document_header(source.title, source.metadata)
        # Without a title heading the contents page comes first, and a level-2
        # heading there would skip level 1 for assistive technology.
        toc = self._toc_html(outline, "h2" if header else "h1")
        page = Template(self._assemble_page(source.title, header, toc, body), self.placeholders)
        info = self._render_info(
            source.title, source.metadata, outline, bool(toc), source.conversion
        )
        info["language"] = self.language
        return page, info


def _authored_text(source: Ingested, path: str) -> list[str]:
    """Every authored string a render can copy into its HTML.

    The file name is one: an untitled document is titled by it.
    """
    texts = [source.body_html, source.title or "", os.path.basename(path)]
    texts.extend(part for pair in source.metadata for part in pair)
    texts.extend(figure.get("code", "") for figure in source.figures)
    texts.extend(figure.get("head", "") for figure in source.figures)
    texts.extend(text for _, name, anchor in source.outline for text in (name, anchor))
    return texts


def effective_settings(
    settings: Settings, source: Ingested
) -> tuple[Settings, list[tuple[str, str]]]:
    """Resolve ``--theme source`` against this one input document."""
    if not settings.source_theme:
        return settings, []
    if source.source_theme is None:
        return replace(settings, source_theme=False), [
            (
                "SOURCE_THEME_FALLBACK",
                f"This input has no usable source theme; using {settings.theme.name!r}",
            )
        ]
    theme = source.source_theme.merged(**settings.source_theme_colors)
    failures = warn_on_low_contrast(theme, str(source.source_theme.source))
    warnings = [("SOURCE_THEME_CONTRAST", describe_failure(failure)) for failure in failures]
    fonts = settings.fonts
    if settings.source_fonts:
        if theme.fonts is not None:
            fonts = theme.fonts
        else:
            warnings.append(
                (
                    "SOURCE_FONT_FALLBACK",
                    f"The source theme has no usable typeface requests; using {fonts.label}",
                )
            )
    return replace(settings, theme=theme, fonts=fonts, source_theme=False), warnings
