"""Read and write Chromium's PDF output one object at a time.

General PDF libraries load every object of a file into memory, and a tagged
table carries several objects per cell, so finishing a large export that way
cost more time and memory than printing it. Chromium's output has a simple,
regular shape: numbered objects listed in one classic cross-reference table,
dictionaries without compression, and page content in Flate streams. This
module reads that shape by object number, parses only the dictionaries and
arrays a caller asks for, and writes a new file by copying every other
object's bytes unchanged.

Anything outside that shape raises ``PdfShapeError``; callers then fall back
to a general library.
"""

from __future__ import annotations

import re
from array import array
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO


class PdfShapeError(ValueError):
    """The file is not in the shape this module reads."""


class Name(bytes):
    """A PDF name, stored with its leading slash."""


class Raw(bytes):
    """A token written back unchanged: reals, booleans, null and strings."""


@dataclass(frozen=True)
class Ref:
    num: int
    gen: int = 0


_WHITESPACE = b" \t\r\n\f\x00"
_TOKEN_END = re.compile(rb"[ \t\r\n\f\x00()<>\[\]{}/%]")
_LINE_END = re.compile(rb"[\r\n]")


class _Lexer:
    """Parse the PDF object syntax of dictionaries and arrays."""

    def __init__(self, data: bytes, pos: int = 0):
        self.data = data
        self.pos = pos

    def _skip(self) -> None:
        data, pos, end = self.data, self.pos, len(self.data)
        while pos < end:
            byte = data[pos]
            if byte in _WHITESPACE:
                pos += 1
            elif byte == 0x25:  # a comment runs to the end of its line
                newline = _LINE_END.search(data, pos)
                pos = end if newline is None else newline.start()
            else:
                break
        self.pos = pos

    def value(self) -> Any:
        self._skip()
        data, pos = self.data, self.pos
        if pos >= len(data):
            raise PdfShapeError("unexpected end of object")
        byte = data[pos : pos + 1]
        if byte == b"<":
            if data[pos + 1 : pos + 2] == b"<":
                return self._dictionary()
            end = data.index(b">", pos)
            self.pos = end + 1
            return Raw(data[pos : end + 1])
        if byte == b"[":
            return self._array()
        if byte == b"(":
            end = string_end(data, pos)
            self.pos = end
            return Raw(data[pos:end])
        if byte == b"/":
            match = _TOKEN_END.search(data, pos + 1)
            end = match.start() if match else len(data)
            self.pos = end
            return Name(data[pos:end])
        match = _TOKEN_END.search(data, pos)
        end = match.start() if match else len(data)
        token = data[pos:end]
        if not token:
            raise PdfShapeError("unexpected delimiter %r" % byte)
        self.pos = end
        if token.isdigit():
            return self._integer_or_reference(token)
        return Raw(token)

    def _dictionary(self) -> dict:
        self.pos += 2
        result: dict = {}
        while True:
            self._skip()
            if self.data[self.pos : self.pos + 2] == b">>":
                self.pos += 2
                return result
            key = self.value()
            if not isinstance(key, Name):
                raise PdfShapeError("dictionary key is not a name")
            result[key] = self.value()

    def _array(self) -> list:
        self.pos += 1
        items = []
        while True:
            self._skip()
            if self.data[self.pos : self.pos + 1] == b"]":
                self.pos += 1
                return items
            items.append(self.value())

    def _integer_or_reference(self, token: bytes) -> Any:
        data = self.data
        save = self.pos
        self._skip()
        match = _TOKEN_END.search(data, self.pos)
        generation_end = match.start() if match else len(data)
        generation = data[self.pos : generation_end]
        if generation.isdigit():
            self.pos = generation_end
            self._skip()
            after = self.pos + 1
            if data[self.pos : after] == b"R" and (
                after == len(data) or _TOKEN_END.match(data, after)
            ):
                self.pos = after
                return Ref(int(token), int(generation))
        self.pos = save
        return int(token)


