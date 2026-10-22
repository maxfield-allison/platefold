"""Finding and editing tokens in PDF object bytes without touching strings.

Joining parts renumbers the references in every copied object and renames
element IDs, working on each object's bytes. Text inside a literal string, a
hex string, a name or a comment must never be read as a reference or an ID,
whatever it looks like. These tests check the scanner against the full parser
on hand-written cases and on many generated objects.
"""

import random

import pytest

from platefold.pdfjoin import _ID_FIELDS, JoinError, _remap, _remap_bytes, _string_token
from platefold.pdfobjects import (
    PdfShapeError,
    Raw,
    Ref,
    parse,
    remap_references,
    scan,
    string_bytes,
)


def _shift(num: int) -> int:
    return num + 1000


# ------------------------------------------------------------------ examples


@pytest.mark.parametrize(
    ("head", "expected"),
    [
        (b"<</Alt (Literal 1 0 R)\n/P 2 0 R>>", b"<</Alt (Literal 1 0 R)\n/P 1002 0 R>>"),
        # Escaped and nested parentheses keep the string open.
        (b"<</Alt (a \\) 3 0 R \\( b)/K 4 0 R>>", b"<</Alt (a \\) 3 0 R \\( b)/K 1004 0 R>>"),
        (b"<</Alt (x (5 0 R) (6 0 R (7 0 R)) y) /K 8 0 R>>", None),
        (b"<</Alt (\\\\) /K 8 0 R>>", b"<</Alt (\\\\) /K 1008 0 R>>"),
        (b"<</Alt (50% 9 0 R) /K [1 0 R]>>", b"<</Alt (50% 9 0 R) /K [1001 0 R]>>"),
        (
            b"<</ActualText <31 20 30 20 52> /K 2 0 R>>",
            b"<</ActualText <31 20 30 20 52> /K 1002 0 R>>",
        ),
        # A comment holds no references, but may sit between a reference's tokens.
        (b"<</K 3 %4 0 R\n0 R>>", b"<</K 1003 %4 0 R\n0 R>>"),
        (b"<</K 3 %x\r0 R>>", b"<</K 1003 %x\r0 R>>"),
        # Digits ending a name, a real or a word are not an object number.
        (b"[/1 0 R]", b"[/1 0 R]"),
        (b"[/N1 0 R 1.5 0 R -2 0 R]", b"[/N1 0 R 1.5 0 R -2 0 R]"),
        (b"[1 0 RG 2 0 R]", b"[1 0 RG 1002 0 R]"),
        (b"[(a)3 0 R<ab>4 0 R]", b"[(a)1003 0 R<ab>1004 0 R]"),
        (b"12 0 R", b"1012 0 R"),
        (b"<<>>", b"<<>>"),
    ],
)
def test_only_reference_tokens_are_renumbered(head, expected):
    if expected is None:
        expected = head.replace(b"/K 8 0 R", b"/K 1008 0 R")
    assert remap_references(head, _shift) == expected
    assert parse(remap_references(head, _shift)) == _remap(parse(head), _shift)


def test_an_unterminated_string_is_refused():
    with pytest.raises(PdfShapeError):
        scan(b"<</Alt (never closed>>")


def test_ids_are_renamed_only_in_id_fields():
    head = (
        b"<</Type /StructElem /ID (node1)\n/Alt (node1)\n/ActualText <6E6F646531>"
        b"\n/A [<</O /Table /Headers [(node1) (node2)]>> <</O /Layout /T (node2)>>]"
        b"\n/K [(node1)] /P 5 0 R>>"
    )
    ids = {b"node1": b"p1-node1", b"node2": b"p1-node2"}

    result = parse(_remap_bytes(head, _shift, ids))

    assert result[b"/ID"] == Raw(b"(p1-node1)")
    assert result[b"/Alt"] == Raw(b"(node1)")
    assert string_bytes(result[b"/ActualText"]) == b"node1"
    assert result[b"/A"][0][b"/Headers"] == [Raw(b"(p1-node1)"), Raw(b"(p1-node2)")]
    assert result[b"/A"][1][b"/T"] == Raw(b"(node2)")
    assert result[b"/K"] == [Raw(b"(node1)")]
    assert result[b"/P"] == Ref(1005)


def test_structure_parent_keys_are_renumbered_only_as_values():
    head = (
        b"<</Type /Annot /Subtype /Link /Contents (see /StructParent 3 and 1 0 R)"
        b"\n/A <</S /URI /URI (https://example.com/\\(1 0 R\\))>>\n/StructParent 3\n/P 2 0 R>>"
    )
    result = _remap_bytes(head, _shift, {}, {3: 7})
    assert result == head.replace(b"/StructParent 3\n", b"/StructParent 7\n").replace(
        b"/P 2 0 R", b"/P 1002 0 R"
    )
    with pytest.raises(JoinError, match="structure-parent key 3"):
        _remap_bytes(head, _shift, {}, {})


# --------------------------------------------------------------- generated

