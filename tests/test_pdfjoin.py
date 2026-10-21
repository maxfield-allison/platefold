"""Joining parts: pages, structure tree, parent tree, IDs and destinations.

The parts here are built in the shape Chromium writes: a flat page tree, a
classic cross-reference table and a structure tree that mirrors the DOM, with
a table whose header row repeats on every page as the same TH elements.
"""

import re
import zlib
from pathlib import Path

import pytest

from platefold import pdfjoin
from platefold.pdfobjects import (
    Name,
    PdfFile,
    Raw,
    Ref,
    Writer,
    kids,
    parse,
    serialize,
    string_bytes,
)


def _elem(role: bytes, parent: int, kids_value, page: Ref | None = None, **extra) -> bytes:
    node = {
        Name(b"/Type"): Name(b"/StructElem"),
        Name(b"/S"): Name(role),
        Name(b"/P"): Ref(parent),
        Name(b"/K"): kids_value,
    }
    if page is not None:
        node[Name(b"/Pg")] = page
    node.update({Name(b"/" + key.encode()): value for key, value in extra.items()})
    return serialize(node)


class _Builder:
    """Write one part: a table printed over pages, as Chromium tags it."""

    def __init__(self, path: Path, pages: list[list[int]], *, header_id: str, **options):
        self.objects: dict[int, tuple[bytes, bytes | None]] = {}
        self.next = 1
        self.header_id = header_id
        self.link_to = options.get("link_to")
        # Authored strings: a figure's alternative text and the link's description.
        self.alt: bytes | None = options.get("alt")
        self.link_text: bytes = options.get("link_text", b"")
        info = self.put(b"<</Title (part)\n/Creator (Chromium)>>")
        self.page_tree, self.tree, self.document, self.table = (self.reserve() for _ in range(4))
        self.header_row, self.th, self.th_text = (self.reserve() for _ in range(3))
        self.page_refs = [Ref(self.reserve()) for _ in pages]
        self.parent_arrays: dict[int, list] = {}
        self.rows: list[Ref] = []
        self.link: int | None = None
        for index, printed in enumerate(pages):
            self._page(index, printed)
        self._header()
        root = self.put(
            serialize(self._catalog(options.get("dest"), options.get("sect", False), options))
        )
        with path.open("wb") as handle:
            writer = Writer(handle, self.next)
            for num in sorted(self.objects):
                writer.write(num, *self.objects[num])
            writer.finish({Name(b"/Root"): Ref(root), Name(b"/Info"): Ref(info)})

    def reserve(self) -> int:
        num = self.next
        self.next += 1
        return num

    def put(self, head: bytes, stream: bytes | None = None) -> int:
        num = self.reserve()
        self.objects[num] = (head, stream)
        return num

    def _page(self, index: int, printed: list[int]) -> None:
        page = self.page_refs[index]
        ops = [b"/TH <</MCID 0 >>BDC (H) Tj EMC"]
        owners = [Ref(self.th_text)]
        annots = b""
        if index == 0 and self.link_to:
            self.link = self.reserve()
            ops.append(b"/Link <</MCID 1 >>BDC (see) Tj EMC")
            owners.append(Ref(self.link))
            self.annotation = self.put(
                b"<</Type /Annot\n/Subtype /Link\n/Rect [0 0 10 10]\n/Dest /%s%s\n/StructParent %d>>"
                % (self.link_to.encode(), self.link_text, len(self.page_refs))
            )
            annots = b"\n/Annots [%d 0 R]" % self.annotation
        for row in printed:
            mcid = len(owners)
            tr, td, text = self.reserve(), self.reserve(), self.reserve()
            self.objects[text] = (_elem(b"/NonStruct", td, mcid, page), None)
            self.objects[td] = (
                b"<</Type /StructElem\n/S /TD\n/P %d 0 R\n/K %d 0 R\n/A [<</O /Table\n/Headers [(%s)]>>]>>"
                % (tr, text, self.header_id.encode()),
                None,
            )
            self.objects[tr] = (_elem(b"/TR", self.table, [Ref(td)], page), None)
            ops.append(b"/TD <</MCID %d >>BDC (r%d) Tj EMC" % (mcid, row))
            owners.append(Ref(text))
            self.rows.append(Ref(tr))
        self.parent_arrays[index] = owners
        content = zlib.compress(b"\n".join(ops))
        stream = self.put(b"<</Filter /FlateDecode\n/Length %d>>" % len(content), content)
        self.objects[page.num] = (
            b"<</Type /Page\n/Resources <</ProcSet [/PDF /Text]>>\n/MediaBox [0 0 612 792]%s"
            b"\n/Contents %d 0 R\n/StructParents %d\n/Parent %d 0 R>>"
            % (annots, stream, index, self.page_tree),
            None,
        )

    def _header(self) -> None:
        # The header cell's content: a bare MCID on its own page, MCRs elsewhere.
        mcrs = [
            {Name(b"/Type"): Name(b"/MCR"), Name(b"/Pg"): page, Name(b"/MCID"): 0}
            for page in self.page_refs[1:]
        ]
        self.objects[self.th_text] = (
            _elem(b"/NonStruct", self.th, [0, *mcrs], self.page_refs[0]),
            None,
        )
        self.objects[self.th] = (
            b"<</Type /StructElem\n/S /TH\n/P %d 0 R\n/K %d 0 R\n/ID (%s)>>"
            % (self.header_row, self.th_text, self.header_id.encode()),
            None,
        )
        self.objects[self.header_row] = (_elem(b"/TR", self.table, [Ref(self.th)]), None)

    def _catalog(self, dest, sect: bool, options: dict) -> dict:
        parent = self.document
        if sect:
            # The table one level deeper, inside a section.
            parent = self.reserve()
            self.objects[parent] = (_elem(b"/Sect", self.document, [Ref(self.table)]), None)
        self.objects[self.table] = (
            _elem(b"/Table", parent, [Ref(self.header_row), *self.rows]),
            None,
        )
        top = [Ref(parent if sect else self.table)]
        if self.alt is not None:
            # A part followed by a continuation ends with its table, and a
            # continuation opens with it, so the figure goes on the other side.
            figure = self.put(
                b"<</Type /StructElem\n/S /Figure\n/Alt %s\n/P %d 0 R>>" % (self.alt, self.document)
            )
            top.insert(0 if options.get("alt_first") else len(top), Ref(figure))
        numbers: list = []
        for index, owners in self.parent_arrays.items():
            numbers += [index, Ref(self.put(serialize(owners)))]
        if self.link:
            objr = {
                Name(b"/Type"): Name(b"/OBJR"),
                Name(b"/Obj"): Ref(self.annotation),
                Name(b"/Pg"): self.page_refs[0],
            }
            self.objects[self.link] = (
                _elem(b"/Link", self.document, [1, objr], self.page_refs[0]),
                None,
            )
            top.insert(0, Ref(self.link))
            numbers += [len(self.page_refs), Ref(self.link)]
        self.objects[self.document] = (_elem(b"/Document", self.tree, top), None)
        parent_tree = self.put(
            serialize({Name(b"/Type"): Name(b"/ParentTree"), Name(b"/Nums"): numbers})
        )
        name = self.header_id.encode()
        leaf = self.put(
            b"<</Limits [(%s) (%s)]\n/Names [(%s) %d 0 R]>>" % (name, name, name, self.th)
        )
        id_tree = self.put(b"<</Kids [%d 0 R]>>" % leaf)
        self.objects[self.tree] = (
            b"<</Type /StructTreeRoot\n/K %d 0 R\n/ParentTreeNextKey %d\n/ParentTree %d 0 R"
            b"\n/IDTree %d 0 R>>" % (self.document, len(numbers) // 2, parent_tree, id_tree),
            None,
        )
        self.objects[self.page_tree] = (
            serialize(
                {
                    Name(b"/Type"): Name(b"/Pages"),
                    Name(b"/Count"): len(self.page_refs),
                    Name(b"/Kids"): self.page_refs,
                }
            ),
            None,
        )
        catalog: dict = {
            Name(b"/Type"): Name(b"/Catalog"),
            Name(b"/Pages"): Ref(self.page_tree),
            Name(b"/MarkInfo"): {
                Name(b"/Type"): Name(b"/MarkInfo"),
                Name(b"/Marked"): Raw(b"true"),
            },
            Name(b"/StructTreeRoot"): Ref(self.tree),
            Name(b"/Lang"): Raw(b"(en)"),
        }
        if dest:
            name, page_index = dest
            target = self.page_refs[page_index].num
            catalog[Name(b"/Dests")] = Ref(
                self.put(b"<</%s [%d 0 R /XYZ 0 700 0]>>" % (name.encode(), target))
            )
        return catalog


def _rows_of(pdf: PdfFile) -> list[tuple[int, int]]:
    """(page index, row number) for each body row, in structure order."""
    pages = {num: index for index, num in enumerate(pdf.pages())}
    tree = pdf.get(pdf.root[b"/StructTreeRoot"])
    document = pdf.get(tree[b"/K"])
    table = next(pdf.get(k) for k in kids(document[b"/K"]) if pdf.get(k)[b"/S"] == Name(b"/Table"))
    found = []
    for ref in kids(table[b"/K"])[1:]:
        tr = pdf.get(ref)
        text = pdf.get(pdf.get(kids(tr[b"/K"])[0])[b"/K"])
        page = pages[text[b"/Pg"].num]
        content = zlib.decompress(pdf.raw(pdf.get(Ref(pdf.pages()[page]))[b"/Contents"].num).stream)
        match = re.search(rb"/TD <</MCID %d >>BDC \(r(\d+)\)" % text[b"/K"], content)
        found.append((page, int(match.group(1))))
    return found


def _references(pdf: PdfFile) -> set[int]:
    targets: set[int] = set()

    def walk(value) -> None:
        if isinstance(value, Ref):
            targets.add(value.num)
        elif isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for num in pdf.order:
        walk(parse(pdf.raw(num).head))
    return targets


@pytest.fixture
def two_parts(tmp_path):
    # Part one printed rows 0-9; its last page (rows 8-9) is printed again by
    # part two, which starts with row 7 on a page of its own, then breaks.
    first = tmp_path / "first.pdf"
    _Builder(
        first,
        [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]],
        header_id="node1",
        link_to="later",
        dest=("later", 0),
    )
    second = tmp_path / "second.pdf"
    _Builder(second, [[7], [8, 9, 10, 11], [12, 13, 14, 15]], header_id="node9", dest=("later", 2))
    return [
        pdfjoin.Part(str(first), keep=2),
        pdfjoin.Part(str(second), first=1, continues=True, header_rows=1, stand_ins=frozenset()),
    ], tmp_path


