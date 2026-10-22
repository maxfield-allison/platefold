"""Finish a printed PDF in one streaming pass over its objects.

Finishing adds the outline and document properties and applies the
accessibility repairs of ``pdf_semantics``: decoration marked as artifacts,
list-item bodies, link descriptions and XMP metadata. A general PDF library
does that by loading the whole document, which for a 100,000-cell table took
over three minutes and 1.4 GB. This module rewrites the file object by object
instead: every object it does not change is copied byte for byte, page
content is marked one page at a time, and only the few objects it edits are
parsed. The result is a new file with one cross-reference table, not an
incremental update, so the replaced content streams do not stay behind and
double its size.

It reads Chromium's output shape (see ``pdfobjects``); other shapes raise
``PdfShapeError`` and the caller finishes the file with pypdf instead.
"""

from __future__ import annotations

import bisect
import re
import zlib
from array import array
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pdf_semantics import _LIST_PARTS, STANDARD_TYPES, _xmp, role_map
from .pdfobjects import (
    Name,
    PdfFile,
    PdfShapeError,
    Raw,
    Ref,
    Writer,
    kids,
    parse,
    serialize,
    string_bytes,
    text_string,
)

# Operators as pdf_semantics.mark_artifacts groups them.
_PATH = {b"m", b"l", b"c", b"v", b"y", b"h", b"re"}
_CLIP = {b"W", b"W*"}
_PAINT = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
_SINGLE = {b"Tj", b"TJ", b"'", b'"', b"Do", b"sh"}
_OPEN = {b"BDC", b"BMC"}
_KEYWORDS = {b"true", b"false", b"null"}

# Operators, and the operand tokens that could hide an operator-like word.
# Numbers are skipped by the search itself. Literal strings are not matched:
# a page holding one goes through pypdf's parser instead (see mark_page).
_CONTENT_TOKEN = re.compile(
    rb"<<|>>|<[0-9A-Fa-f\s]*>|/[^\s/\[\]()<>{}%]*|%[^\r\n]*|([A-Za-z'\"][^\s/\[\]()<>{}%]*)"
)


def mark_page(data: bytes) -> tuple[bytes, int]:
    """Wrap unmarked painting on one page as artifacts; same result as mark_artifacts.

    Works on byte offsets rather than a parsed operation list, which is what
    makes a page of thousands of table cells take milliseconds.
    """
    if b"(" in data or re.search(rb"(?<![\w/])BI(?![\w])", data):
        return _mark_page_with_pypdf(data)
    operators = _operators(data)
    spans = _artifact_spans([name for name, _, _ in operators])
    if not spans:
        return data, 0
    pieces: list[bytes] = []
    written = 0
    for first, last in spans:
        start, end = operators[first][1], operators[last][2]
        pieces += [data[written:start], b"\n/Artifact BMC", data[start:end], b"\nEMC"]
        written = end
    pieces.append(data[written:])
    return b"".join(pieces), len(spans)


def _operators(data: bytes) -> list[tuple[bytes, int, int]]:
    """Each operator with the offsets of its operands' start and its own end."""
    operators: list[tuple[bytes, int, int]] = []
    previous = 0
    for match in _CONTENT_TOKEN.finditer(data):
        name = match.group(1)
        if name is None or name in _KEYWORDS:
            continue
        operators.append((name, previous, match.end()))
        previous = match.end()
    return operators


def _artifact_spans(names: list[bytes]) -> list[tuple[int, int]]:
    """Runs of operators to wrap, grouped exactly as mark_artifacts groups them."""
    spans: list[tuple[int, int]] = []
    depth = 0
    index = 0
    count = len(names)
    while index < count:
        name = names[index]
        if depth == 0 and name in _PATH:
            end = index
            while end < count and names[end] in _PATH | _CLIP:
                end += 1
            if end < count and names[end] in _PAINT:
                spans.append((index, end))
                end += 1
            index = end
            continue
        if depth == 0 and name in _SINGLE:
            spans.append((index, index))
        elif name in _OPEN:
            depth += 1
        elif name == b"EMC":
            depth = max(0, depth - 1)
        index += 1
    return spans


def _mark_page_with_pypdf(data: bytes) -> tuple[bytes, int]:
    from pypdf.generic import ContentStream, DecodedStreamObject

    from .pdf_semantics import mark_artifacts

    stream = DecodedStreamObject()
    stream.set_data(data)
    content = ContentStream(stream, None)
    operations, wrapped = mark_artifacts(content.operations)
    if not wrapped:
        return data, 0
    content.operations = operations
    return content.get_data(), wrapped


