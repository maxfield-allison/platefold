"""Join PDFs printed in several Chromium passes into one tagged document.

A very large table is printed in parts, each its own browser pass, so the
browser's layout memory is bounded by one part rather than the whole table
(see ``chunked``). This module joins those parts into what one pass would
have printed: the same pages, one structure tree, continuous structure-parent
keys, and merged named destinations and element IDs.

General PDF libraries hold every object of every input in memory, and a
tagged table has several objects per cell, so they cost gigabytes at the
sizes that need splitting. This joiner streams. It copies each part's objects
in order, renumbering the references in their dictionaries and never touching
stream data or the text of strings (see ``pdfobjects.Syntax``). Only the structure elements a later part extends are held until
the end: the chain from the document element to an open table, and that
table's header rows.

Where a part continues a table, the structure trees are merged the way
Chromium writes one pass. Chromium tags a repeated header row as the same TH
elements, with one marked-content reference per page, so the continuation's
header cells are mapped onto the first part's and their references appended.
Its rows join the first part's Table, and whatever follows the table joins
the matching ancestors.
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pdfobjects import (
    Name,
    PdfFile,
    PdfShapeError,
    Raw,
    Ref,
    Writer,
    kids,
    parse,
    remap_references,
    scan,
    serialize,
    splice,
    string_bytes,
)


class JoinError(PdfShapeError):
    """A part could not be joined without losing or misplacing content."""


@dataclass
class Part:
    """One printed pass, as the joiner should take it.

    ``keep`` leading pages are kept; the pages after them were printed again
    by the next part. ``continues`` means this part restarts inside the last
    table of the previous part, which has ``header_rows`` header rows.
    ``stand_ins`` are link targets this part printed only as empty
    placeholders, so their named destinations yield to the real ones.
    """

    path: str
    keep: int | None = None
    first: int = 0  # leading pages the joiner leaves out, before a forced break
    continues: bool = False
    header_rows: int = 0
    stand_ins: frozenset[str] = frozenset()


# ------------------------------------------------------------- structure


_OPEN: dict[str, tuple[tuple[int, int], PdfFile]] = {}


def forget_parts() -> None:
    """Drop the kept readers once a join is done."""
    _OPEN.clear()


def _open(path: str | Path) -> PdfFile:
    """A part's reader, reused while the file is unchanged.

    Finding a restart point, the first kept page, the page sizes and the join
    all read the same parts; one index and one parse of each element serve
    them all. Only the most recent few parts are kept.
    """
    key = str(path)
    stat = Path(path).stat()
    stamp = (stat.st_size, stat.st_mtime_ns)
    cached = _OPEN.get(key)
    if cached is not None and cached[0] == stamp:
        return cached[1]
    pdf = PdfFile(path)
    _OPEN[key] = (stamp, pdf)
    while len(_OPEN) > 3:
        del _OPEN[next(iter(_OPEN))]
    return pdf


def _is_element(pdf: PdfFile, item: Any) -> bool:
    if not isinstance(item, Ref):
        return False
    node = pdf.get(item)
    return isinstance(node, dict) and b"/S" in node


def _role(node: Any) -> bytes:
    return bytes(node.get(b"/S", b"")) if isinstance(node, dict) else b""


def _element_kids(pdf: PdfFile, node: dict) -> list[Ref]:
    return [kid for kid in kids(node.get(b"/K")) if _is_element(pdf, kid)]


def table_rows(pdf: PdfFile, table: Ref) -> list[Ref]:
    """A table's rows in order, looking through THead, TBody and TFoot groups."""
    rows: list[Ref] = []
    for kid in _element_kids(pdf, pdf.get(table)):
        role = _role(pdf.get(kid))
        if role == b"/TR":
            rows.append(kid)
        elif role in (b"/THead", b"/TBody", b"/TFoot"):
            rows.extend(_element_kids(pdf, pdf.get(kid)))
    return rows


def _document(pdf: PdfFile) -> Ref:
    tree = pdf.get(pdf.root[b"/StructTreeRoot"])
    top = kids(tree.get(b"/K"))
    if len(top) != 1 or not isinstance(top[0], Ref):
        raise JoinError(f"{pdf.path}: structure tree has no single document element")
    return top[0]