def test_joined_pages_keep_each_row_once_in_order(two_parts):
    parts, tmp_path = two_parts
    parts[0].stand_ins = frozenset({"later"})
    out = tmp_path / "joined.pdf"

    count = pdfjoin.join(parts, out)

    pdf = PdfFile(out)
    assert count == len(pdf.pages()) == 4
    assert _rows_of(pdf) == [
        (0, 0),
        (0, 1),
        (0, 2),
        (0, 3),
        (1, 4),
        (1, 5),
        (1, 6),
        (1, 7),
        (2, 8),
        (2, 9),
        (2, 10),
        (2, 11),
        (3, 12),
        (3, 13),
        (3, 14),
        (3, 15),
    ]
    # Every reference resolves: nothing points at a dropped page or row.
    assert _references(pdf) <= set(pdf.offsets)


def test_the_repeated_header_is_one_element_with_content_on_every_page(two_parts):
    parts, tmp_path = two_parts
    out = tmp_path / "joined.pdf"
    pdfjoin.join(parts, out)
    pdf = PdfFile(out)
    pages = {num: index for index, num in enumerate(pdf.pages())}
    tree = pdf.get(pdf.root[b"/StructTreeRoot"])
    table = pdf.get(kids(pdf.get(tree[b"/K"])[b"/K"])[-1])
    header = pdf.get(kids(table[b"/K"])[0])
    th = pdf.get(kids(header[b"/K"])[0])
    text = pdf.get(th[b"/K"])

    places = [
        (pages[text[b"/Pg"].num], item)
        if isinstance(item, int)
        else (pages[item[b"/Pg"].num], item[b"/MCID"])
        for item in kids(text[b"/K"])
    ]
    assert places == [(0, 0), (1, 0), (2, 0), (3, 0)]
    # One ID, and every data cell's header points to it.
    assert string_bytes(th[b"/ID"]) == b"node1"
    headers = set(re.findall(rb"/Headers \[\(([^)]*)\)\]", out.read_bytes()))
    assert headers == {b"node1"}


