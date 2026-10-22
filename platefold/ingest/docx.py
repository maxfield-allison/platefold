"""Convert a bounded DOCX package into Plate|Fold's semantic HTML vocabulary."""

from __future__ import annotations

import html
import re
import xml.dom
from bisect import bisect_right
from contextvars import ContextVar
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import mammoth
import mammoth.docx.xmlparser as mammoth_xmlparser
from defusedxml import minidom as defused_minidom
from markdown.extensions.toc import slugify

from ..accessibility import valid_language
from ..cancellation import CancelCheck, CancelledError, Throttle, checked, never_cancelled
from ..config import Settings
from ..htmlplan import cell_spans
from ..limits import ResourceLimitError, ResourceLimits
from ..ooxml import InvalidOOXMLError, OOXMLPackage, drawingml_theme
from ..placeholders import Placeholders
from ..table_grid import GRID_PAD, SHORT_ROW, Grid
from . import Ingested
from .docx_semantics import DECORATIVE_ALT, DocxParts, docx_parts, prepared
from .heading_ids import HeadingIds
from .markdown import FENCE_OPEN

_CORE_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_VOID = {"br", "hr", "img"}
_IMAGE_EXTENSIONS = {
    "image/bmp": "bmp",
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/svg+xml": "svg",
    "image/tiff": "tiff",
    "image/webp": "webp",
}
_ALLOWED = {
    "a",
    "blockquote",
    "br",
    "code",
    "del",
    "em",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "hr",
    "img",
    "li",
    "ol",
    "p",
    "pre",
    "s",
    "strong",
    "span",
    "sub",
    "sup",
    "table",
    "tbody",
    "td",
    "tfoot",
    "th",
    "thead",
    "tr",
    "ul",
}
_DROP_CONTENT = {"applet", "audio", "embed", "iframe", "object", "script", "style", "video"}
_ATTRS = {
    "a": {"href"},
    "img": {"alt", "src"},
    "li": {"id"},
    "ol": {"start"},
    "td": {"colspan", "rowspan"},
    "th": {"colspan", "rowspan", "scope"},
}
_SAFE_HREF = re.compile(r"^(?:#[A-Za-z0-9_.:-]+|https?://|mailto:)", re.I)
_SAFE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_SANITIZE_CHUNK = 64 * 1024
# The cancel check for the conversion running in this thread, and a throttled
# form of it for per-element work. Mammoth's parser seams are module-wide, so
# the checks travel in a context variable rather than in the seams themselves;
# concurrent conversions each see their own.
_mammoth_cancel: ContextVar[tuple[CancelCheck, CancelCheck]] = ContextVar(
    "platefold_mammoth_cancel", default=(never_cancelled, never_cancelled)
)
_MammothElement = mammoth_xmlparser.XmlElement


class _CheckedChildren(list):
    """An element's children, reaching the throttled check once per child."""

    __slots__ = ()

    def __iter__(self):
        tick = _mammoth_cancel.get()[1]
        for child in super().__iter__():
            tick()
            yield child


class _CheckedElement(_MammothElement):
    """Mammoth's XML element, reaching a cancel check as it is built and read.

    After parsing, Mammoth turns the DOM into its own element tree and then
    walks that tree, each taking longer than the parse on a large part. Both
    create elements or iterate their children, so wrapping `children` gives
    both a check per element. It remains a Mammoth element for every
    `isinstance` test.
    """

    def __setattr__(self, name, value):
        if name == "children" and type(value) is not _CheckedChildren:
            _mammoth_cancel.get()[1]()
            value = _CheckedChildren(value)
        super().__setattr__(name, value)


class _CheckedReader:
    """A file object that checks for cancellation before every read."""

    def __init__(self, handle, cancel_check: CancelCheck):
        self._handle = handle
        self._cancel_check = cancel_check

    def read(self, size: int = -1) -> bytes:
        self._cancel_check()
        return self._handle.read(size)