def _last_path(pdf: PdfFile, start: Ref) -> list[Ref]:
    """Elements from ``start`` along each last element child down to a Table."""
    path = [start]
    while _role(pdf.get(path[-1])) != b"/Table":
        children = _element_kids(pdf, pdf.get(path[-1]))
        if not children or len(path) > 256:
            raise JoinError(f"{pdf.path}: the part does not end in a table")
        path.append(children[-1])
    return path


def _first_path(pdf: PdfFile, start: Ref) -> list[Ref]:
    """Elements from ``start`` along each first element child down to a Table."""
    path = [start]
    while _role(pdf.get(path[-1])) != b"/Table":
        children = _element_kids(pdf, pdf.get(path[-1]))
        if not children or len(path) > 256:
            raise JoinError(f"{pdf.path}: the continuation does not open a table")
        path.append(children[0])
    return path


def _element_pages(pdf: PdfFile, ref: Ref, page_index: dict[int, int]) -> set[int]:
    """Every page index on which an element or its descendants have content."""
    pages: set[int] = set()
    stack: list[tuple[Any, int | None]] = [(ref, None)]
    while stack:
        item, inherited = stack.pop()
        if isinstance(item, int):
            if inherited is not None:
                pages.add(inherited)
            continue
        node = pdf.get(item)
        if not isinstance(node, dict):
            continue
        page = node.get(b"/Pg")
        own = page_index.get(page.num) if isinstance(page, Ref) else inherited
        if node.get(b"/Type") in (Name(b"/MCR"), Name(b"/OBJR")):
            if own is not None:
                pages.add(own)
            continue
        stack.extend((kid, own) for kid in kids(node.get(b"/K")))
    return pages


def _subtree(pdf: PdfFile, ref: Ref) -> set[int]:
    found = {ref.num}
    for kid in _element_kids(pdf, pdf.get(ref)):
        found |= _subtree(pdf, kid)
    return found


def restart_point(
    path: str | Path, header_rows: int, allowed: list[bool]
) -> tuple[int, int] | None:
    """Where the next part should start again after this part's last table.

    Returns ``(row, keep)``: the index among this part's body rows of the last
    row that begins a page cleanly, and how many leading pages to keep. A
    clean start means the row is the first thing on its page after the
    repeated header: the row before it ended on an earlier page, and no cell
    spans into it from above (``allowed``). None when no page after the
    table's first one begins cleanly.
    """
    pdf = _open(path)
    page_index = {num: index for index, num in enumerate(pdf.pages())}
    rows = table_rows(pdf, _last_path(pdf, _document(pdf))[-1])[header_rows:]
    if len(rows) != len(allowed):
        raise JoinError(f"{pdf.path}: printed {len(rows)} rows, expected {len(allowed)}")
    spans: dict[int, tuple[int, int] | None] = {}

    def span(index: int) -> tuple[int, int] | None:
        if index not in spans:
            found = _element_pages(pdf, rows[index], page_index)
            spans[index] = (min(found), max(found)) if found else None
        return spans[index]

    for index in range(len(rows) - 1, 0, -1):
        here, before = span(index), span(index - 1)
        if here is None or before is None or here[0] == 0:
            return None  # a row without content cannot be placed on a page
        if allowed[index] and before[1] < here[0]:
            return index, here[0]
    return None


def continuation_page(path: str | Path, header_rows: int, repeated: int = 1) -> int:
    """The page on which a continuation part's first kept row begins.

    A continuation part prints rows before its first one, ``repeated`` of
    them tagged, then forces a page break; everything before that break is
    left out of the join.
    """
    pdf = _open(path)
    page_index = {num: index for index, num in enumerate(pdf.pages())}
    table = _first_path(pdf, _document(pdf))[-1]
    rows = table_rows(pdf, table)[header_rows:]
    first = repeated
    if len(rows) <= first:
        raise JoinError(f"{pdf.path}: the continuation printed no rows")
    pages = _element_pages(pdf, rows[first], page_index)
    if not pages or min(pages) == 0:
        raise JoinError(f"{pdf.path}: the continuation did not break before its first row")
    return min(pages)


def page_sizes(parts: list[Part]) -> list[tuple[float, float]]:
    """Width and height in points of every page the joined document will have."""
    sizes: list[tuple[float, float]] = []
    for part in parts:
        pdf = _open(part.path)
        pages = pdf.pages()
        keep = len(pages) if part.keep is None else part.keep
        sizes.extend(pdf.media_box(page) for page in pages[part.first : keep])
    return sizes


