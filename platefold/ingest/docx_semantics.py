"""Carry OOXML language, decorative intent and row indents through Mammoth.

Mammoth does not expose w:lang. A private, bounded copy adds character style
mappings for effective run languages; the authored package is never changed.

Mammoth also ignores the grid columns a table row leaves empty before its
first cell and after its last (w:gridBefore, w:gridAfter), so an indented row
converts as if it started in the first column. The same copy gives each such
row a stand-in cell spanning those columns, holding only a key the document
cannot contain; the DOCX reader turns every stand-in back into an empty cell.

The copy changes the parts Mammoth converts, which are not always named
word/document.xml and so on: a package names its main document through its
relationships, and the main document names its styles, notes and comments
the same way. ``docx_parts`` finds them as Mammoth does.
"""

from __future__ import annotations

import io
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from xml.etree import ElementTree as ET

from ..accessibility import valid_language
from ..cancellation import checked, never_cancelled
from ..limits import ResourceLimitError
from ..placeholders import new_key

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DC = "{http://purl.org/dc/elements/1.1/}"
WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
DECORATIVE = "{http://schemas.microsoft.com/office/drawing/2017/decorative}decorative"
DECORATIVE_ALT = "__platefold_explicitly_decorative__"
RELATIONSHIP = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
# Mammoth recognises only the transitional relationship types, so these do too.
OFFICE_RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


@dataclass(frozen=True)
class DocxParts:
    """The names of the parts Mammoth reads from one package."""

    main: str
    styles: str
    footnotes: str
    endnotes: str
    comments: str

    @property
    def text(self) -> tuple[str, ...]:
        """The parts whose text Mammoth converts."""
        return (self.main, self.footnotes, self.endnotes, self.comments)


def docx_parts(package) -> DocxParts:
    """Find the main document, styles, notes and comments as Mammoth 1.12 does.

    The package relationships name the main document, falling back to
    word/document.xml; the main document's relationships name the rest,
    falling back to word/<kind>.xml. A target that is not in the package is
    skipped. The main document may still be absent; callers check.
    """
    main = _related_part(package, "_rels/.rels", "officeDocument", "", "word/document.xml")
    folder, _, name = main.rpartition("/")
    relationships = f"{folder}/_rels/{name}.rels" if folder else f"_rels/{name}.rels"

    def find(kind: str) -> str:
        return _related_part(package, relationships, kind, folder, f"word/{kind}.xml")

    return DocxParts(main, find("styles"), find("footnotes"), find("endnotes"), find("comments"))


def _related_part(package, relationships: str, kind: str, folder: str, fallback: str) -> str:
    root = package.xml(relationships)
    for relationship in root.iter(RELATIONSHIP) if root is not None else ():
        if relationship.get("Type") != OFFICE_RELATIONSHIPS + kind:
            continue
        target = relationship.get("Target", "")
        joined = target if target.startswith("/") or not folder else f"{folder}/{target}"
        if package.has(joined.lstrip("/")):
            return joined.lstrip("/")
    return fallback


def _language(properties):
    node = properties.find(W + "lang") if properties is not None else None
    value = node.get(W + "val") if node is not None else None
    return value if valid_language(value) else None


class Languages:
    def __init__(self, package, styles_part: str):
        self.styles = package.xml(styles_part)
        if self.styles is None:
            self.styles = ET.Element(W + "styles")
        self.by_id = {s.get(W + "styleId"): s for s in self.styles.findall(W + "style")}
        self.default = _language(
            self.styles.find(W + "docDefaults/" + W + "rPrDefault/" + W + "rPr")
        )
        for style_id, style in self.by_id.items():
            if style.get(W + "type") == "paragraph" and style.get(W + "default") in {"1", "true"}:
                self.default = self.style_language(style_id) or self.default
        core = package.xml("docProps/core.xml")
        core_lang = core.find(DC + "language") if core is not None else None
        self.document = self.default or (core_lang.text if core_lang is not None else None)
        self.mappings: dict[str, str] = {}

    def style_language(self, style_id, seen=None):
        if style_id not in self.by_id or style_id in (seen or set()):
            return None
        style = self.by_id[style_id]
        direct = _language(style.find(W + "rPr"))
        parent = style.find(W + "basedOn")
        if direct or parent is None:
            return direct
        return self.style_language(parent.get(W + "val"), (seen or set()) | {style_id})

    def _run(self, run, inherited):
        properties = run.find(W + "rPr")
        if properties is None:
            properties = ET.SubElement(run, W + "rPr")
        style = properties.find(W + "rStyle")
        original = style.get(W + "val") if style is not None else None
        language = _language(properties) or self.style_language(original) or inherited
        if not language or language == self.document:
            return
        original_style = self.by_id.get(original)
        original_name = original_style.find(W + "name") if original_style is not None else None
        if (
            original_name is not None
            and original_name.get(W + "val") == "Strong"
            and properties.find(W + "b") is None
        ):
            ET.SubElement(properties, W + "b")
        if language not in self.mappings:
            ident = "PlateFoldLanguage" + str(len(self.mappings))
            while ident in self.by_id:
                ident += "x"
            self.mappings[language] = ident
            added = ET.SubElement(
                self.styles, W + "style", {W + "type": "character", W + "styleId": ident}
            )
            ET.SubElement(added, W + "name", {W + "val": ident})
        if style is None:
            style = ET.SubElement(properties, W + "rStyle")
        style.set(W + "val", self.mappings[language])

    def annotate(self, root, cancel_check=never_cancelled):
        for paragraph in checked(root.iter(W + "p"), cancel_check):
            properties = paragraph.find(W + "pPr")
            style = properties.find(W + "pStyle") if properties is not None else None
            direct = _language(properties.find(W + "rPr")) if properties is not None else None
            inherited = (
                direct
                or self.style_language(style.get(W + "val") if style is not None else None)
                or self.default
            )
            for run in paragraph.iter(W + "r"):
                self._run(run, inherited)
        for drawing in checked(root.iter(WP + "docPr"), cancel_check):
            flag = drawing.find(".//" + DECORATIVE)
            if flag is not None and flag.get("val") in {"1", "true"}:
                drawing.set("descr", DECORATIVE_ALT)

    def style_map(self):
        return "\n".join(
            f"r[style-name='{ident}'] => span[lang='{lang}']"
            for lang, ident in self.mappings.items()
        )