def _parse_mammoth_part(fileobj):
    # minidom reads the part 16 KiB at a time, so a large part is parsed
    # between cancel checks rather than in one uninterruptible call.
    return defused_minidom.parse(_CheckedReader(fileobj, _mammoth_cancel.get()[0]))


def _defuse_mammoth_xml() -> None:
    """Replace Mammoth's minidom parser without monkey-patching the stdlib.

    Mammoth imports ``xml`` as a module and resolves ``xml.dom.minidom.parse``
    at call time.  Giving that module a private facade preserves ``Node`` while
    routing every converter parse through defusedxml and the cancel check.
    The parser likewise resolves ``XmlElement`` at call time, so the elements
    it builds are the checked subclass.
    """
    mammoth_xmlparser.xml = SimpleNamespace(
        dom=SimpleNamespace(minidom=SimpleNamespace(parse=_parse_mammoth_part), Node=xml.dom.Node)
    )
    mammoth_xmlparser.XmlElement = _CheckedElement


def _attribute(tag: str, name: str, value: str | None, warnings: list[tuple[str, str]]):
    if name == "lang" and valid_language(value):
        return name, value
    if value is None or name not in _ATTRS.get(tag, set()):
        return None
    if name == "href" and not _SAFE_HREF.match(value.strip()):
        warnings.append(("DOCX_LINK_REMOVED", f"Removed unsafe DOCX link target: {value[:120]}"))
        return None
    if name == "src" and not value.startswith("__platefold_docx_asset_"):
        warnings.append(("DOCX_IMAGE_REMOVED", "Removed a DOCX image not supplied by the package"))
        return None
    if name == "id" and not _SAFE_ID.fullmatch(value):
        return None
    if name in {"colspan", "rowspan", "start"} and not re.fullmatch(r"[1-9][0-9]{0,3}", value):
        return None
    if name == "scope" and value not in {"row", "col", "rowgroup", "colgroup"}:
        return None
    return name, value


class _Grid(Grid):
    """The grid of one Word table; its warning names the narrowed cells."""

    def warning(self) -> tuple[str, str]:
        where = f' (under "{self.heading[:80]}")' if self.heading else ""
        return (
            "DOCX_TABLE_OVERLAP",
            f"{self.name}{where} has merged cells that overlap in the Word file's grid; "
            f"{self.narrowed} cell(s) were narrowed to the columns left free, keeping their "
            "text. Check the table's merged cells in Word.",
        )