# ------------------------------------------------------------- rewriting


# The fields holding structure element IDs: an element's own, and the header
# cells a table cell's attributes name.
_ID_FIELDS = frozenset({b"/ID", b"/Headers"})
_STRING_SPECIAL = re.compile(rb"[()\\]")
# A /StructParent key: a whole integer token, not a reference's object number.
_STRUCT_PARENT = re.compile(
    rb"/StructParent[ \t\r\n\f\x00]+(\d+)(?![^ \t\r\n\f\x00()<>\[\]{}/%])"
    rb"(?![ \t\r\n\f\x00]+\d+[ \t\r\n\f\x00]+R(?![^ \t\r\n\f\x00()<>\[\]{}/%]))"
)


def _string_token(value: bytes) -> Raw:
    if _STRING_SPECIAL.search(value) is not None:
        value = _STRING_SPECIAL.sub(rb"\\\g<0>", value)
    return Raw(b"(" + value + b")")


def _remap(value: Any, mapping: Callable[[int], int]) -> Any:
    if isinstance(value, Ref):
        return Ref(mapping(value.num))
    if isinstance(value, dict):
        return {key: _remap(item, mapping) for key, item in value.items()}
    if isinstance(value, list):
        return [_remap(item, mapping) for item in value]
    return value


def _remap_bytes(
    head: bytes,
    mapping: Callable[[int], int],
    ids: dict[bytes, bytes] | None = None,
    keys: dict[int, int] | None = None,
) -> bytes:
    """An object's bytes with its references renumbered.

    Only reference tokens are renumbered; a string, name or comment that
    reads like a reference is left alone (see ``pdfobjects.Syntax``). With
    ``ids``, element IDs are renamed where they are IDs, the strings of /ID
    and /Headers, and not where other text happens to equal one. With
    ``keys``, /StructParent keys are renumbered. Everything else, the
    spacing included, is copied unchanged.
    """
    renames = bool(ids) and (b"/ID" in head or b"/Headers" in head)
    parents = keys is not None and b"/StructParent" in head
    if not renames and not parents:
        return remap_references(head, mapping)
    syntax = scan(head)
    edits = syntax.references(mapping)
    if renames and ids:
        for start, end in syntax.strings:
            renamed = ids.get(string_bytes(head[start:end]))
            if renamed is not None and syntax.field(start) in _ID_FIELDS:
                edits.append((start, end, bytes(_string_token(renamed))))
    if parents and keys is not None:
        for match in _STRUCT_PARENT.finditer(syntax.view):
            start, end = match.span(1)
            if syntax.field(start, arrays=False) != b"/StructParent":
                continue
            key = int(match.group(1))
            if key not in keys:
                raise JoinError(f"an object names structure-parent key {key}, which was left out")
            edits.append((start, end, b"%d" % keys[key]))
    return splice(head, edits)


def _name_text(name: bytes) -> str:
    raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), bytes(name)[1:])
    return raw.decode("utf-8", errors="replace")


# ---------------------------------------------------------------- joining


@dataclass
class _PartState:
    """What joining one part needs to know before copying its objects."""

    pdf: PdfFile
    part: Part
    pages: list[int]
    first: int
    keep: int
    base: int
    alias: dict[int, int] = field(default_factory=dict)
    skip: set[int] = field(default_factory=set)
    hold: set[int] = field(default_factory=set)
    ids: dict[bytes, bytes] = field(default_factory=dict)
    keys: dict[int, int] = field(default_factory=dict)  # structure-parent key -> joined key

    @property
    def kept(self) -> list[int]:
        return self.pages[self.first : self.keep]

    @property
    def dropped(self) -> set[int]:
        return set(self.pages[: self.first]) | set(self.pages[self.keep :])

    def map(self, num: int) -> int:
        return self.alias.get(num, self.base + num)


