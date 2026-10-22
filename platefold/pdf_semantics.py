"""Finish Chromium's tagged PDF output for assistive technology.

Chromium tags real content but leaves page backgrounds, borders and rules
unmarked, places contents-page links directly inside list items, and writes
no link descriptions or XMP metadata. These repairs change only
marked-content operators, structure elements, annotation descriptions and
document metadata, so every page renders exactly as printed. They do not make
a document conform to PDF/UA, and Plate|Fold writes no PDF/UA identifier.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any
from xml.sax.saxutils import escape

from pypdf import PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    IndirectObject,
    NameObject,
    TextStringObject,
)

_PATH = {b"m", b"l", b"c", b"v", b"y", b"h", b"re"}
_CLIP = {b"W", b"W*"}
_PAINT = {b"S", b"s", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}
_SINGLE = {b"Tj", b"TJ", b"'", b'"', b"Do", b"sh", b"INLINE IMAGE"}
_OPEN = {b"BDC", b"BMC"}
_LIST_PARTS = {"/Lbl", "/LBody"}


def _resolve(value: Any) -> Any:
    return value.get_object() if isinstance(value, IndirectObject) else value


def _same(left: Any, right: Any) -> bool:
    left = left if isinstance(left, IndirectObject) else getattr(left, "indirect_reference", None)
    right = (
        right if isinstance(right, IndirectObject) else getattr(right, "indirect_reference", None)
    )
    return (
        isinstance(left, IndirectObject)
        and isinstance(right, IndirectObject)
        and left.idnum == right.idnum
    )


def _artifact(operations: list) -> list:
    return [([NameObject("/Artifact")], b"BMC"), *operations, ([], b"EMC")]


def mark_artifacts(operations: list) -> tuple[list, int]:
    """Wrap painting that sits outside every marked-content sequence.

    A path is wrapped from its first construction operator through its
    painting operator, so no marked-content operator lands inside a path
    object. Clipping paths ended by ``n`` paint nothing and stay unwrapped.
    """
    result: list = []
    depth = 0
    wrapped = 0
    index = 0
    while index < len(operations):
        operands, operator = operations[index]
        if depth == 0 and operator in _PATH:
            end = index
            while end < len(operations) and operations[end][1] in _PATH | _CLIP:
                end += 1
            if end < len(operations) and operations[end][1] in _PAINT:
                result.extend(_artifact(operations[index : end + 1]))
                wrapped += 1
                index = end + 1
            else:
                result.extend(operations[index:end])
                index = end
            continue
        if depth == 0 and operator in _SINGLE:
            result.extend(_artifact([(operands, operator)]))
            wrapped += 1
        else:
            if operator in _OPEN:
                depth += 1
            elif operator == b"EMC":
                depth = max(0, depth - 1)
            result.append((operands, operator))
        index += 1
    return result, wrapped


class _ParentTree:
    """Editable view of the number tree from StructParents keys to elements."""

    def __init__(self, tree: DictionaryObject):
        self.leaves: list[ArrayObject] = []
        self._collect(tree.get("/ParentTree"))

    def _collect(self, ref: Any) -> None:
        node = _resolve(ref)
        if not isinstance(node, DictionaryObject):
            return
        if "/Nums" in node:
            self.leaves.append(_resolve(node["/Nums"]))
        for child in _resolve(node.get("/Kids", ArrayObject())):
            self._collect(child)

    def _entries(self) -> Iterator[tuple[ArrayObject, int]]:
        for numbers in self.leaves:
            for position in range(0, len(numbers) - 1, 2):
                yield numbers, position

    def repoint(self, key: Any, mcid: int | None, old: Any, new: IndirectObject) -> None:
        """Point one backlink at ``new``: an MCID's slot, or an object's key."""
        if key is None:
            return
        for numbers, position in self._entries():
            if int(numbers[position]) != int(key):
                continue
            value = numbers[position + 1]
            if mcid is None:
                if _same(value, old):
                    numbers[position + 1] = new
                continue
            array = _resolve(value)
            if (
                isinstance(array, ArrayObject)
                and 0 <= mcid < len(array)
                and _same(array[mcid], old)
            ):
                array[mcid] = new


def _kids(element: DictionaryObject) -> list:
    kids = element.get("/K")
    if kids is None:
        return []
    resolved = _resolve(kids)
    return list(resolved) if isinstance(resolved, ArrayObject) else [kids]


def _role(kid: Any) -> str | None:
    value = _resolve(kid)
    if isinstance(value, DictionaryObject) and value.get("/Type", "/StructElem") == "/StructElem":
        role = value.get("/S")
        return str(role) if role is not None else None
    return None


def _walk(ref: Any, seen: set[int]) -> Iterator[tuple[Any, DictionaryObject]]:
    if isinstance(ref, IndirectObject):
        if ref.idnum in seen:
            return
        seen.add(ref.idnum)
    node = _resolve(ref)
    if isinstance(node, ArrayObject):
        for child in node:
            yield from _walk(child, seen)
    elif isinstance(node, DictionaryObject) and node.get("/Type") not in ("/MCR", "/OBJR"):
        if "/S" in node:
            yield ref, node
        if "/K" in node:
            yield from _walk(node["/K"], seen)


def wrap_list_bodies(writer: PdfWriter, tree: DictionaryObject) -> int:
    """Give each list item's content an LBody, as ISO 32000 list structure requires."""
    parents = _ParentTree(tree)
    wrapped = 0
    for ref, item in list(_walk(tree.get("/K"), set())):
        if item.get("/S") != "/LI" or not isinstance(ref, IndirectObject):
            continue
        kids = _kids(item)
        loose = [kid for kid in kids if _role(kid) not in _LIST_PARTS]
        if not loose:
            continue
        body = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/StructElem"),
                NameObject("/S"): NameObject("/LBody"),
                NameObject("/P"): ref,
                NameObject("/K"): ArrayObject(loose),
            }
        )
        if "/Pg" in item:
            body[NameObject("/Pg")] = item["/Pg"]
        body_ref = writer._add_object(body)
        for kid in loose:
            value = _resolve(kid)
            if isinstance(value, int):
                parents.repoint(
                    _resolve(item.get("/Pg", {})).get("/StructParents"), int(value), ref, body_ref
                )
            elif isinstance(value, DictionaryObject) and value.get("/Type") == "/MCR":
                page = value.get("/Pg", item.get("/Pg"))
                holder = _resolve(value.get("/Stm", page)) or {}
                mcid = _resolve(value["/MCID"])
                parents.repoint(holder.get("/StructParents"), int(mcid), ref, body_ref)
            elif isinstance(value, DictionaryObject) and value.get("/Type") == "/OBJR":
                target = _resolve(value["/Obj"])
                parents.repoint(target.get("/StructParent"), None, ref, body_ref)
            elif isinstance(value, DictionaryObject):
                value[NameObject("/P")] = body_ref
        parts = [kid for kid in kids if _role(kid) in _LIST_PARTS]
        item[NameObject("/K")] = ArrayObject([*parts, body_ref])
        wrapped += 1
    return wrapped