class _Sanitizer(HTMLParser):
    """Whitelist Mammoth output while assigning headings and table widths."""

    def __init__(self, max_cells: int, stand_in: str | None = None) -> None:
        super().__init__(convert_charrefs=True)
        self.max_cells = max_cells  # table cells, with those that fill short rows
        self.stand_in = stand_in  # the text of a cell standing in for a Word row indent
        self.cells = 0
        self.parts: list[str] = []
        self.outline: list[tuple[int, str, str]] = []
        self.warnings: list[tuple[str, str]] = []
        self.anchors = HeadingIds()
        self.heading: dict | None = None
        self.tables: list[dict] = []
        self.table_count = 0
        self.drop_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self.drop_depth:
            self.drop_depth += 1
            return
        if tag in _DROP_CONTENT:
            self.drop_depth = 1
            self.warnings.append(("DOCX_HTML_REMOVED", f"Removed unsafe generated <{tag}> content"))
            return
        if tag not in _ALLOWED:
            self.warnings.append(("DOCX_HTML_REMOVED", f"Removed generated <{tag}> wrapper"))
            return
        cleaned = []
        for name, value in attrs:
            if item := _attribute(tag, name.lower(), value, self.warnings):
                cleaned.append(item)
        if self.tables:
            cleaned = self._place(tag, cleaned)
        rendered_attrs = "".join(
            f' {name}="{html.escape(value, quote=True)}"' for name, value in cleaned
        )
        if tag.startswith("h") and tag[1:].isdigit():
            self.heading = {"level": int(tag[1:]), "index": len(self.parts), "text": []}
            self.parts.append("")
        elif tag == "table":
            self._open_table()
        else:
            self.parts.append(f"<{tag}{rendered_attrs}>")
            if tag in {"td", "th"} and self.tables:
                self.tables[-1]["cell"] = 0

    def _open_table(self) -> None:
        # Tables are numbered as a reader counts them, at the top level; a
        # nested table is named after the table it sits in.
        if self.tables:
            self.tables[-1]["holds"] = True  # the open cell holds a table
        else:
            self.table_count += 1
        name = f"{'A table inside ' if self.tables else ''}Table {self.table_count}"
        grid = _Grid(name, self.outline[-1][1] if self.outline else None)
        self.tables.append(
            {"index": len(self.parts), "widths": [], "row": [], "cell": None, "grid": grid}
        )
        self.parts.append("")

    def _place(self, tag: str, cleaned: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """Place a row group, row or cell of the open table; a cell's attributes as kept."""
        grid: _Grid = self.tables[-1]["grid"]
        if tag in {"thead", "tbody", "tfoot"}:
            grid.group()
        elif tag == "tr":
            grid.row(len(self.parts))
        elif tag in {"td", "th"}:
            self.cells += 1
            spans = "".join(
                f' {name}="{value}"' for name, value in cleaned if name in ("colspan", "rowspan")
            )
            colspan, rowspan = cell_spans(spans)
            kept = grid.cell(colspan, rowspan)
            table = self.tables[-1]
            table.update(
                start=len(self.parts), kept=kept, narrowed=kept < colspan, text="", holds=False
            )
            if kept < colspan:
                cleaned = [(name, value) for name, value in cleaned if name != "colspan"]
                cleaned += [("colspan", str(kept))] if kept > 1 else []
        return cleaned

    def _pad(self, grid: _Grid) -> None:
        """Fill the table's short rows out to its grid, within the document's cell limit."""
        pads = grid.padding(self.max_cells - self.cells)
        if pads is None:
            raise ResourceLimitError(
                f"DOCX tables need more than {self.max_cells:,} cells with their short rows "
                "filled out to one grid; split the document or its tables"
            )
        self.cells += sum(len(spans) for *_, spans in pads)
        for start, end, spans in pads:
            cells = "".join(
                f'<td class="{GRID_PAD}"' + (f' colspan="{span}"' if span > 1 else "") + "></td>"
                for span in spans
            )
            self.parts[end] = cells + self.parts[end]
            if start >= 0:
                self.parts[start] = self.parts[start].replace("<tr", f'<tr class="{SHORT_ROW}"', 1)

    def _close_cell(self, tag: str) -> str:
        """Close the open table's cell; the end tag to write.

        A cell holding only the stand-in text marks columns a Word row leaves
        empty (see docx_semantics). It becomes an empty cell like those that
        fill short rows, a data cell even in a header row, and its text is
        neither kept nor counted in the table's width.
        """
        table = self.tables[-1]
        width, table["cell"] = table["cell"], None
        if self.stand_in and table["text"] == self.stand_in and not table["holds"]:
            span = table["kept"]
            colspan = f' colspan="{span}"' if span > 1 else ""
            self.parts[table["start"] :] = [f'<td class="{GRID_PAD}"{colspan}>']
            table["grid"].stand_in(table["narrowed"])
            width, tag = 0, "td"
        table["row"].append(width)
        return tag

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag: str) -> None:  # noqa: C901
        tag = tag.lower()
        if self.drop_depth:
            self.drop_depth -= 1
            return
        if tag not in _ALLOWED or tag in _VOID:
            return
        if tag.startswith("h") and self.heading is not None:
            name = "".join(self.heading["text"]).strip()
            anchor = self.anchors(slugify(name, "-"))
            level = self.heading["level"]
            self.parts[self.heading["index"]] = f'<h{level} id="{html.escape(anchor, quote=True)}">'
            self.outline.append((level, name, anchor))
            self.heading = None
        if tag == "td" or tag == "th":
            if self.tables and self.tables[-1]["cell"] is not None:
                tag = self._close_cell(tag)
        elif tag == "tr":
            if self.tables:
                table = self.tables[-1]
                table["grid"].end_row(len(self.parts))
                for index, width in enumerate(table["row"]):
                    if index == len(table["widths"]):
                        table["widths"].append(width)
                    else:
                        table["widths"][index] = max(table["widths"][index], width)
                table["row"] = []
        elif tag == "table" and self.tables:
            table = self.tables.pop()
            if table["grid"].narrowed:
                self.warnings.append(table["grid"].warning())
            self._pad(table["grid"])
            natural = sum(min(28, max(8, width)) * 7 + 16 for width in table["widths"])
            self.parts[table["index"]] = (
                '<section class="pf-data-section"><table class="pf-data-table" '
                f'data-natural-width="{max(1, natural)}">'
            )
            self.parts.append("</table></section>")
            return
        self.parts.append(f"</{tag}>")

    def handle_data(self, data: str) -> None:
        if self.drop_depth:
            return
        self.parts.append(html.escape(data))
        if self.heading is not None:
            self.heading["text"].append(data)
        for table in self.tables:
            if table["cell"] is not None:
                table["cell"] += max(
                    (len(line.expandtabs(4)) for line in data.splitlines()), default=0
                )
        # Only the innermost cell's own text can make it a stand-in.
        if self.stand_in and self.tables and self.tables[-1]["cell"] is not None:
            table = self.tables[-1]
            table["text"] = (table["text"] + data)[: len(self.stand_in) + 1]

    def handle_entityref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.handle_data(html.unescape(f"&#{name};"))

    def unknown_decl(self, _data: str) -> None:
        self.warnings.append(("DOCX_HTML_REMOVED", "Removed a generated HTML declaration"))

    def result(self) -> tuple[str, list[tuple[int, str, str]], list[tuple[str, str]]]:
        self.close()
        if self.drop_depth or self.heading is not None or self.tables:
            raise InvalidOOXMLError("DOCX conversion produced malformed HTML structure")
        body = "".join(self.parts)
        if self.stand_in:
            # A stand-in Mammoth wrote anywhere but as a table cell of its own
            # leaves only its key, which no authored text can contain.
            body = body.replace(self.stand_in, "")
        return body, self.outline, self.warnings