# ------------------------------------------------------------------ editing


def _name_text(name: bytes) -> str:
    raw = re.sub(rb"#([0-9A-Fa-f]{2})", lambda m: bytes([int(m.group(1), 16)]), bytes(name)[1:])
    return raw.decode("utf-8", errors="replace")


class _Edits:
    """Parsed objects being changed, and new objects, by number."""

    def __init__(self, pdf: PdfFile):
        self.pdf = pdf
        self.changed: dict[int, Any] = {}
        self.added: dict[int, tuple[bytes, bytes | None]] = {}
        size = pdf.trailer.get(b"/Size")
        if not isinstance(size, int):
            raise PdfShapeError(f"{pdf.path}: trailer has no object count")
        self.size = size

    def load(self, num: int) -> Any:
        if num not in self.changed:
            self.changed[num] = parse(self.pdf.raw(num).head)
        return self.changed[num]

    def resolve(self, value: Any) -> Any:
        return self.load(value.num) if isinstance(value, Ref) else value

    def add(self, head: bytes, stream: bytes | None = None) -> Ref:
        num = self.size
        self.size += 1
        self.added[num] = (head, stream)
        return Ref(num)

    def reserve(self) -> Ref:
        num = self.size
        self.size += 1
        return Ref(num)


class _ParentTree:
    """StructParents keys to the objects holding their entries."""

    def __init__(self, edits: _Edits, tree: dict):
        self.edits = edits
        self.leaves: list[int] = []  # numbers of number-tree nodes with /Nums
        root = tree.get(b"/ParentTree")
        if isinstance(root, Ref):
            self._collect(root.num, 0)

    def _collect(self, num: int, depth: int) -> None:
        if depth > 32:
            raise PdfShapeError("parent tree too deep")
        node = self.edits.pdf.get(Ref(num))
        if b"/Nums" in node:
            self.leaves.append(num)
        for kid in node.get(b"/Kids", []):
            if isinstance(kid, Ref):
                self._collect(kid.num, depth + 1)

    def repoint(self, key, mcid: int | None, old: Ref, new: Ref) -> None:
        """As pdf_semantics._ParentTree.repoint: one backlink from ``old`` to ``new``."""
        if not isinstance(key, int):
            return
        for leaf in self.leaves:
            node = self.edits.changed.get(leaf) or self.edits.pdf.get(Ref(leaf))
            numbers = node[b"/Nums"]
            for position in range(0, len(numbers) - 1, 2):
                if numbers[position] != key:
                    continue
                value = numbers[position + 1]
                if mcid is None:
                    if value == old:
                        self.edits.load(leaf)[b"/Nums"][position + 1] = new
                    continue
                array = self.edits.resolve(value) if isinstance(value, Ref) else value
                if not (isinstance(array, list) and 0 <= mcid < len(array) and array[mcid] == old):
                    continue
                if isinstance(value, Ref):
                    array[mcid] = new
                else:
                    self.edits.load(leaf)[b"/Nums"][position + 1][mcid] = new


def _role(edits: _Edits, kid) -> bytes | None:
    if not isinstance(kid, Ref):
        return None
    node = edits.pdf.get(kid) if kid.num not in edits.changed else edits.changed[kid.num]
    if isinstance(node, dict) and node.get(b"/Type", Name(b"/StructElem")) == Name(b"/StructElem"):
        role = node.get(b"/S")
        return bytes(role) if role is not None else None
    return None


_LIST_ITEM = re.compile(rb"/S\s*/LI(?=[\s/>\]])")


def wrap_list_bodies(edits: _Edits, tree: dict) -> int:
    """pdf_semantics.wrap_list_bodies, over only the LI elements' objects."""
    pdf = edits.pdf
    starts = array("q", (pdf.offsets[num] for num in pdf.order))
    items: list[int] = []
    for match in _LIST_ITEM.finditer(pdf.data):
        position = bisect.bisect_right(starts, match.start()) - 1
        if position < 0:
            continue
        num = pdf.order[position]
        if match.start() < pdf.ends[num] and (not items or items[-1] != num):
            items.append(num)
    parents = _ParentTree(edits, tree)
    wrapped = 0
    for num in items:
        raw = pdf.raw(num)
        if raw.stream is not None:
            continue
        item = edits.load(num)
        if not isinstance(item, dict) or item.get(b"/S") != Name(b"/LI"):
            continue
        children = kids(item.get(b"/K"))
        loose = [kid for kid in children if (_role(edits, kid) or b"").decode() not in _LIST_PARTS]
        if not loose:
            continue
        body_ref = edits.reserve()
        body = {
            Name(b"/Type"): Name(b"/StructElem"),
            Name(b"/S"): Name(b"/LBody"),
            Name(b"/P"): Ref(num),
            Name(b"/K"): loose,
        }
        if b"/Pg" in item:
            body[Name(b"/Pg")] = item[b"/Pg"]
        edits.changed[body_ref.num] = body
        for kid in loose:
            _move_to_body(edits, parents, item, Ref(num), kid, body_ref)
        parts = [kid for kid in children if (_role(edits, kid) or b"").decode() in _LIST_PARTS]
        item[Name(b"/K")] = [*parts, body_ref]
        wrapped += 1
    return wrapped