def describe_links(writer: PdfWriter, names: dict[str, str]) -> int:
    """Give every link annotation a Contents description.

    External links are described by their address. Internal links use the
    heading they lead to, falling back to the destination's name.
    """
    described = 0
    for page in writer.pages:
        for ref in _resolve(page.get("/Annots", ArrayObject())):
            annotation = _resolve(ref)
            if annotation.get("/Subtype") != "/Link" or "/Contents" in annotation:
                continue
            action = _resolve(annotation.get("/A", DictionaryObject()))
            destination = annotation.get("/Dest", action.get("/D"))
            if "/URI" in action:
                text = str(action["/URI"])
            elif destination is not None and not isinstance(_resolve(destination), ArrayObject):
                anchor = str(destination).lstrip("/")
                text = names.get(anchor, anchor)
            else:
                continue
            annotation[NameObject("/Contents")] = TextStringObject(text)
            described += 1
    return described


def _xmp(properties: dict[str, str], language: str | None) -> bytes:
    def item(value: str) -> str:
        return escape(value)

    fields = [
        '<dc:title><rdf:Alt><rdf:li xml:lang="x-default">%s</rdf:li></rdf:Alt></dc:title>'
        % item(properties.get("/Title", ""))
    ]
    if properties.get("/Author"):
        fields.append(
            "<dc:creator><rdf:Seq><rdf:li>%s</rdf:li></rdf:Seq></dc:creator>"
            % item(properties["/Author"])
        )
    if properties.get("/Subject"):
        fields.append(
            '<dc:description><rdf:Alt><rdf:li xml:lang="x-default">%s</rdf:li></rdf:Alt>'
            "</dc:description>" % item(properties["/Subject"])
        )
    if language:
        fields.append(
            "<dc:language><rdf:Bag><rdf:li>%s</rdf:li></rdf:Bag></dc:language>" % item(language)
        )
    if properties.get("/Keywords"):
        fields.append("<pdf:Keywords>%s</pdf:Keywords>" % item(properties["/Keywords"]))
    fields.append("<pdf:Producer>%s</pdf:Producer>" % item(properties.get("/Producer", "")))
    fields.append("<xmp:CreatorTool>%s</xmp:CreatorTool>" % item(properties.get("/Creator", "")))
    packet = (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:pdf="http://ns.adobe.com/pdf/1.3/" xmlns:xmp="http://ns.adobe.com/xap/1.0/">'
        + "".join(fields)
        + '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
    )
    return packet.encode("utf-8")