def _sanitize(
    fragment: str,
    cancel_check: CancelCheck = never_cancelled,
    max_cells: int = ResourceLimits().max_document_cells,
    stand_in: str | None = None,
):
    parser = _Sanitizer(max_cells, stand_in)
    try:
        for offset in range(0, len(fragment), _SANITIZE_CHUNK):
            cancel_check()
            parser.feed(fragment[offset : offset + _SANITIZE_CHUNK])
        return parser.result()
    except (RecursionError, AssertionError) as exc:
        raise InvalidOOXMLError("DOCX conversion produced invalid HTML") from exc


def _extract_mermaid(
    body: str, outline: list[tuple[int, str, str]], placeholders: Placeholders
) -> tuple[str, list[dict]]:
    figures: list[dict] = []
    pattern = re.compile(r"<pre>(?:<code>)?(.*?)(?:</code>)?</pre>", re.S)
    heading_positions = [match.start() for match in re.finditer(r"<h[1-6]\b", body)]

    def replace(match: re.Match) -> str:
        text = html.unescape(re.sub(r"<br>", "\n", match.group(1))).replace("\r\n", "\n")
        lines = text.split("\n")
        opener = FENCE_OPEN.match(lines[0]) if lines else None
        if not opener or opener.group(3).lower() != "mermaid":
            return match.group(0)
        ticks = opener.group(2)
        closer = re.compile(r"^[ \t]*`{%d,}[ \t]*$" % len(ticks))
        try:
            end = next(index for index, line in enumerate(lines[1:], 1) if closer.match(line))
        except StopIteration:
            return match.group(0)
        if any(line.strip() for line in lines[end + 1 :]):
            return match.group(0)
        heading_index = bisect_right(heading_positions, match.start()) - 1
        figures.append(
            {
                "code": "\n".join(lines[1:end]).rstrip() + "\n",
                "head": outline[heading_index][1] if heading_index >= 0 else "",
            }
        )
        return placeholders.figure(len(figures) - 1)

    return pattern.sub(replace, body), figures