def string_end(data: bytes, pos: int) -> int:
    """The offset just past the literal string that starts at ``pos``."""
    close = data.find(b")", pos + 1)
    if close > 0 and data.find(b"(", pos + 1, close) < 0 and data.find(b"\\", pos + 1, close) < 0:
        return close + 1  # no nesting or escape: the first ")" closes it
    depth = 0
    index = pos
    end = len(data)
    while index < end:
        byte = data[index]
        if byte == 0x5C:  # a backslash escapes the next byte
            index += 2
            continue
        if byte == 0x28:
            depth += 1
        elif byte == 0x29:
            depth -= 1
            if depth == 0:
                return index + 1
        index += 1
    raise PdfShapeError("unterminated string")


# ---------------------------------------------------------------- scanning

_REGULAR = rb"[^ \t\r\n\f\x00()<>\[\]{}/%]"
_SPACE = rb"[ \t\r\n\f\x00]"
# Where text stops being syntax: a literal string, a hex string or a comment.
_OPAQUE = re.compile(rb"[(%]|<(?<!<<)(?!<)")
# A reference: an object number and a generation that are whole tokens,
# followed by the keyword R. The first digit must not continue a name, a
# real or a word; testing that after the digit lets the search skip ahead.
_REFERENCE = re.compile(
    rb"(\d(?<!"
    + _REGULAR
    + rb"\d)(?<!/\d)\d*)"
    + _SPACE
    + rb"+(\d+)"
    + _SPACE
    + rb"+R(?!"
    + _REGULAR
    + rb")"
)
_CONTAINER = re.compile(rb"<<|>>|\[|\]")
_NAME_AND_SPACE = re.compile(rb"(/" + _REGULAR + rb"*)" + _SPACE + rb"*")


@dataclass
class Syntax:
    """One object's bytes as syntax alone, for finding and editing tokens in place.

    ``view`` is the bytes with every literal and hex string filled with "("
    and every comment with spaces; it has the same length, so offsets in it
    are offsets in the bytes. Nothing inside a string or a comment is left
    to read as a reference, a name or a bracket, and a string still ends
    the token before it. ``strings`` are the spans of the strings.
    """

    view: bytes
    strings: list[tuple[int, int]]

    def references(self, mapping: Callable[[int], int]) -> list[tuple[int, int, bytes]]:
        """Edits (see ``splice``) renumbering each indirect reference through ``mapping``.

        The generation becomes 0, as the written file numbers every object.
        """
        return _reference_edits(self.view, mapping)

    def field(self, pos: int, *, arrays: bool = True) -> bytes | None:
        """The key of the dictionary entry whose value is, or holds, the token at ``pos``.

        An array's items belong to the entry holding the array, so each
        string of ``/Headers [(a) (b)]`` has the field ``/Headers``; without
        ``arrays`` they have none. None at the top level.
        """
        depth = 0
        for match in reversed(list(_CONTAINER.finditer(self.view, 0, pos))):
            token = match.group()
            if token == b">>" or token == b"]":
                depth += 1
            elif depth:
                depth -= 1
            elif token == b"[":
                if not arrays:
                    return None
                pos = match.start()  # the field of the array itself
            else:
                # In a dictionary a value directly follows its key, and a
                # name holds no "/" after its first.
                slash = self.view.rfind(b"/", match.end(), pos)
                name = _NAME_AND_SPACE.fullmatch(self.view, slash, pos) if slash >= 0 else None
                return name.group(1) if name is not None else None
        return None


def scan(data: bytes) -> Syntax:
    """Find the strings and comments in one object's bytes (see ``Syntax``)."""
    opaque = _OPAQUE.search(data)
    if opaque is None:
        return Syntax(data, [])
    pieces: list[bytes] = []
    strings: list[tuple[int, int]] = []
    position = 0
    while opaque is not None:
        start = opaque.start()
        opener = data[start]
        if opener == 0x28:  # (
            end = string_end(data, start)
        elif opener == 0x25:  # %, to the end of the line
            newline = _LINE_END.search(data, start)
            end = len(data) if newline is None else newline.start()
        else:
            end = data.find(b">", start) + 1
            if end == 0:
                raise PdfShapeError("unterminated hex string")
        if opener == 0x25:
            pieces += (data[position:start], b" " * (end - start))
        else:
            pieces += (data[position:start], b"(" * (end - start))
            strings.append((start, end))
        position = end
        opaque = _OPAQUE.search(data, position)
    pieces.append(data[position:])
    return Syntax(b"".join(pieces), strings)