def write_metadata(writer: PdfWriter, properties: dict[str, str], language: str | None) -> None:
    """Mirror the document properties into an XMP metadata stream."""
    stream = DecodedStreamObject()
    stream[NameObject("/Type")] = NameObject("/Metadata")
    stream[NameObject("/Subtype")] = NameObject("/XML")
    stream.set_data(_xmp(properties, language))
    writer._root_object[NameObject("/Metadata")] = writer._add_object(stream)


# ISO 32000-1 14.8.4 standard structure types. PDF/UA-1 rule 7.1-5 requires
# any other type to be role-mapped to one of these.
STANDARD_TYPES = frozenset(
    "/" + name
    for name in [
        "Document",
        "Part",
        "Art",
        "Sect",
        "Div",
        "BlockQuote",
        "Caption",
        "TOC",
        "TOCI",
        "Index",
        "NonStruct",
        "Private",
        "H",
        "H1",
        "H2",
        "H3",
        "H4",
        "H5",
        "H6",
        "P",
        "L",
        "LI",
        "Lbl",
        "LBody",
        "Table",
        "TR",
        "TH",
        "TD",
        "THead",
        "TBody",
        "TFoot",
        "Span",
        "Quote",
        "Note",
        "Reference",
        "BibEntry",
        "Code",
        "Link",
        "Annot",
        "Ruby",
        "RB",
        "RT",
        "RP",
        "Warichu",
        "WT",
        "WP",
        "Figure",
        "Formula",
        "Form",
    ]
)
# PDF 2.0 types Chromium writes (Strong, Em) and the others PDF 2.0 added,
# mapped to their PDF 1.7 equivalents. Anything else unknown groups as NonStruct.
PDF2_ROLES = {
    "/Strong": "/Span",
    "/Em": "/Span",
    "/Sub": "/Span",
    "/Title": "/P",
    "/FENote": "/Note",
    "/Aside": "/Div",
    "/DocumentFragment": "/Div",
}


def role_map(roles: set[str], existing: dict[str, str]) -> dict[str, str]:
    """RoleMap entries for every non-standard type, keeping existing mappings."""
    mapping = dict(existing)
    for role in sorted(roles - STANDARD_TYPES):
        mapping.setdefault(role, PDF2_ROLES.get(role, "/NonStruct"))
    return mapping


def map_roles(tree: DictionaryObject) -> int:
    """Role-map every non-standard structure type in the tree; returns how many."""
    roles = {str(node["/S"]) for _, node in _walk(tree.get("/K"), set())}
    current = _resolve(tree.get("/RoleMap"))
    existing = {str(k): str(v) for k, v in current.items()} if current else {}
    mapping = role_map(roles, existing)
    if mapping == existing:
        return 0
    tree[NameObject("/RoleMap")] = DictionaryObject(
        {NameObject(k): NameObject(v) for k, v in mapping.items()}
    )
    return len(mapping) - len(existing)


def finish(writer: PdfWriter, info: dict, properties: dict[str, str]) -> dict[str, int]:
    """Apply every repair to a cloned, tagged document; returns what changed."""
    root = writer._root_object
    tree = _resolve(root.get("/StructTreeRoot"))
    counts = {"artifacts": 0, "list_bodies": 0, "links": 0, "roles": 0}
    if isinstance(tree, DictionaryObject):
        counts["list_bodies"] = wrap_list_bodies(writer, tree)
        counts["roles"] = map_roles(tree)
        for page in writer.pages:
            if "/StructParents" not in page:
                continue
            content = page.get_contents()
            if content is None:
                continue
            operations, wrapped = mark_artifacts(content.operations)
            if wrapped:
                content.operations = operations
                page.replace_contents(content)
                page.compress_content_streams()
                counts["artifacts"] += wrapped
    counts["links"] = describe_links(writer, info.get("names", {}))
    write_metadata(writer, properties, info.get("language"))
    return counts