def _style_map(package: OOXMLPackage, styles_part: str) -> str:  # noqa: C901
    root = package.xml(styles_part)
    if root is None:
        return "p[style-name='Code Block'] => pre:separator('\\n')"
    styles: dict[str, dict] = {}
    for element in checked(root.findall(f"{{{_WORD_NS}}}style"), package.cancel_check):
        if element.get(f"{{{_WORD_NS}}}type") != "paragraph":
            continue
        style_id = element.get(f"{{{_WORD_NS}}}styleId")
        name_node = element.find(f"{{{_WORD_NS}}}name")
        if not style_id or name_node is None:
            continue
        outline = element.find(f".//{{{_WORD_NS}}}outlineLvl")
        based = element.find(f"{{{_WORD_NS}}}basedOn")
        styles[style_id] = {
            "name": name_node.get(f"{{{_WORD_NS}}}val", ""),
            "level": outline.get(f"{{{_WORD_NS}}}val") if outline is not None else None,
            "based": based.get(f"{{{_WORD_NS}}}val") if based is not None else None,
        }

    def level(style_id: str, seen: set[str] | None = None) -> int | None:
        if style_id not in styles or style_id in (seen or set()):
            return None
        style = styles[style_id]
        try:
            direct = int(style["level"]) + 1 if style["level"] is not None else None
        except ValueError:
            direct = None
        if direct and 1 <= direct <= 6:
            return direct
        return level(style["based"], (seen or set()) | {style_id}) if style["based"] else None

    mappings = ["p[style-name='Code Block'] => pre:separator('\\n')"]
    for style_id, style in styles.items():
        name = style["name"]
        if not name or any(character in name for character in "'\r\n"):
            continue
        if resolved := level(style_id):
            mappings.append(f"p[style-name='{name}'] => h{resolved}:fresh")
        elif name.casefold() in {"code block", "preformatted", "preformatted text"}:
            mappings.append(f"p[style-name='{name}'] => pre:separator('\\n')")
    return "\n".join(dict.fromkeys(mappings))


def _core_properties(package: OOXMLPackage):
    root = package.xml("docProps/core.xml")
    if root is None:
        return None, []

    def text(namespace: str, name: str) -> str:
        node = root.find(f"{{{namespace}}}{name}")
        return (node.text or "").strip() if node is not None else ""

    title = text(_DC_NS, "title") or None
    metadata = []
    for label, value in (
        ("Author", text(_DC_NS, "creator")),
        ("Version", text(_CORE_NS, "version") or text(_CORE_NS, "revision")),
        ("Document", text(_DC_NS, "subject")),
    ):
        if value:
            metadata.append((label, value))
    return title, metadata