def splice(data: bytes, edits: list[tuple[int, int, bytes]]) -> bytes:
    """``data`` with each (start, end) span replaced; the spans must not overlap."""
    if not edits:
        return data
    edits.sort()
    pieces: list[bytes] = []
    position = 0
    for start, end, replacement in edits:
        pieces += (data[position:start], replacement)
        position = end
    pieces.append(data[position:])
    return b"".join(pieces)


def _reference_edits(view: bytes, mapping: Callable[[int], int]) -> list[tuple[int, int, bytes]]:
    edits = []
    for match in _REFERENCE.finditer(view):
        start, end = match.span(1)
        edits.append((start, end, b"%d" % mapping(int(match.group(1)))))
        if match.group(2) != b"0":
            start, end = match.span(2)
            edits.append((start, end, b"0"))
    return edits


def remap_references(data: bytes, mapping: Callable[[int], int]) -> bytes:
    """``data`` with each indirect reference renumbered through ``mapping``.

    Text in strings and comments that reads like a reference stays as it is.
    """
    view = data if _OPAQUE.search(data) is None else scan(data).view
    return splice(data, _reference_edits(view, mapping))


def parse(data: bytes) -> Any:
    """Parse one PDF value, such as an object's dictionary."""
    return _Lexer(data).value()


def serialize(value) -> bytes:
    """Write a parsed value back as PDF syntax."""
    if isinstance(value, Ref):
        return b"%d %d R" % (value.num, value.gen)
    if isinstance(value, (Name, Raw)):
        return bytes(value)
    if isinstance(value, bool):
        return b"true" if value else b"false"
    if isinstance(value, int):
        return b"%d" % value
    if isinstance(value, dict):
        return b"<<" + b"\n".join(bytes(k) + b" " + serialize(v) for k, v in value.items()) + b">>"
    if isinstance(value, list):
        return b"[" + b" ".join(serialize(v) for v in value) + b"]"
    raise TypeError("cannot write %r as PDF" % type(value))


def text_string(value: str) -> Raw:
    """A PDF text string: PDFDocEncoding-safe ASCII literal, else UTF-16BE hex."""
    if value.isascii() and all(32 <= ord(c) < 127 for c in value):
        escaped = re.sub(r"([()\\])", r"\\\1", value)
        return Raw(b"(" + escaped.encode("ascii") + b")")
    return Raw(b"<FEFF" + value.encode("utf-16-be").hex().upper().encode("ascii") + b">")


def string_bytes(raw: bytes) -> bytes:
    """The bytes a literal or hex string token stands for."""
    if raw.startswith(b"<"):
        digits = re.sub(rb"\s", b"", raw[1:-1])
        if len(digits) % 2:
            digits += b"0"
        return bytes.fromhex(digits.decode("ascii"))
    if b"\\" not in raw:
        return raw[1:-1]
    escapes = {b"n": b"\n", b"r": b"\r", b"t": b"\t", b"b": b"\b", b"f": b"\f"}

    def replace(match: re.Match[bytes]) -> bytes:
        code: bytes = match.group(1)
        if code[:1].isdigit():
            return bytes([int(code, 8) & 0xFF])
        if code in (b"\r\n", b"\n", b"\r"):
            return b""
        return escapes.get(code, code)

    return re.sub(rb"\\([0-7]{1,3}|\r\n|.)", replace, raw[1:-1], flags=re.S)


# ---------------------------------------------------------------- reading


class _Offsets(Mapping[int, int]):
    """Object number -> byte offset, stored in an array (-1 for a free number)."""

    def __init__(self, table: array):
        self._table = table
        self._count = sum(1 for offset in table if offset >= 0)

    def __getitem__(self, num: int) -> int:
        if 0 <= num < len(self._table) and self._table[num] >= 0:
            return self._table[num]
        raise KeyError(num)

    def __contains__(self, num: object) -> bool:
        return isinstance(num, int) and 0 <= num < len(self._table) and self._table[num] >= 0

    def __iter__(self) -> Iterator[int]:
        return (num for num, offset in enumerate(self._table) if offset >= 0)

    def __len__(self) -> int:
        return self._count


_OBJ_HEADER = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")
_STREAM = re.compile(rb">>\s*stream\r?\n")