def _grid_columns(properties, name: str) -> int:
    """A row's w:gridBefore or w:gridAfter; 0 for none, or for a value that is not a count."""
    node = properties.find(W + name) if properties is not None else None
    value = (node.get(W + "val") or "") if node is not None else ""
    # isdigit() also accepts digits such as "²" that int() refuses; four digits
    # is as many as a cell's colspan may have (see ingest.docx).
    return int(value) if value.isascii() and value.isdigit() and len(value) <= 4 else 0


def _stand_in(key: str, columns: int):
    cell = ET.Element(W + "tc")
    ET.SubElement(ET.SubElement(cell, W + "tcPr"), W + "gridSpan", {W + "val": str(columns)})
    run = ET.SubElement(ET.SubElement(cell, W + "p"), W + "r")
    ET.SubElement(run, W + "t").text = key
    return cell


def mark_row_indents(root, key: str, cancel_check=never_cancelled) -> int:
    """Give each row's empty leading and trailing grid columns a stand-in cell; how many.

    A row's w:gridBefore columns come before its first cell and its
    w:gridAfter columns after its last. Their widths (w:wBefore, w:wAfter)
    are not carried, as no other Word column width is: Plate|Fold sizes the
    columns itself. Placed as cells, the columns also count in Mammoth's own
    placement, so a vertical merge below an indented row continues in the
    column it is in. Every row is its own element, so nested tables, header
    rows and rows a merge continues through are marked where they stand. Only
    rows in a table are marked: Mammoth writes a stray row outside one, where
    no stand-in would be taken for a cell.
    """
    added = 0
    for row in checked(_table_rows(root), cancel_check):
        properties = row.find(W + "trPr")
        before = _grid_columns(properties, "gridBefore")
        after = _grid_columns(properties, "gridAfter")
        if before:
            # After the row's own properties, before its first cell.
            at = sum(1 for child in row if child.tag in (W + "tblPrEx", W + "trPr"))
            row.insert(at, _stand_in(key, before))
        if after:
            row.append(_stand_in(key, after))
        added += bool(before) + bool(after)
    return added


def _table_rows(root):
    """Every w:tr that is a row of a w:tbl, directly or inside content controls."""
    for table in root.iter(W + "tbl"):
        pending = list(table)
        while pending:
            child = pending.pop(0)
            if child.tag == W + "tr":
                yield child
            elif child.tag in (W + "sdt", W + "sdtContent", W + "customXml"):
                pending[:0] = list(child)


class _CheckedBuffer(io.BytesIO):
    # The serializer writes through a text buffer, so each call carries a
    # block of output and can afford a full check.
    def __init__(self, cancel_check):
        super().__init__()
        self._cancel_check = cancel_check

    def write(self, data) -> int:
        self._cancel_check()
        return super().write(data)


def _serialize(root, cancel_check) -> bytes:
    """`ET.tostring`, checking for cancellation as the serializer writes."""
    buffer = _CheckedBuffer(cancel_check)
    ET.ElementTree(root).write(buffer, encoding="utf-8", xml_declaration=True)
    return buffer.getvalue()


@contextmanager
def prepared(package, docx: DocxParts):
    cancel_check = package.cancel_check
    languages = Languages(package, docx.styles)
    replacements = {}
    parts = [part for part in dict.fromkeys(docx.text) if package.has(part)]
    # A key none of the text parts contains marks the stand-ins for row indents.
    key = new_key(package.read(part).decode("utf-8", "replace") for part in parts)
    for part in parts:
        root = package.xml(part)
        if root is not None:
            languages.annotate(root, cancel_check)
            mark_row_indents(root, key, cancel_check)
            replacements[part] = _serialize(root, cancel_check)
    if languages.mappings:
        replacements[docx.styles] = _serialize(languages.styles, cancel_check)
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as result:
        total = 0
        with zipfile.ZipFile(result, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in dict.fromkeys([*package.names(), *replacements]):
                cancel_check()
                raw = replacements.get(name)
                if raw is None:
                    raw = package.read(name)
                total += len(raw)
                if total > package.limits.max_archive_expanded_bytes:
                    raise ResourceLimitError(
                        "DOCX semantic preparation exceeds the expanded archive limit"
                    )
                archive.writestr(name, raw)
        result.seek(0)
        yield result, languages.document, languages.style_map(), key