def _image_converter(
    assets: dict[str, bytes], settings: Settings, cancel_check: CancelCheck = never_cancelled
):
    # Mammoth reopens the image part for every reference. Keying by part keeps
    # one copy of an image placed many times, and the running total holds the
    # distinct images to the same ceiling the document applies after ingest.
    keys: dict[str, str] = {}
    held = 0

    def convert(image):
        nonlocal held
        cancel_check()
        content_type = image.content_type.lower()
        try:
            extension = _IMAGE_EXTENSIONS[content_type]
        except KeyError:
            raise InvalidOOXMLError(
                f"Unsupported DOCX embedded image type: {content_type or 'unknown'}"
            ) from None
        with image.open() as source:
            part = getattr(source, "name", None)
            key = keys.get(part) if isinstance(part, str) else None
            if key is None:
                raw = source.read(settings.limits.max_image_bytes + 1)
                if len(raw) > settings.limits.max_image_bytes:
                    raise ResourceLimitError(
                        f"DOCX image exceeds the {settings.limits.max_image_bytes}-byte image limit"
                    )
                held += len(raw)
                if held > settings.limits.max_embedded_bytes:
                    raise ResourceLimitError(
                        "DOCX images exceed the "
                        f"{settings.limits.max_embedded_bytes}-byte embedded asset limit"
                    )
                key = f"__platefold_docx_asset_{len(assets)}.{extension}"
                assets[key] = raw
                if isinstance(part, str):
                    keys[part] = key
        attrs = {"src": key}
        if image.alt_text == DECORATIVE_ALT:
            attrs["alt"] = ""
        return attrs

    return mammoth.images.img_element(convert)


def source_theme_for_path(path: str | Path, settings: Settings):
    """Read only the reusable DrawingML theme from one validated DOCX."""
    source = Path(path)
    with OOXMLPackage(
        source, settings.limits, max_xml_nodes=settings.limits.max_docx_xml_nodes
    ) as package:
        _main_document(package)
        return drawingml_theme(package, "word/theme/theme1.xml", label=source.name)


def _main_document(package: OOXMLPackage) -> DocxParts:
    """The package's DOCX parts, refusing a package without its main document."""
    parts = docx_parts(package)
    if not package.has(parts.main):
        raise InvalidOOXMLError("DOCX package has no main document part")
    return parts


def _checkpoint(cancel_check: CancelCheck):
    """A Mammoth document transform that only checks for cancellation."""

    def transform(document):
        cancel_check()
        return document

    return transform


def ingest(path: str, settings: Settings, cancel_check: CancelCheck | None = None) -> Ingested:
    source = Path(path)
    cancel_check = cancel_check or never_cancelled
    assets: dict[str, bytes] = {}
    context = _mammoth_cancel.set((cancel_check, Throttle(cancel_check, 4096)))
    try:
        with OOXMLPackage(
            source,
            settings.limits,
            max_xml_nodes=settings.limits.max_docx_xml_nodes,
            cancel_check=cancel_check,
        ) as package:
            parts = _main_document(package)
            theme, theme_warnings = drawingml_theme(
                package, "word/theme/theme1.xml", label=source.name
            )
            title, metadata = _core_properties(package)
            style_map = _style_map(package, parts.styles)
            _defuse_mammoth_xml()
            with prepared(package, parts) as (handle, language, language_map, stand_in):
                cancel_check()
                converted = mammoth.convert_to_html(
                    handle,
                    style_map=style_map + "\n" + language_map,
                    include_embedded_style_map=False,
                    external_file_access=False,
                    convert_image=_image_converter(assets, settings, cancel_check),
                    transform_document=_checkpoint(cancel_check),
                )
        cancel_check()
        body, outline, html_warnings = _sanitize(
            converted.value, cancel_check, settings.limits.max_document_cells, stand_in
        )
        cancel_check()
        placeholders = Placeholders.unused_in([body])
        body, figures = _extract_mermaid(body, outline, placeholders)
    except (ResourceLimitError, InvalidOOXMLError, CancelledError):
        raise
    except Exception as exc:
        raise InvalidOOXMLError(f"Invalid DOCX structure: {source.name}") from exc
    finally:
        _mammoth_cancel.reset(context)

    messages = [
        ("DOCX_CONVERSION", message.message)
        for message in converted.messages
        if getattr(message, "message", "")
    ]
    return Ingested(
        body_html=body,
        title=title,
        language=language,
        metadata=metadata,
        outline=outline,
        figures=figures,
        placeholders=placeholders,
        assets=assets,
        source_theme=theme,
        warnings=[*theme_warnings, *messages, *html_warnings],
    )