@dataclass
class RawObject:
    num: int
    head: bytes  # the object's value, dictionary part of a stream
    stream: bytes | None  # the stream's still-encoded bytes


class PdfFile:
    """Random access to the objects of one Chromium PDF."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        self.data = Path(path).read_bytes()
        self.xref_offset = self._startxref()
        table, self.trailer = self._xref()
        if b"/Prev" in self.trailer or b"/Encrypt" in self.trailer:
            raise PdfShapeError(f"{self.path}: updated or encrypted file")
        # Arrays indexed by object number: a joined table has millions of
        # objects, and dictionaries of them took hundreds of megabytes.
        self.offsets = _Offsets(table)
        self.order = array("q", sorted(self.offsets, key=table.__getitem__))
        self.ends = array("q", [-1]) * len(table)
        for position, num in enumerate(self.order):
            following = position + 1
            self.ends[num] = (
                table[self.order[following]] if following < len(self.order) else self.xref_offset
            )
        self._pages: list[int] | None = None
        self._parsed: dict[int, Any] = {}
        self.page_tree: set[int] = set()  # the intermediate /Pages nodes

    def _startxref(self) -> int:
        match = re.search(rb"startxref\s+(\d+)\s+%%EOF\s*$", self.data[-1024:])
        if match is None:
            raise PdfShapeError(f"{self.path}: no startxref")
        offset = int(match.group(1))
        if self.data[offset : offset + 4] != b"xref":
            raise PdfShapeError(f"{self.path}: not a classic cross-reference table")
        return offset

    def _xref(self) -> tuple[array, dict]:
        data = self.data
        trailer_at = data.index(b"trailer", self.xref_offset)
        offsets = array("q")
        lines = iter(data[self.xref_offset + 4 : trailer_at].split(b"\n"))
        for line in lines:
            header = line.split()
            if not header:
                continue
            if len(header) != 2:
                raise PdfShapeError(f"{self.path}: malformed cross-reference section")
            first, count = int(header[0]), int(header[1])
            if len(offsets) < first + count:
                offsets.extend([-1] * (first + count - len(offsets)))
            for index in range(count):
                entry = next(lines).split()
                if len(entry) != 3:
                    raise PdfShapeError(f"{self.path}: malformed cross-reference entry")
                if entry[2] == b"n":
                    offsets[first + index] = int(entry[0])
        trailer = _Lexer(data, trailer_at + len(b"trailer")).value()
        if not isinstance(trailer, dict):
            raise PdfShapeError(f"{self.path}: malformed trailer")
        return offsets, trailer

    def span(self, num: int) -> bytes:
        """The object's bytes exactly as written, "N G obj" through "endobj"."""
        start = self.offsets[num]
        data = self.data[start : self.ends[num]]
        close = data.rfind(b"endobj")
        if close < 0:
            raise PdfShapeError(f"{self.path}: object {num} has no endobj")
        return data[: close + len(b"endobj")]

    def raw(self, num: int) -> RawObject:
        data = self.data
        start = self.offsets[num]
        match = _OBJ_HEADER.match(data, start)
        if match is None or int(match.group(1)) != num:
            raise PdfShapeError(f"{self.path}: object {num} is not at its offset")
        body = match.end()
        end = self.ends[num]
        stream = _STREAM.search(data, body, end)
        if stream is None:
            text = data[body:end]
            close = text.rfind(b"endobj")
            if close < 0:
                raise PdfShapeError(f"{self.path}: object {num} has no endobj")
            return RawObject(num, text[:close].strip(), None)
        head = data[body : stream.start() + 2].strip()
        length = parse(head).get(b"/Length")
        if not isinstance(length, int):
            raise PdfShapeError(f"{self.path}: object {num} has an indirect length")
        begin = stream.end()
        if not data[begin + length : begin + length + 20].lstrip().startswith(b"endstream"):
            raise PdfShapeError(f"{self.path}: object {num} has the wrong length")
        return RawObject(num, head, data[begin : begin + length])

    def get(self, value: Any) -> Any:
        """The parsed object a reference points at, or the value itself.

        Parsed objects are kept, so walking a structure tree more than once
        parses each element once. Callers must not change what they get.
        """
        if not isinstance(value, Ref):
            return value
        parsed = self._parsed.get(value.num)
        if parsed is None:
            parsed = self._parsed[value.num] = parse(self.raw(value.num).head)
        return parsed

    @property
    def root_ref(self) -> Ref:
        return self.trailer[b"/Root"]

    @property
    def root(self) -> dict:
        return self.get(self.root_ref)

    def pages(self) -> list[int]:
        """Page object numbers in order."""
        if self._pages is None:
            found: list[int] = []
            self._walk_pages(self.root[b"/Pages"], found, 0)
            self._pages = found
        return self._pages

    def _walk_pages(self, ref, found: list[int], depth: int) -> None:
        if depth > 64:
            raise PdfShapeError(f"{self.path}: page tree too deep")
        node = self.get(ref)
        if node.get(b"/Type") != Name(b"/Pages"):
            found.append(ref.num)
            return
        self.page_tree.add(ref.num)
        for key in (b"/Resources", b"/MediaBox", b"/CropBox", b"/Rotate"):
            if key in node:
                raise PdfShapeError(f"{self.path}: inherited page attributes")
        for kid in node[b"/Kids"]:
            self._walk_pages(kid, found, depth + 1)

    def media_box(self, page: int) -> tuple[float, float]:
        box = self.get(Ref(page))[b"/MediaBox"]
        x0, y0, x1, y1 = (float(bytes(v)) if isinstance(v, Raw) else float(v) for v in box)
        return x1 - x0, y1 - y0

    def number_tree(self, ref) -> Iterator[tuple[int, object]]:
        node = self.get(ref)
        for kid in node.get(b"/Kids", []):
            yield from self.number_tree(kid)
        numbers = node.get(b"/Nums", [])
        for index in range(0, len(numbers) - 1, 2):
            yield numbers[index], numbers[index + 1]

    def name_tree(self, ref) -> Iterator[tuple[bytes, object]]:
        node = self.get(ref)
        for kid in node.get(b"/Kids", []):
            yield from self.name_tree(kid)
        names = node.get(b"/Names", [])
        for index in range(0, len(names) - 1, 2):
            yield string_bytes(names[index]), names[index + 1]