def test_parent_tree_keys_are_contiguous_and_point_at_the_marked_content(two_parts):
    parts, tmp_path = two_parts
    out = tmp_path / "joined.pdf"
    pdfjoin.join(parts, out)
    pdf = PdfFile(out)
    tree = pdf.get(pdf.root[b"/StructTreeRoot"])
    entries = dict(pdf.number_tree(tree[b"/ParentTree"]))

    keys = [pdf.get(Ref(num))[b"/StructParents"] for num in pdf.pages()]
    assert keys == sorted(keys)
    # Four pages and the link annotation, numbered without the dropped pages' gaps.
    assert sorted(entries) == [0, 1, 2, 3, 4]
    assert tree[b"/ParentTreeNextKey"] == 5
    for index, num in enumerate(pdf.pages()):
        owners = pdf.get(entries[keys[index]])
        content = zlib.decompress(pdf.raw(pdf.get(Ref(num))[b"/Contents"].num).stream)
        for mcid in map(int, re.findall(rb"MCID (\d+)", content)):
            owner = pdf.get(owners[mcid])
            assert owner[b"/Pg"].num == num or any(
                isinstance(k, dict) and k.get(b"/Pg") == Ref(num) and k.get(b"/MCID") == mcid
                for k in kids(owner[b"/K"])
            )