def _move_to_body(edits, parents, item: dict, item_ref: Ref, kid, body_ref: Ref) -> None:
    value = edits.resolve(kid)
    if isinstance(value, int):
        page = edits.pdf.get(item.get(b"/Pg")) if isinstance(item.get(b"/Pg"), Ref) else {}
        parents.repoint(page.get(b"/StructParents"), value, item_ref, body_ref)
    elif isinstance(value, dict) and value.get(b"/Type") == Name(b"/MCR"):
        holder_ref = value.get(b"/Stm", value.get(b"/Pg", item.get(b"/Pg")))
        holder = edits.pdf.get(holder_ref) if isinstance(holder_ref, Ref) else {}
        parents.repoint(holder.get(b"/StructParents"), value.get(b"/MCID"), item_ref, body_ref)
    elif isinstance(value, dict) and value.get(b"/Type") == Name(b"/OBJR"):
        target = edits.pdf.get(value.get(b"/Obj"))
        parents.repoint(target.get(b"/StructParent"), None, item_ref, body_ref)
    elif isinstance(value, dict) and isinstance(kid, Ref):
        value[Name(b"/P")] = body_ref


_ROLE = re.compile(rb"/S\s*(/[A-Za-z0-9_.#-]+)")


def map_roles(edits: _Edits, tree_ref: Ref) -> int:
    """pdf_semantics.map_roles, reading role names from the raw file.

    A name that is not a standard structure type is confirmed by parsing one
    object that uses it as a structure element's /S, since actions and soft
    masks also carry /S. Standard names need no confirmation.
    """
    pdf = edits.pdf
    starts = array("q", (pdf.offsets[num] for num in pdf.order))
    candidates: dict[str, list[int]] = {}
    for match in _ROLE.finditer(pdf.data):
        role = match[1].decode("latin-1")
        if role in STANDARD_TYPES or len(candidates.get(role, ())) >= 8:
            continue
        position = bisect.bisect_right(starts, match.start()) - 1
        if position >= 0:
            candidates.setdefault(role, []).append(pdf.order[position])
    roles = set()
    for role, nums in candidates.items():
        for num in nums:
            if pdf.raw(num).stream is not None:
                continue
            node = parse(pdf.raw(num).head)
            if (
                isinstance(node, dict)
                and node.get(b"/S") == Name(role.encode("latin-1"))
                and b"/P" in node
                and node.get(b"/Type", Name(b"/StructElem")) == Name(b"/StructElem")
            ):
                roles.add(role)
                break
    tree = edits.load(tree_ref.num)
    current = edits.resolve(tree.get(b"/RoleMap")) or {}
    existing = {bytes(k).decode("latin-1"): bytes(v).decode("latin-1") for k, v in current.items()}
    mapping = role_map(roles, existing)
    if mapping == existing:
        return 0
    tree[Name(b"/RoleMap")] = {
        Name(k.encode("latin-1")): Name(v.encode("latin-1")) for k, v in mapping.items()
    }
    return len(mapping) - len(existing)


def describe_links(edits: _Edits, pages: list[int], names: dict[str, str]) -> int:
    """pdf_semantics.describe_links, editing only the link annotations."""
    described = 0
    for page_num in pages:
        page = edits.pdf.get(Ref(page_num))
        for ref in kids(edits.pdf.get(page.get(b"/Annots"))):
            if not isinstance(ref, Ref):
                continue  # a direct annotation would need its page rewritten
            annotation = edits.pdf.get(ref)
            if annotation.get(b"/Subtype") != Name(b"/Link") or b"/Contents" in annotation:
                continue
            action = edits.pdf.get(annotation.get(b"/A", {}))
            destination = annotation.get(b"/Dest", action.get(b"/D"))
            if b"/URI" in action:
                uri = string_bytes(action[b"/URI"])
                text = uri.decode("utf-8", errors="replace")
            elif isinstance(destination, Name):
                anchor = _name_text(destination)
                text = names.get(anchor, anchor)
            elif isinstance(destination, Raw) and destination[:1] in (b"(", b"<"):
                anchor = string_bytes(destination).decode("utf-8", errors="replace")
                text = names.get(anchor, anchor)
            else:
                continue
            edits.load(ref.num)[Name(b"/Contents")] = text_string(text)
            described += 1
    return described


