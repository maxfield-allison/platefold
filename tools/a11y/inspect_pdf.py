"""Inspect structural evidence, without claiming PDF/UA or reading-order acceptance."""

import hashlib
from collections import Counter
from pathlib import Path

from pypdf import PdfReader
from pypdf.generic import ArrayObject, DictionaryObject, IndirectObject


def resolve(value):
    return value.get_object() if isinstance(value, IndirectObject) else value


def plain(value):
    value = resolve(value)
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list):
        return [plain(v) for v in value]
    return str(value)


class Structure:
    def __init__(self, reader):
        self.pages = {p.indirect_reference.idnum: i + 1 for i, p in enumerate(reader.pages)}
        self.nodes = []
        self.visited = set()

    def page_number(self, ref):
        return self.pages.get(ref.idnum) if isinstance(ref, IndirectObject) else None

    def node(self, obj, parent_page, depth):
        page = self.page_number(obj.get("/Pg")) or parent_page
        node = {"depth": depth, "page": page}
        for key in ("/S", "/Alt", "/ActualText", "/Lang", "/A", "/MCID", "/Type", "/ID"):
            if key in obj:
                node[key] = plain(obj[key])
        if "/Pg" in obj and self.page_number(obj.get("/Pg")) is None:
            node["invalid_page_reference"] = True
        if "/Obj" in obj:
            target = resolve(obj["/Obj"])
            node["object_subtype"] = str(target.get("/Subtype"))
            node["object_page"] = self.page_number(target.get("/P"))
        self.nodes.append(node)
        if "/K" in obj:
            self.walk(obj["/K"], page, depth + 1)

    def walk(self, ref, parent_page=None, depth=0):
        if isinstance(ref, IndirectObject):
            if ref.idnum in self.visited:
                return
            self.visited.add(ref.idnum)
        obj = resolve(ref)
        if isinstance(obj, ArrayObject):
            for child in obj:
                self.walk(child, parent_page, depth)
        elif isinstance(obj, DictionaryObject):
            self.node(obj, parent_page, depth)
        elif isinstance(obj, int):
            self.nodes.append({"depth": depth, "page": parent_page, "mcid": int(obj)})


def annotations(reader):
    result = []
    for i, page in enumerate(reader.pages, 1):
        for ref in page.get("/Annots", []):
            annotation = resolve(ref)
            result.append(
                {
                    "page": i,
                    "subtype": str(annotation.get("/Subtype")),
                    "struct_parent": annotation.get("/StructParent"),
                    "uri": str(resolve(annotation.get("/A", {})).get("/URI", "")),
                }
            )
    return result


class _ParentTreeCheck:
    def __init__(self, tree):
        self.parents = {}
        self.errors = []
        self.numbers(tree.get("/ParentTree", {}))
        self.walk(tree)

    def numbers(self, ref):
        node = resolve(ref)
        entries = node.get("/Nums", [])
        self.parents.update(zip(entries[::2], entries[1::2], strict=True))
        for child in node.get("/Kids", []):
            self.numbers(child)

    @staticmethod
    def same(left, right):
        left = (
            left if isinstance(left, IndirectObject) else getattr(left, "indirect_reference", None)
        )
        right = (
            right
            if isinstance(right, IndirectObject)
            else getattr(right, "indirect_reference", None)
        )
        return (
            isinstance(left, IndirectObject)
            and isinstance(right, IndirectObject)
            and left.idnum == right.idnum
            and left.generation == right.generation
        )

    def marked(self, index, page, owner, stream=None):
        container = resolve(stream or page)
        key = container.get("/StructParents") if isinstance(container, dict) else None
        array = resolve(self.parents.get(key))
        if (
            not isinstance(array, list)
            or not 0 <= index < len(array)
            or not self.same(array[index], owner)
        ):
            self.errors.append(f"MCID {index} has no matching parent for StructParents {key}")

    def walk(self, ref, page=None, owner=None):
        node = resolve(ref)
        if isinstance(node, list):
            for child in node:
                self.walk(child, page, owner)
        elif isinstance(node, int):
            self.marked(node, page, owner)
        elif isinstance(node, dict):
            self.element(ref, node, page, owner)

    def element(self, ref, node, page, owner):
        page = node.get("/Pg", page)
        if node.get("/Type") == "/MCR":
            self.marked(node["/MCID"], page, owner, node.get("/Stm"))
        elif node.get("/Type") == "/OBJR":
            annotation = resolve(node["/Obj"])
            if not self.same(self.parents.get(annotation.get("/StructParent")), owner):
                self.errors.append("Annotation has no matching structural parent")
        elif "/K" in node:
            self.walk(node.get("/K"), page, ref if node.get("/Type") == "/StructElem" else owner)


def parent_tree_errors(tree):
    """Check retained tree-to-content/annotation backlinks in trusted fixtures."""
    return _ParentTreeCheck(tree).errors if tree else ["Missing structure tree"]


def inspect(path: Path) -> dict:
    with PdfReader(path) as reader:
        root = reader.root_object
        structure = Structure(reader)
        tree = resolve(root.get("/StructTreeRoot"))
        if tree:
            structure.walk(tree)
        nodes = structure.nodes
        text = [p.extract_text() or "" for p in reader.pages]
        combined = "\n".join(text)
        return {
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
            "pages": len(reader.pages),
            "page_sizes_pt": [list(map(float, p.mediabox[2:])) for p in reader.pages],
            "catalog_keys": sorted(map(str, root.keys())),
            "marked": plain(root.get("/MarkInfo")),
            "language": str(root.get("/Lang", "")),
            "structure_tree": tree is not None,
            "parent_tree": bool(tree and "/ParentTree" in tree),
            "parent_tree_errors": parent_tree_errors(tree),
            "roles": dict(Counter(n["/S"] for n in nodes if "/S" in n)),
            "alternatives": [n["/Alt"] for n in nodes if "/Alt" in n],
            "nodes": nodes,
            "annotations": annotations(reader),
            "page_struct_parents": [p.get("/StructParents") for p in reader.pages],
            "page_tabs": [str(p.get("/Tabs", "")) for p in reader.pages],
            "text": text,
            "sentinel_offsets": {
                s: combined.find(s) for s in ("ALPHA", "BRAVO", "CHARLIE", "DELTA")
            },
            "outline_top_level": len(reader.outline),
        }