class _Joiner:
    def __init__(self, writer: Writer, overlay: _Overlay | None, cancel_check: Callable[[], None]):
        self.writer = writer
        self.overlay = overlay
        self.cancel_check = cancel_check
        self.pages_num = writer.allocate()
        self.pages: list[int] = []
        self.parent_tree: list[tuple[int, Any]] = []
        self.key_base = 0
        self.dests: dict[bytes, Any] = {}
        self.weak_dests: set[bytes] = set()
        self.ids: dict[bytes, Ref] = {}
        self.held: dict[int, dict] = {}  # final number -> element later parts extend
        self.chain: list[int] = []  # final numbers, document element to an open table
        self.header: list[int] = []  # final numbers of that table's header rows
        self.catalog: dict | None = None
        self.info_head: bytes | None = None

    # -- one part ----------------------------------------------------------
    def add(self, index: int, part: Part, cut_header_rows: int | None) -> None:
        """Copy one part; ``cut_header_rows`` is set when the next part continues it."""
        self.cancel_check()
        pdf = _open(part.path)
        pages = pdf.pages()
        keep = len(pages) if part.keep is None else part.keep
        if not 0 <= part.first < keep <= len(pages):
            raise JoinError(f"{part.path}: cannot keep pages {part.first}-{keep} of {len(pages)}")
        base = self.writer.reserve(len(pdf.ends))
        state = _PartState(pdf, part, pages, part.first, keep, base)
        own = self._document_objects(state, first=index == 0)
        self._drop_pages(state)
        document = _document(pdf)
        if index == 0:
            state.hold.add(document.num)
            self.chain = [state.map(document.num)]
        else:
            self._merge(state, document)
        self._hold_open_table(state, document, cut_header_rows)
        prefix = b"p%d-" % index
        tree = pdf.get(pdf.root[b"/StructTreeRoot"])
        for key, _value in pdf.name_tree(tree[b"/IDTree"]) if b"/IDTree" in tree else ():
            if index:
                state.ids.setdefault(key, prefix + key)
        self._parent_tree(state, tree)
        self._destinations(state)
        self._element_ids(state, tree)
        self._copy_objects(state, own)
        self.pages.extend(state.map(page) for page in state.kept)

    def _document_objects(self, state: _PartState, *, first: bool) -> set[int]:
        """This part's document-level objects, which the joiner writes once itself."""
        pdf = state.pdf
        root = pdf.root
        pdf.pages()
        own = {pdf.root_ref.num, root[b"/StructTreeRoot"].num, *pdf.page_tree}
        tree = pdf.get(root[b"/StructTreeRoot"])
        for key in (b"/ParentTree", b"/IDTree"):
            if isinstance(tree.get(key), Ref):
                own |= self._tree_nodes(pdf, tree[key])
        if isinstance(root.get(b"/Dests"), Ref):
            own.add(root[b"/Dests"].num)
        info = pdf.trailer.get(b"/Info")
        if isinstance(info, Ref):
            own.add(info.num)
            if first:
                self.info_head = pdf.raw(info.num).head
        if first:
            self.catalog = root
        return own

    @staticmethod
    def _tree_nodes(pdf: PdfFile, ref: Ref) -> set[int]:
        nodes = {ref.num}
        for kid in pdf.get(ref).get(b"/Kids", []):
            if isinstance(kid, Ref):
                nodes |= _Joiner._tree_nodes(pdf, kid)
        return nodes

    def _drop_pages(self, state: _PartState) -> None:
        """Skip the pages the next part printed again, with their rows and content."""
        pdf = state.pdf
        dropped = state.dropped
        if not dropped:
            return
        state.skip |= dropped
        for page in dropped:
            node = pdf.get(Ref(page))
            for key in (b"/Contents", b"/Annots"):
                value = node.get(key)
                if key == b"/Annots" and isinstance(value, Ref):
                    state.skip.add(value.num)
                    value = pdf.get(value)
                state.skip |= {item.num for item in kids(value) if isinstance(item, Ref)}
        self._drop_rows(state, dropped)

    @staticmethod
    def _drop_rows(state: _PartState, dropped: set[int]) -> None:
        """Skip the table rows printed only on dropped pages, from either end."""
        pdf = state.pdf
        page_index = {num: position for position, num in enumerate(state.pages)}
        dropped_index = {page_index[num] for num in dropped}
        document = _document(pdf)
        runs: list[list[Ref]] = []
        if state.keep < len(state.pages):
            runs.append(list(reversed(table_rows(pdf, _last_path(pdf, document)[-1]))))
        if state.first:
            rows = table_rows(pdf, _first_path(pdf, document)[-1])
            runs.append(rows[state.part.header_rows :])
        for run in runs:
            for row in run:
                row_pages = _element_pages(pdf, row, page_index)
                if not row_pages or not row_pages <= dropped_index:
                    break
                state.skip |= _subtree(pdf, row)

    # -- structure ---------------------------------------------------------
    def _merge(self, state: _PartState, document: Ref) -> None:
        """Map this part's opening elements onto the joined tree and extend them."""
        pdf = state.pdf
        if not state.part.continues:
            node = pdf.get(document)
            state.alias[document.num] = self.chain[0]
            self._extend(
                self.held[self.chain[0]],
                [self._content(state, k, node) for k in kids(node.get(b"/K"))],
            )
            return
        path = _first_path(pdf, document)
        if len(path) != len(self.chain):
            raise JoinError(f"{pdf.path}: the continuation does not match the open table")
        for level, (ref, final) in enumerate(zip(path, self.chain, strict=True)):
            node = pdf.get(ref)
            held = self.held[final]
            if _role(node) != _role(held):
                raise JoinError(f"{pdf.path}: continuation structure differs at level {level}")
            state.alias[ref.num] = final
            if level + 1 < len(path):
                aligned = {path[level + 1].num}
            else:
                aligned = self._align_header(state, ref)
            rest = [
                k for k in kids(node.get(b"/K")) if not (isinstance(k, Ref) and k.num in aligned)
            ]
            self._extend(held, [self._content(state, k, node) for k in rest])

    def _align_header(self, state: _PartState, table: Ref) -> set[int]:
        rows = table_rows(state.pdf, table)
        count = state.part.header_rows
        if len(rows) < count or len(self.header) != count:
            raise JoinError(f"{state.pdf.path}: header rows do not match")
        for row, target in zip(rows[:count], self.header, strict=True):
            self._align(state, row, target)
        return {row.num for row in rows[:count]}

    def _align(self, state: _PartState, ref: Ref, target: int) -> None:
        """Map a repeated header subtree onto the first one, adding its content."""
        pdf = state.pdf
        node = pdf.get(ref)
        held = self.held.get(target)
        if held is None or _role(node) != _role(held):
            raise JoinError(f"{pdf.path}: the repeated header does not match")
        state.alias[ref.num] = target
        if b"/ID" in node and b"/ID" in held:
            state.ids[string_bytes(node[b"/ID"])] = string_bytes(held[b"/ID"])
        mine = _element_kids(pdf, node)
        theirs = [k for k in kids(held.get(b"/K")) if isinstance(k, Ref) and k.num in self.held]
        if len(mine) != len(theirs):
            raise JoinError(f"{pdf.path}: the repeated header does not match")
        for kid, other in zip(mine, theirs, strict=True):
            self._align(state, kid, other.num)
        content = [k for k in kids(node.get(b"/K")) if not _is_element(pdf, k)]
        self._extend(held, [self._content(state, k, node) for k in content])

    @staticmethod
    def _content(state: _PartState, item: Any, parent: dict) -> Any:
        """A kid of an element, a bare MCID turned into a page-qualified reference."""
        if isinstance(item, int):
            page = parent.get(b"/Pg")
            if not isinstance(page, Ref):
                raise JoinError(f"{state.pdf.path}: marked content without a page")
            return {
                Name(b"/Type"): Name(b"/MCR"),
                Name(b"/Pg"): Ref(state.map(page.num)),
                Name(b"/MCID"): item,
            }
        return _remap(item, state.map)

    @staticmethod
    def _extend(held: dict, items: list) -> None:
        """Append kids; content on the element's own page stays a bare MCID, as Chromium writes it."""
        if not items:
            return
        page = held.get(b"/Pg")
        own = [
            item[b"/MCID"]
            if isinstance(item, dict)
            and item.get(b"/Type") == Name(b"/MCR")
            and item.get(b"/Pg") == page
            and b"/Stm" not in item
            else item
            for item in items
        ]
        current = kids(held.get(b"/K"))
        combined = current + own
        held[Name(b"/K")] = combined[0] if len(combined) == 1 else combined

    def _hold_open_table(self, state: _PartState, document: Ref, header_rows: int | None) -> None:
        """Keep, unwritten, what a continuation of this part's last table will extend."""
        if header_rows is None:
            self.chain = self.chain[:1]
            self.header = []
            return
        pdf = state.pdf
        path = _last_path(pdf, document)
        state.hold |= {ref.num for ref in path if ref.num not in state.alias}
        rows = table_rows(pdf, path[-1])[:header_rows]
        for row in rows:
            if row.num not in state.alias:
                state.hold |= _subtree(pdf, row)
        self.chain = [state.map(ref.num) for ref in path]
        self.header = [state.map(row.num) for row in rows]

    # -- document-level entries -------------------------------------------
    def _parent_tree(self, state: _PartState, tree: dict) -> None:
        """Number this part's structure-parent keys after the earlier parts'.

        Keys of dropped pages and their annotations are left out and the rest
        renumbered without gaps, as one pass numbers them.
        """
        pdf = state.pdf
        dropped_keys: set[int] = set()
        for num in state.skip:
            node = pdf.get(Ref(num)) if num in pdf.offsets else None
            if not isinstance(node, dict):
                continue
            for key in (b"/StructParents", b"/StructParent"):
                if isinstance(node.get(key), int):
                    dropped_keys.add(node[key])
        entries = sorted(
            pdf.number_tree(tree[b"/ParentTree"]) if b"/ParentTree" in tree else [],
            key=lambda e: e[0],
        )
        for key, value in entries:
            if key in dropped_keys:
                if isinstance(value, Ref):
                    state.skip.add(value.num)
                continue
            state.keys[key] = self.key_base
            self.parent_tree.append((self.key_base, _remap(value, state.map)))
            self.key_base += 1

    def _destinations(self, state: _PartState) -> None:
        pdf = state.pdf
        dests = pdf.root.get(b"/Dests")
        if dests is None:
            return
        dropped = state.dropped
        for name, value in pdf.get(dests).items():
            destination = pdf.get(value)
            if isinstance(destination, dict):
                destination = destination.get(b"/D")
            if not (
                isinstance(destination, list) and destination and isinstance(destination[0], Ref)
            ):
                continue
            if destination[0].num in dropped:
                continue
            key = bytes(name)
            weak = _name_text(key) in state.part.stand_ins
            if weak and key in self.dests:
                continue
            self.dests[key] = _remap(destination, state.map)
            if weak:
                self.weak_dests.add(key)
            else:
                self.weak_dests.discard(key)

    def _element_ids(self, state: _PartState, tree: dict) -> None:
        if b"/IDTree" not in tree:
            return
        for key, value in state.pdf.name_tree(tree[b"/IDTree"]):
            if isinstance(value, Ref) and (value.num in state.alias or value.num in state.skip):
                continue
            self.ids[state.ids.get(key, key)] = _remap(value, state.map)

    # -- objects -----------------------------------------------------------
    def _copy_objects(self, state: _PartState, own: set[int]) -> None:
        pdf = state.pdf
        pages = set(state.pages)
        dropped = state.dropped
        on_dropped = (
            re.compile(rb"/Pg\s+(?:%s)\s+0\s+R" % b"|".join(b"%d" % p for p in dropped))
            if dropped
            else None
        )
        for position, num in enumerate(pdf.order):
            if position % 512 == 0:
                self.cancel_check()
            if num in own or num in state.skip or num in state.alias:
                continue
            obj = pdf.raw(num)
            final = state.map(num)
            if num in state.hold:
                self.held[final] = parse(_remap_bytes(obj.head, state.map, state.ids, state.keys))
                continue
            if on_dropped is not None and obj.stream is None and on_dropped.search(obj.head):
                page = parse(obj.head).get(b"/Pg")
                if isinstance(page, Ref) and page.num in dropped:
                    continue  # content that the next part printed again
            if num in pages:
                self._write_page(state, num, obj.head)
                continue
            head = _remap_bytes(obj.head, state.map, state.ids, state.keys)
            self.writer.write(final, head, obj.stream)

    def _write_page(self, state: _PartState, num: int, head: bytes) -> None:
        node = _remap(parse(head), state.map)
        node[Name(b"/Parent")] = Ref(self.pages_num)
        if isinstance(node.get(b"/StructParents"), int):
            node[Name(b"/StructParents")] = state.keys[node[b"/StructParents"]]
        if self.overlay is not None:
            self.overlay.apply(len(self.pages) + state.kept.index(num), node)
        final = state.map(num)
        self.writer.write(final, serialize(node))

    # -- document ----------------------------------------------------------
    def finish(self) -> int:
        writer = self.writer
        struct_num = writer.allocate()
        self.held[self.chain[0]][Name(b"/P")] = Ref(struct_num)
        for num, node in self.held.items():
            live = [kid for kid in kids(node.get(b"/K")) if self._live(kid)]
            node[Name(b"/K")] = live[0] if len(live) == 1 else live
            writer.write(num, serialize(node))
        pages = [Ref(page) for page in self.pages]
        writer.write(
            self.pages_num,
            serialize(
                {
                    Name(b"/Type"): Name(b"/Pages"),
                    Name(b"/Count"): len(pages),
                    Name(b"/Kids"): pages,
                }
            ),
        )
        catalog = self._catalog(struct_num)
        root = writer.allocate()
        writer.write(root, serialize(catalog))
        trailer: dict[Name, Any] = {Name(b"/Root"): Ref(root)}
        if self.info_head is not None:
            info = writer.allocate()
            writer.write(info, self.info_head)
            trailer[Name(b"/Info")] = Ref(info)
        writer.finish(trailer)
        return len(pages)

    def _catalog(self, struct_num: int) -> dict:
        writer = self.writer
        numbers: list[Any] = []
        for key, value in sorted(self.parent_tree, key=lambda item: item[0]):
            numbers += [key, value]
        parent_tree = writer.allocate()
        writer.write(
            parent_tree, serialize({Name(b"/Type"): Name(b"/ParentTree"), Name(b"/Nums"): numbers})
        )
        struct: dict[Name, Any] = {
            Name(b"/Type"): Name(b"/StructTreeRoot"),
            Name(b"/K"): Ref(self.chain[0]),
            Name(b"/ParentTree"): Ref(parent_tree),
            Name(b"/ParentTreeNextKey"): self.key_base,
        }
        live = sorted((key, value) for key, value in self.ids.items() if self._live(value))
        if live:
            names: list[Any] = []
            for key, value in live:
                names += [_string_token(key), value]
            id_tree = writer.allocate()
            limits = [_string_token(live[0][0]), _string_token(live[-1][0])]
            writer.write(id_tree, serialize({Name(b"/Limits"): limits, Name(b"/Names"): names}))
            struct[Name(b"/IDTree")] = Ref(id_tree)
        writer.write(struct_num, serialize(struct))
        if self.catalog is None:
            raise JoinError("no parts to join")
        catalog = {
            key: value
            for key, value in self.catalog.items()
            if key not in (b"/Pages", b"/StructTreeRoot", b"/Dests", b"/Outlines")
        }
        for key, value in catalog.items():
            if isinstance(value, Ref):
                raise JoinError("the catalog has an unexpected indirect %r" % key)
        catalog[Name(b"/Pages")] = Ref(self.pages_num)
        catalog[Name(b"/StructTreeRoot")] = Ref(struct_num)
        for key in self.weak_dests:
            # A stand-in whose real target never printed: no destination, as in one pass.
            self.dests.pop(key, None)
        if self.dests:
            dests = writer.allocate()
            writer.write(dests, serialize({Name(k): v for k, v in sorted(self.dests.items())}))
            catalog[Name(b"/Dests")] = Ref(dests)
        return catalog

    def _live(self, item: Any) -> bool:
        if isinstance(item, Ref):
            return self.writer.written(item.num) or item.num in self.held
        if isinstance(item, dict) and item.get(b"/Type") in (Name(b"/MCR"), Name(b"/OBJR")):
            page, target = item.get(b"/Pg"), item.get(b"/Obj")
            return (not isinstance(page, Ref) or self.writer.written(page.num)) and (
                not isinstance(target, Ref) or self.writer.written(target.num)
            )
        return True