_SPACES = [b" ", b"\n", b"\r\n", b"\r", b"\t", b"\x0c", b"\x00", b"  "]
_TEXT = [
    b"a",
    b"Z",
    b"7",
    b" ",
    b"R",
    b"%",
    b"/",
    b"<",
    b">>",
    b"[",
    b"]",
    b"1 0 R",
    b" 12 0 R ",
    b"/StructParent 3",
    b"\\(",
    b"\\)",
    b"\\\\",
    b"\\n",
    b"\\053",
    b"\\\n",
    b"\n",
    b"node1",
]
_FIELDS = [b"/ID", b"/Headers", b"/Alt", b"/K", b"/A", b"/StructParent", b"/T"]


class _Generator:
    """Random PDF values, written with random spacing and comments."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)

    def literal(self, depth: int = 0) -> bytes:
        pieces = []
        for _ in range(self.rng.randint(0, 6)):
            if depth < 4 and self.rng.random() < 0.15:
                pieces.append(self.literal(depth + 1))  # balanced, so unescaped
            else:
                pieces.append(self.rng.choice(_TEXT))
        return b"(" + b"".join(pieces) + b")"

    def hex(self) -> bytes:
        digits = [
            self.rng.choice(b"0123456789abcdefABCDEF \n") for _ in range(self.rng.randint(0, 9))
        ]
        return b"<" + bytes(digits) + b">"

    def name(self) -> bytes:
        if self.rng.random() < 0.5:
            return self.rng.choice([*_FIELDS, b"/1", b"/R", b"/0R", b"/a#20b", b"/"])
        return b"/" + bytes(
            self.rng.choice(b"abcXYZ019.-+_") for _ in range(self.rng.randint(1, 5))
        )

    def scalar(self) -> list[bytes]:
        choice = self.rng.random()
        if choice < 0.2:
            return [self.literal()]
        if choice < 0.3:
            return [self.hex()]
        if choice < 0.45:
            return [self.name()]
        if choice < 0.65:
            return [b"%d" % self.rng.randint(0, 99), b"%d" % self.rng.randint(0, 2), b"R"]
        return [self.rng.choice([b"0", b"12", b"-3", b"1.5", b"-.5", b"+4", b"true", b"null"])]

    def value(self, depth: int = 0) -> list[bytes]:
        choice = self.rng.random()
        if depth < 4 and choice < 0.25:
            tokens = [b"<<"]
            keys = {self.rng.choice(_FIELDS) if self.rng.random() < 0.6 else self.name()}
            for key in keys | {self.name() for _ in range(self.rng.randint(0, 4))}:
                tokens += [key, *self.value(depth + 1)]
            return [*tokens, b">>"]
        if depth < 4 and choice < 0.45:
            tokens = [b"["]
            for _ in range(self.rng.randint(0, 5)):
                tokens += self.value(depth + 1)
            return [*tokens, b"]"]
        return self.scalar()

    def write(self, tokens: list[bytes]) -> bytes:
        out = bytearray()
        previous = b""
        for token in tokens:
            # A name ends only where a delimiter or white space does.
            ends_regular = previous[:1] == b"/" or (previous and _regular(previous[-1]))
            if (ends_regular and _regular(token[0])) or self.rng.random() < 0.4:
                out += self.rng.choice(_SPACES)
            if self.rng.random() < 0.08:
                out += b"%" + self.rng.choice([b"", b" 1 0 R", b" (", b" >>"]) + b"\n"
            out += token
            previous = token
        return bytes(out)


def _regular(byte: int) -> bool:
    return byte not in b" \t\r\n\x0c\x00()<>[]{}/%"


def _expected(value, ids, keys, field=None, *, direct=True):
    """What renumbering and renaming should make of a parsed value."""
    if isinstance(value, Ref):
        return Ref(_shift(value.num))
    if isinstance(value, dict):
        return {k: _expected(v, ids, keys, bytes(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_expected(item, ids, keys, field, direct=False) for item in value]
    if isinstance(value, Raw) and value[:1] in (b"(", b"<") and field in _ID_FIELDS:
        renamed = ids.get(string_bytes(value))
        return value if renamed is None else _string_token(renamed)
    if field == b"/StructParent" and direct and type(value) is int and value >= 0:
        return keys[value]
    return value


def _strings(value) -> list[bytes]:
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    if isinstance(value, Raw) and value[:1] in (b"(", b"<"):
        return [bytes(value)]
    return []


@pytest.mark.parametrize("seed", range(40))
def test_generated_objects_edit_as_the_parser_reads_them(seed):
    generator = _Generator(seed)
    ids = {b"node1": b"p9-node1", b"": b"p9-empty", b"a": b"(renamed)"}
    keys = {key: key + 500 for key in range(100)}
    for _ in range(100):
        data = generator.write(generator.value())
        parsed = parse(data)

        # The scanner finds exactly the strings the parser reads, byte for byte.
        syntax = scan(data)
        assert [data[s:e] for s, e in syntax.strings] == _strings(parsed), data
        assert len(syntax.view) == len(data)

        assert parse(remap_references(data, _shift)) == _remap(parsed, _shift), data
        edited = _remap_bytes(data, _shift, ids, keys)
        assert parse(edited) == _expected(parsed, ids, keys), data