def test_a_stand_in_destination_yields_to_the_real_one(two_parts):
    parts, tmp_path = two_parts
    parts[0].stand_ins = frozenset({"later"})
    out = tmp_path / "joined.pdf"
    pdfjoin.join(parts, out)
    pdf = PdfFile(out)
    dests = pdf.get(pdf.root[b"/Dests"])
    assert dests[b"/later"][0] == Ref(pdf.pages()[3])
    annotation = pdf.get(kids(pdf.get(Ref(pdf.pages()[0]))[b"/Annots"])[0])
    assert annotation[b"/Dest"] == Name(b"/later")
    entries = dict(pdf.number_tree(pdf.get(pdf.root[b"/StructTreeRoot"])[b"/ParentTree"]))
    assert pdf.get(entries[annotation[b"/StructParent"]])[b"/S"] == Name(b"/Link")


def test_the_running_header_pass_draws_over_each_page(two_parts, tmp_path):
    parts, _ = two_parts
    overlay = tmp_path / "running.pdf"
    _Builder(overlay, [[], [], [], []], header_id="x")
    out = tmp_path / "joined.pdf"
    pdfjoin.join(parts, out, overlay=overlay)
    pdf = PdfFile(out)
    for num in pdf.pages():
        page = pdf.get(Ref(num))
        streams = kids(page[b"/Contents"])
        assert len(streams) == 3
        assert pdf.raw(streams[-1].num).stream.endswith(b"/PfRunning Do\n")
        form = pdf.get(page[b"/Resources"][b"/XObject"][b"/PfRunning"])
        assert form[b"/Subtype"] == Name(b"/Form")
    assert _references(pdf) <= set(pdf.offsets)


def test_restart_point_finds_the_row_that_begins_the_last_page(tmp_path):
    path = tmp_path / "part.pdf"
    _Builder(path, [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]], header_id="n")

    assert pdfjoin.restart_point(path, 1, [True] * 10) == (8, 2)
    # A cell spanning into row 8 moves the restart back a page.
    assert pdfjoin.restart_point(path, 1, [True] * 8 + [False, True]) == (4, 1)


def test_the_continuation_page_is_where_the_first_kept_row_begins(tmp_path):
    path = tmp_path / "part.pdf"
    _Builder(path, [[7], [8, 9]], header_id="n")
    assert pdfjoin.continuation_page(path, 1) == 1


def test_a_continuation_that_does_not_match_the_open_table_is_refused(two_parts):
    parts, tmp_path = two_parts
    other = tmp_path / "other.pdf"
    _Builder(other, [[7], [8, 9, 10, 11]], header_id="n", sect=True)
    parts[1] = pdfjoin.Part(str(other), first=1, continues=True, header_rows=1)
    with pytest.raises(pdfjoin.JoinError, match="does not match the open table"):
        pdfjoin.join(parts, tmp_path / "joined.pdf")


# Strings that read like references or IDs, as an author could write them.
_FIRST_ALT = b"(Literal 1 0 R and \\) 2 0 R \\( and (nested 3 0 R))"
_LAST_ALT = b"(node9)"
_LINK_TEXT = b"\n/Contents (see /StructParent 0 and 4 0 R)"


def test_strings_that_read_like_references_or_ids_survive_the_join(tmp_path):
    first = tmp_path / "first.pdf"
    _Builder(
        first,
        [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9]],
        header_id="node1",
        link_to="later",
        link_text=_LINK_TEXT,
        alt=_FIRST_ALT,
        alt_first=True,
    )
    second = tmp_path / "second.pdf"
    # "node9" is also this part's header ID, which the join renames to "node1".
    _Builder(second, [[7], [8, 9, 10, 11]], header_id="node9", alt=_LAST_ALT)
    parts = [
        pdfjoin.Part(str(first), keep=2),
        pdfjoin.Part(str(second), first=1, continues=True, header_rows=1),
    ]
    out = tmp_path / "joined.pdf"
    pdfjoin.join(parts, out)

    pdf = PdfFile(out)
    alternatives = [
        pdf.raw(num).head.split(b"/Alt ", 1)[1].split(b"\n/P ", 1)[0]
        for num in pdf.order
        if b"/Alt " in pdf.raw(num).head
    ]
    assert alternatives == [_FIRST_ALT, _LAST_ALT]
    annotation = pdf.get(kids(pdf.get(Ref(pdf.pages()[0]))[b"/Annots"])[0])
    assert annotation[b"/Contents"] == Raw(b"(see /StructParent 0 and 4 0 R)")
    # The link keeps its own structure-parent key, and the IDs that are IDs are renamed.
    entries = dict(pdf.number_tree(pdf.get(pdf.root[b"/StructTreeRoot"])[b"/ParentTree"]))
    assert pdf.get(entries[annotation[b"/StructParent"]])[b"/S"] == Name(b"/Link")
    headers = set(re.findall(rb"/Headers \[\(([^)]*)\)\]", out.read_bytes()))
    assert headers == {b"node1"}
    assert _references(pdf) <= set(pdf.offsets)