def kids(value) -> list:
    """An element's /K as a list."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


# ---------------------------------------------------------------- writing


class Writer:
    """Write objects by number, then one classic cross-reference table."""

    def __init__(self, handle: BinaryIO, size: int = 1):
        self.handle = handle
        self.handle.write(b"%PDF-1.4\n%\xd3\xeb\xe9\xe1\n")
        # Offsets by object number, -1 where none was written.
        self.offsets = array("q")
        self.size = size

    def allocate(self) -> int:
        num = self.size
        self.size += 1
        return num

    def reserve(self, count: int) -> int:
        first = self.size
        self.size += count
        return first

    def _record(self, num: int) -> None:
        if num >= len(self.offsets):
            self.offsets.extend([-1] * (max(num + 1, self.size) - len(self.offsets)))
        self.offsets[num] = self.handle.tell()

    def written(self, num: int) -> bool:
        return 0 <= num < len(self.offsets) and self.offsets[num] >= 0

    def copy(self, num: int, span: bytes) -> None:
        """Write an object's original bytes; its number must be unchanged."""
        self._record(num)
        self.handle.write(span)
        self.handle.write(b"\n")

    def write(self, num: int, head: bytes, stream: bytes | None = None) -> None:
        self._record(num)
        self.handle.write(b"%d 0 obj\n" % num)
        self.handle.write(head)
        if stream is not None:
            self.handle.write(b" stream\n")
            self.handle.write(stream)
            self.handle.write(b"\nendstream")
        self.handle.write(b"\nendobj\n")

    def finish(self, trailer: dict) -> None:
        xref = self.handle.tell()
        self.handle.write(b"xref\n0 %d\n" % self.size)
        self.handle.write(b"0000000000 65535 f\r\n")
        for num in range(1, self.size):
            offset = self.offsets[num] if num < len(self.offsets) else -1
            if offset < 0:
                self.handle.write(b"0000000000 00000 f\r\n")
            else:
                self.handle.write(b"%010d 00000 n\r\n" % offset)
        trailer = dict(trailer)
        trailer[Name(b"/Size")] = self.size
        self.handle.write(b"trailer\n" + serialize(trailer) + b"\nstartxref\n%d\n%%%%EOF\n" % xref)