class _Overlay:
    """Draw each page of a running-header PDF over the matching joined page.

    A pass numbers its pages from one, so the parts print without running
    headers and footers, and one pass over empty pages of the same sizes
    prints them all. Each of those pages becomes a form XObject drawn after
    the body content, in the page's default coordinate space, exactly where
    Chromium would have drawn it.
    """

    NAME = Name(b"/PfRunning")

    def __init__(self, path: str | Path, writer: Writer):
        self.pdf = PdfFile(path)
        self.writer = writer
        self.pages = self.pdf.pages()
        self.base = writer.reserve(len(self.pdf.ends))
        self.open = writer.allocate()
        self.close = writer.allocate()
        skip = self._not_copied()
        for num in self.pdf.order:
            if num in skip:
                continue
            obj = self.pdf.raw(num)
            writer.write(self.base + num, remap_references(obj.head, self._map), obj.stream)
        writer.write(self.open, b"<</Length 2>>", b"q\n")
        body = b"Q\n" + bytes(self.NAME) + b" Do\n"
        writer.write(self.close, b"<</Length %d>>" % len(body), body)

    def _map(self, num: int) -> int:
        return self.base + num

    def _not_copied(self) -> set[int]:
        """Pages, their content (it becomes forms) and the document-level objects."""
        pdf = self.pdf
        root = pdf.root
        skip = {pdf.root_ref.num, *pdf.page_tree, *self.pages}
        for page in self.pages:
            skip |= {
                item.num
                for item in kids(pdf.get(Ref(page)).get(b"/Contents"))
                if isinstance(item, Ref)
            }
        struct = root.get(b"/StructTreeRoot")
        if isinstance(struct, Ref):
            skip |= self._reachable(struct)
        if isinstance(root.get(b"/Dests"), Ref):
            skip.add(root[b"/Dests"].num)
        info = pdf.trailer.get(b"/Info")
        if isinstance(info, Ref):
            skip.add(info.num)
        return skip

    def _reachable(self, start: Ref) -> set[int]:
        """Objects of the empty document's structure tree, without its pages."""
        pages = set(self.pages)
        found: set[int] = set()
        stack: list[Any] = [start]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                stack.extend(item.values())
            elif isinstance(item, Ref) and item.num not in found and item.num not in pages:
                found.add(item.num)
                stack.append(self.pdf.get(item))
        return found

    def apply(self, index: int, page: dict) -> None:
        if index >= len(self.pages):
            raise JoinError("the running-header pass has fewer pages than the document")
        page[Name(b"/Contents")] = [Ref(self.open), *kids(page.get(b"/Contents")), Ref(self.close)]
        resources = page.get(b"/Resources")
        if not isinstance(resources, dict) or isinstance(resources.get(b"/XObject"), Ref):
            raise JoinError("page resources are not direct dictionaries")
        forms = dict(resources.get(b"/XObject") or {})
        forms[self.NAME] = Ref(self._form(index))
        resources[Name(b"/XObject")] = forms

    def _form(self, index: int) -> int:
        page = self.pdf.get(Ref(self.pages[index]))
        data = b""
        for item in kids(page.get(b"/Contents")):
            obj = self.pdf.raw(item.num)
            content_filter = parse(obj.head).get(b"/Filter")
            stream = obj.stream or b""
            if content_filter == Name(b"/FlateDecode"):
                stream = zlib.decompress(stream)
            elif content_filter is not None:
                raise JoinError("unsupported filter in running-header content")
            data += stream + b"\n"
        encoded = zlib.compress(data)
        resources = serialize(_remap(page[b"/Resources"], self._map))
        num = self.writer.allocate()
        self.writer.write(
            num,
            b"<</Type /XObject /Subtype /Form /BBox %s /Resources %s /Filter /FlateDecode /Length %d>>"
            % (serialize(page[b"/MediaBox"]), resources, len(encoded)),
            encoded,
        )
        return num


def join(
    parts: list[Part],
    out: str | Path,
    *,
    overlay: str | Path | None = None,
    cancel_check: Callable[[], None] | None = None,
) -> int:
    """Write the joined document to ``out``; returns its page count.

    A part followed by a continuation must end in the table that part
    continues, and its ``keep`` must already leave out the pages the
    continuation printed again.
    """
    with open(out, "wb") as handle:
        writer = Writer(handle)
        joiner = _Joiner(
            writer, _Overlay(overlay, writer) if overlay else None, cancel_check or (lambda: None)
        )
        for index, part in enumerate(parts):
            following = parts[index + 1] if index + 1 < len(parts) else None
            cut = following.header_rows if following is not None and following.continues else None
            joiner.add(index, part, cut)
        return joiner.finish()