def _outline(edits: _Edits, nodes: list[dict], page_refs: list[Ref]) -> Ref | None:
    if not nodes:
        return None
    outline_ref = edits.reserve()
    outline: dict[Name, Any] = {Name(b"/Type"): Name(b"/Outlines")}
    outline[Name(b"/Count")] = _link_outline(edits, nodes, outline_ref, outline, page_refs)
    edits.changed[outline_ref.num] = outline
    return outline_ref


def _link_outline(edits, nodes, parent: Ref, holder: dict, page_refs: list[Ref]) -> int:
    refs = [edits.reserve() for _ in nodes]
    holder[Name(b"/First")] = refs[0]
    holder[Name(b"/Last")] = refs[-1]
    count = len(nodes)
    for index, (node, ref) in enumerate(zip(nodes, refs, strict=True)):
        item: dict[Name, Any] = {
            Name(b"/Title"): text_string(node["title"]),
            Name(b"/Parent"): parent,
            Name(b"/Dest"): [page_refs[node["page"]], Name(b"/Fit")],
        }
        if index:
            item[Name(b"/Prev")] = refs[index - 1]
        if index + 1 < len(refs):
            item[Name(b"/Next")] = refs[index + 1]
        if node["children"]:
            # A positive count leaves the entry expanded, as pypdf writes it.
            descendants = _link_outline(edits, node["children"], ref, item, page_refs)
            item[Name(b"/Count")] = descendants
            count += descendants
        edits.changed[ref.num] = item
    return count


def outline_nodes(info: dict, pages: dict[str, int], page_count: int) -> list[dict]:
    """Nest the mapped headings by level, as pdf.add_bookmarks nests them."""
    roots: list[dict] = []
    parents: dict[int, dict] = {}
    for anchor in info["order"]:
        if anchor not in pages:
            continue
        level = info["levels"][anchor]
        node = {
            "title": info["names"][anchor][:110],
            "page": min(pages[anchor] - 1, page_count - 1),
            "children": [],
        }
        parent = parents.get(level - 1) if level > 1 else None
        (parent["children"] if parent is not None else roots).append(node)
        parents[level] = node
    return roots


# ------------------------------------------------------------------ writing


def finish_file(
    source: str | Path,
    destination: str | Path,
    info: dict,
    properties: dict[str, str],
    outline: list[dict] | None,
    cancel_check: Callable[[], None] | None = None,
) -> dict[str, int]:
    """Write ``source`` to ``destination`` finished; returns what changed."""
    pdf = PdfFile(source)
    edits = _Edits(pdf)
    pages = pdf.pages()
    counts = {"artifacts": 0, "list_bodies": 0, "links": 0, "roles": 0}
    tree_ref = pdf.root.get(b"/StructTreeRoot")
    tree = pdf.get(tree_ref)
    marked = _Contents()
    if isinstance(tree, dict):
        if not isinstance(tree_ref, Ref):
            raise PdfShapeError(f"{pdf.path}: the structure tree root is not an object")
        counts["list_bodies"] = wrap_list_bodies(edits, tree)
        counts["roles"] = map_roles(edits, tree_ref)
        marked = _tagged_page_contents(edits, pages)
    counts["links"] = describe_links(edits, pages, info.get("names", {}))
    info_ref = _document_level(edits, pages, info, properties, outline)
    trailer = {Name(b"/Root"): pdf.root_ref, Name(b"/Info"): info_ref}
    if b"/ID" in pdf.trailer:
        trailer[Name(b"/ID")] = pdf.trailer[b"/ID"]
    with open(destination, "wb") as handle:
        writer = Writer(handle, edits.size)
        counts["artifacts"] = _write_objects(
            pdf, writer, edits, marked, cancel_check or (lambda: None)
        )
        writer.finish(trailer)
    return counts


@dataclass
class _Contents:
    """How each tagged page's marked content is written."""

    in_place: dict[int, int] = field(default_factory=dict)  # its only stream -> page
    combined: dict[int, tuple[list[int], int]] = field(default_factory=dict)  # page -> streams, new
    unused: set[int] = field(default_factory=set)  # streams no page draws any more


def _tagged_page_contents(edits: _Edits, pages: list[int]) -> _Contents:
    """Plan the content rewrite of every tagged page.

    A page drawn by one stream of its own is rewritten in place. A page drawn
    by several streams, or by one another page shares (a joined document
    draws running headers through shared streams), gets one new stream; the
    streams no page uses afterwards are left out of the file.
    """
    pdf = edits.pdf
    streams_of: dict[int, list[int]] = {}
    users: dict[int, int] = {}
    tagged: set[int] = set()
    for page_num in pages:
        page = pdf.get(Ref(page_num))
        streams = [ref.num for ref in kids(page.get(b"/Contents")) if isinstance(ref, Ref)]
        streams_of[page_num] = streams
        for num in streams:
            users[num] = users.get(num, 0) + 1
        if b"/StructParents" in page and streams:
            tagged.add(page_num)
    plan = _Contents()
    still_used: set[int] = set()
    for page_num, streams in streams_of.items():
        if page_num not in tagged:
            still_used.update(streams)
        elif len(streams) == 1 and users[streams[0]] == 1:
            plan.in_place[streams[0]] = page_num
        else:
            new = edits.reserve()
            edits.load(page_num)[Name(b"/Contents")] = new
            plan.combined[page_num] = (streams, new.num)
    for streams, _new in plan.combined.values():
        plan.unused.update(num for num in streams if num not in still_used)
    return plan


def _document_level(edits: _Edits, pages, info: dict, properties, outline) -> Ref:
    """Outline, XMP metadata and document properties; returns the new Info."""
    pdf = edits.pdf
    root = dict(pdf.root)
    outline_ref = _outline(edits, outline or [], [Ref(num) for num in pages])
    if outline_ref is not None:
        root[Name(b"/Outlines")] = outline_ref
    metadata = _xmp(properties, info.get("language"))
    root[Name(b"/Metadata")] = edits.add(
        b"<</Type /Metadata /Subtype /XML /Length %d>>" % len(metadata), metadata
    )
    edits.changed[pdf.root_ref.num] = root
    information = dict(pdf.get(pdf.trailer.get(b"/Info", {})) or {})
    for key, value in properties.items():
        information[Name(key.encode("ascii"))] = text_string(value)
    return edits.add(serialize(information))


def _write_objects(pdf, writer: Writer, edits: _Edits, marked: _Contents, cancel_check) -> int:
    """Every object in file order, changed ones replaced; returns artifacts marked."""
    artifacts = 0
    for position, num in enumerate(pdf.order):
        if position % 256 == 0:
            cancel_check()
        if num in marked.in_place:
            artifacts += _write_marked(pdf, writer, [num], num)
        elif num in marked.unused:
            continue
        elif num in edits.changed:
            writer.write(num, serialize(edits.changed[num]))
        else:
            writer.copy(num, pdf.span(num))
    for streams, new in marked.combined.values():
        cancel_check()
        artifacts += _write_marked(pdf, writer, streams, new)
    for num, value in edits.changed.items():
        if num not in pdf.offsets:
            writer.write(num, serialize(value))
    for num, (head, stream) in edits.added.items():
        writer.write(num, head, stream)
    return artifacts


def _write_marked(pdf: PdfFile, writer: Writer, streams: list[int], target: int) -> int:
    """Write one page's content, marked, as object ``target``."""
    data = b"".join(_decoded(pdf, num) + (b"\n" if len(streams) > 1 else b"") for num in streams)
    marked, wrapped = mark_page(data)
    if not wrapped and streams == [target]:
        writer.copy(target, pdf.span(target))
        return 0
    encoded = zlib.compress(marked, 6)
    writer.write(target, b"<</Filter /FlateDecode\n/Length %d>>" % len(encoded), encoded)
    return wrapped


def _decoded(pdf: PdfFile, num: int) -> bytes:
    raw = pdf.raw(num)
    head = parse(raw.head)
    if raw.stream is None:
        raise PdfShapeError(f"{pdf.path}: page content {num} is not a stream")
    extra = set(head) - {b"/Filter", b"/Length"}
    if extra:
        raise PdfShapeError(f"{pdf.path}: page content {num} has {sorted(extra)}")
    content_filter = head.get(b"/Filter")
    if content_filter is None:
        return raw.stream
    if content_filter != Name(b"/FlateDecode"):
        raise PdfShapeError(f"{pdf.path}: page content {num} uses {content_filter!r}")
    return zlib.decompress(raw.stream)
