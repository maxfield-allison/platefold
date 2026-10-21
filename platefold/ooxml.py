"""Bounded, entity-safe access to Office Open XML packages.

DOCX and XLSX are ZIP containers.  This module is the shared security boundary
for both: it rejects suspicious central directories and paths before ``zipfile``
allocates an entry per member, accounts for actual decompressed bytes, and
parses every XML part with ``defusedxml`` before a format reader sees it.
Validation checks the caller's cancel check between members and before each
64 KiB chunk is decompressed and parsed, so a large or hostile package cannot
hold off Cancel until it has been read.
"""

from __future__ import annotations

import re
import struct
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from xml.etree.ElementTree import TreeBuilder as _TreeBuilder

from defusedxml import ElementTree as DefusedET
from defusedxml.common import DefusedXmlException

from .cancellation import CancelCheck, CancelledError, Throttle, never_cancelled
from .limits import MIB, ResourceLimitError, ResourceLimits
from .theme import THEMES, FontSet, Theme, get_font_set

MAX_ARCHIVE_ENTRY_BYTES = 64 * MIB
MAX_COMPRESSION_RATIO = 200
MAX_MEMBER_NAME_BYTES = 512
MAX_XML_DEPTH = 128
_CHUNK_BYTES = 64 * 1024

_EOCD = struct.Struct("<4s4H2LH")
# A central-directory file header: signature, 24 fixed bytes, the three
# variable-length field sizes, then 12 more fixed bytes.
_CENTRAL_HEADER = struct.Struct("<4s24xHHH12x")
_ZIP64_LOCATOR = b"PK\x06\x07"
_ZIP64_LOCATOR_SIZE = 20
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


class InvalidOOXMLError(ValueError):
    """The package is malformed or violates the bounded OOXML contract."""


def _central_directory_count(path: Path, limits: ResourceLimits) -> int:
    """Read the EOCD without asking ``zipfile`` to allocate its entry list."""
    size = path.stat().st_size
    if size < _EOCD.size or size > limits.max_archive_expanded_bytes:
        raise ResourceLimitError(
            f"OOXML archive exceeds the {limits.max_archive_expanded_bytes}-byte archive limit"
        )
    tail_size = min(size, 65_535 + _EOCD.size)
    with path.open("rb") as handle:
        handle.seek(size - tail_size)
        tail = handle.read(tail_size)
        offset = tail.rfind(b"PK\x05\x06")
        if offset < 0 or offset + _EOCD.size > len(tail):
            raise InvalidOOXMLError("Invalid OOXML ZIP directory")
        signature, disk, cd_disk, count_disk, count, cd_size, cd_offset, comment = (
            _EOCD.unpack_from(tail, offset)
        )
        eocd_position = size - tail_size + offset
        if (
            signature != b"PK\x05\x06"
            or disk
            or cd_disk
            or count_disk != count
            or count in {0, 0xFFFF}
            or cd_size == 0xFFFFFFFF
            or cd_offset == 0xFFFFFFFF
            or comment != size - eocd_position - _EOCD.size
            or cd_offset + cd_size != eocd_position
        ):
            raise InvalidOOXMLError(
                "Unsupported split, ZIP64, prepended, or malformed OOXML archive"
            )
        # zipfile reads the 20 bytes before the footer and, if they are a ZIP64
        # locator, takes the directory size and offset from the ZIP64 record
        # instead, whatever the footer says. The walk below would then count a
        # different directory, so a locator there is refused outright.
        if eocd_position >= _ZIP64_LOCATOR_SIZE:
            handle.seek(eocd_position - _ZIP64_LOCATOR_SIZE)
            if handle.read(len(_ZIP64_LOCATOR)) == _ZIP64_LOCATOR:
                raise InvalidOOXMLError(
                    "Unsupported split, ZIP64, prepended, or malformed OOXML archive"
                )
        if count > limits.max_archive_entries:
            raise ResourceLimitError(
                f"OOXML archive exceeds the {limits.max_archive_entries}-entry limit"
            )
        # The footer's count is only a claim. ``zipfile`` allocates one entry per
        # record the directory actually holds, so count those records first.
        records = _count_central_directory_records(
            handle, cd_offset, cd_size, limits.max_archive_entries
        )
    if records != count:
        raise InvalidOOXMLError("OOXML ZIP directory entry count is inconsistent")
    return count


def _count_central_directory_records(handle: Any, cd_offset: int, cd_size: int, limit: int) -> int:
    """Walk the central directory one fixed-size header read at a time.

    Stops with ``ResourceLimitError`` as soon as a record beyond ``limit`` is
    found, so a footer that understates the count cannot make ``zipfile``
    allocate an entry for every record in a large directory.
    """
    handle.seek(cd_offset)
    position = 0
    records = 0
    while position < cd_size:
        if records >= limit:
            raise ResourceLimitError(f"OOXML archive exceeds the {limit}-entry limit")
        header = handle.read(_CENTRAL_HEADER.size)
        if len(header) != _CENTRAL_HEADER.size:
            raise InvalidOOXMLError("Invalid OOXML ZIP directory")
        signature, name_length, extra_length, comment_length = _CENTRAL_HEADER.unpack(header)
        position += _CENTRAL_HEADER.size + name_length + extra_length + comment_length
        if signature != b"PK\x01\x02" or position > cd_size:
            raise InvalidOOXMLError("Invalid OOXML ZIP directory")
        handle.seek(cd_offset + position)
        records += 1
    return records


def _safe_member_name(name: str) -> str:
    if (
        not name
        or len(name.encode("utf-8")) > MAX_MEMBER_NAME_BYTES
        or "\\" in name
        or "\x00" in name
        or any(unicodedata.category(character).startswith("C") for character in name)
    ):
        raise InvalidOOXMLError("OOXML archive contains an invalid member name")
    clean = name[:-1] if name.endswith("/") else name
    pure = PurePosixPath(clean)
    if (
        not clean
        or pure.is_absolute()
        or any(part in {"", ".", ".."} for part in pure.parts)
        or re.match(r"^[A-Za-z]:", clean)
    ):
        raise InvalidOOXMLError(f"OOXML member escapes the archive root: {name!r}")
    return clean


class _BoundedTreeBuilder:
    """Build one part's tree while expat streams it, within the package budget.

    Retained memory grows with elements and attributes rather than with bytes:
    ``<a/>`` is four bytes of XML but a whole element object, and a downstream
    converter can multiply that again. Counting both at each start tag stops a
    flood at the first node over budget, before the rest of the part is
    decompressed or built. Delegating to the C tree builder also keeps each
    retained element about half the size of defusedxml's pure-Python default.
    """

    def __init__(self, part: str, remaining: int, limit: int):
        self.part = part
        self.remaining = remaining
        self.limit = limit
        self.depth = 0
        self._builder = _TreeBuilder()

    def start(self, tag: str, attrib: dict[str, str]) -> Any:
        self.depth += 1
        if self.depth > MAX_XML_DEPTH:
            raise ResourceLimitError(
                f"OOXML XML nesting exceeds {MAX_XML_DEPTH} levels: {self.part}"
            )
        self.remaining -= 1 + len(attrib)
        if self.remaining < 0:
            raise ResourceLimitError(
                f"OOXML package exceeds the {self.limit}-node XML limit "
                f"(elements plus attributes): {self.part}"
            )
        return self._builder.start(tag, attrib)

    def end(self, tag: str) -> Any:
        self.depth -= 1
        return self._builder.end(tag)

    def data(self, data: str) -> None:
        self._builder.data(data)

    def close(self) -> Any:
        return self._builder.close()


class _StreamingXML:
    """Feed decompressed chunks straight into a defused, element-counted parser."""

    def __init__(self, part: str, remaining: int, limit: int):
        self.part = part
        self.target = _BoundedTreeBuilder(part, remaining, limit)
        self._parser = DefusedET.DefusedXMLParser(
            target=self.target,
            forbid_dtd=True,
            forbid_entities=True,
            forbid_external=True,
        )

    def feed(self, chunk: bytes) -> None:
        try:
            self._parser.feed(chunk)
        except ResourceLimitError:
            raise
        except (DefusedXmlException, DefusedET.ParseError, ValueError) as exc:
            raise InvalidOOXMLError(f"Unsafe or malformed OOXML part: {self.part}") from exc

    def close(self) -> Any:
        try:
            return self._parser.close()
        except ResourceLimitError:
            raise
        except (DefusedXmlException, DefusedET.ParseError, ValueError) as exc:
            raise InvalidOOXMLError(f"Unsafe or malformed OOXML part: {self.part}") from exc


def _external_relationships(
    root: Any, part: str, allowed: frozenset[str], cancel_check: CancelCheck = never_cancelled
) -> None:
    seen: set[str] = set()
    tick = Throttle(cancel_check, 1024)
    for relationship in root.iter(f"{{{_REL_NS}}}Relationship"):
        tick()
        if relationship.get("TargetMode", "").casefold() != "external":
            continue
        relationship_type = relationship.get("Type", "")
        kind = relationship_type.rsplit("/", 1)[-1] or "unknown"
        target = relationship.get("Target", "")
        relationship_id = relationship.get("Id", "")
        if (
            kind not in allowed
            or not relationship_id
            or relationship_id in seen
            or not target
            or len(relationship_type.encode("utf-8")) > MAX_MEMBER_NAME_BYTES
            or len(relationship_id.encode("utf-8")) > MAX_MEMBER_NAME_BYTES
            or len(target.encode("utf-8")) > MAX_MEMBER_NAME_BYTES
            or any(
                unicodedata.category(character).startswith("C")
                for character in relationship_type + relationship_id
            )
            or any(unicodedata.category(character).startswith("C") for character in target)
        ):
            raise InvalidOOXMLError(f"OOXML external relationship is not allowed in {part}")
        seen.add(relationship_id)


class OOXMLPackage:
    """A validated package with bounded byte and XML accessors.

    The interface is intentionally format-neutral so XLSX can consume the same
    checked revision after DOCX lands.
    """

    def __init__(
        self,
        path: str | Path,
        limits: ResourceLimits,
        *,
        max_xml_nodes: int | None = None,
        allowed_external_relationships: frozenset[str] = frozenset(),
        cancel_check: CancelCheck | None = None,
    ):
        self.path = Path(path)
        self.limits = limits
        # Each format reader passes its own budget; the default is the stricter.
        self.max_xml_nodes = (
            max_xml_nodes
            if max_xml_nodes is not None
            else min(limits.max_docx_xml_nodes, limits.max_xlsx_xml_nodes)
        )
        self.allowed_external_relationships = allowed_external_relationships
        self.cancel_check = cancel_check or never_cancelled
        self._archive: zipfile.ZipFile | None = None
        self._names: dict[str, zipfile.ZipInfo] = {}
        self._xml: dict[str, Any] = {}

    def __enter__(self) -> OOXMLPackage:
        expected = _central_directory_count(self.path, self.limits)
        try:
            archive = zipfile.ZipFile(self.path)
            infos = archive.infolist()
            if len(infos) != expected:
                raise InvalidOOXMLError("OOXML ZIP directory entry count is inconsistent")
            self._archive = archive
            self._validate(infos)
            return self
        except CancelledError:
            # A RuntimeError, but a request to stop rather than a bad archive.
            self.close()
            raise
        except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, RuntimeError) as exc:
            self.close()
            if isinstance(exc, InvalidOOXMLError):
                raise
            raise InvalidOOXMLError("Invalid or unsupported OOXML archive") from exc
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_args) -> None:
        self.close()

    def close(self) -> None:
        if self._archive is not None:
            self._archive.close()
            self._archive = None

    def _validate(self, infos: list[zipfile.ZipInfo]) -> None:
        assert self._archive is not None
        declared_total = 0
        actual_total = 0
        nodes_remaining = self.max_xml_nodes
        seen: set[str] = set()
        for info in infos:
            self.cancel_check()
            name = _safe_member_name(info.filename)
            key = unicodedata.normalize("NFKC", name).casefold()
            if key in seen:
                raise InvalidOOXMLError(f"OOXML archive contains a duplicate member: {name}")
            seen.add(key)
            if info.flag_bits & 1 or info.compress_type not in {
                zipfile.ZIP_STORED,
                zipfile.ZIP_DEFLATED,
            }:
                raise InvalidOOXMLError("OOXML encryption or compression method is unsupported")
            if info.is_dir():
                continue
            per_entry = min(MAX_ARCHIVE_ENTRY_BYTES, self.limits.max_archive_expanded_bytes)
            if info.file_size > per_entry:
                raise ResourceLimitError(
                    f"OOXML member exceeds the {per_entry}-byte per-entry limit: {name}"
                )
            declared_total += info.file_size
            if declared_total > self.limits.max_archive_expanded_bytes:
                raise ResourceLimitError(
                    "OOXML archive exceeds the "
                    f"{self.limits.max_archive_expanded_bytes}-byte expanded limit"
                )
            if info.file_size > max(1, info.compress_size) * MAX_COMPRESSION_RATIO:
                raise ResourceLimitError(
                    f"OOXML member exceeds the {MAX_COMPRESSION_RATIO}:1 compression-ratio limit: {name}"
                )
            parser = (
                _StreamingXML(name, nodes_remaining, self.max_xml_nodes)
                if name.lower().endswith((".xml", ".rels"))
                else None
            )
            actual_total = self._read_validated(info, name, per_entry, actual_total, parser)
            self._names[name] = info
            if parser is not None:
                root = parser.close()
                nodes_remaining = parser.target.remaining
                if name.lower().endswith(".rels"):
                    _external_relationships(
                        root, name, self.allowed_external_relationships, self.cancel_check
                    )
                self._xml[name] = root

    def _read_validated(
        self,
        info: zipfile.ZipInfo,
        name: str,
        per_entry: int,
        actual_total: int,
        parser: _StreamingXML | None,
    ) -> int:
        assert self._archive is not None
        member_size = 0
        try:
            with self._archive.open(info) as member:
                while chunk := member.read(_CHUNK_BYTES):
                    self.cancel_check()
                    member_size += len(chunk)
                    actual_total += len(chunk)
                    if member_size > per_entry:
                        raise ResourceLimitError(
                            f"OOXML member exceeds the {per_entry}-byte per-entry limit: {name}"
                        )
                    if actual_total > self.limits.max_archive_expanded_bytes:
                        raise ResourceLimitError(
                            "OOXML archive exceeds the "
                            f"{self.limits.max_archive_expanded_bytes}-byte expanded limit"
                        )
                    if parser is not None:
                        parser.feed(chunk)
        except (zipfile.BadZipFile, EOFError, OSError) as exc:
            raise InvalidOOXMLError(f"Could not decompress OOXML member: {name}") from exc
        if member_size != info.file_size:
            raise InvalidOOXMLError(f"OOXML member size is inconsistent: {name}")
        return actual_total

    def names(self) -> tuple[str, ...]:
        """Validated non-directory members, in archive order."""
        return tuple(self._names)

    def has(self, part: str) -> bool:
        return part in self._names

    def read(self, part: str, *, max_bytes: int | None = None) -> bytes:
        if self._archive is None:
            raise RuntimeError("OOXML package is closed")
        info = self._names.get(part)
        if info is None:
            raise KeyError(part)
        limit = min(
            MAX_ARCHIVE_ENTRY_BYTES,
            self.limits.max_archive_expanded_bytes,
            max_bytes if max_bytes is not None else self.limits.max_archive_expanded_bytes,
        )
        if info.file_size > limit:
            raise ResourceLimitError(f"OOXML member exceeds the {limit}-byte read limit: {part}")
        with self._archive.open(info) as member:
            raw = member.read(limit + 1)
        if len(raw) > limit:
            raise ResourceLimitError(f"OOXML member exceeds the {limit}-byte read limit: {part}")
        return raw

    def xml(self, part: str) -> Any:
        return self._xml.get(part)


def _colour(element: Any) -> str | None:
    for child in element:
        for value in (child.get("val"), child.get("lastClr")):
            if value and re.fullmatch(r"[0-9A-Fa-f]{6}", value):
                return f"#{value.lower()}"
    return None


def _mix(foreground: str, background: str, foreground_weight: float) -> str:
    fg = [int(foreground[index : index + 2], 16) for index in (1, 3, 5)]
    bg = [int(background[index : index + 2], 16) for index in (1, 3, 5)]
    values = [
        round(a * foreground_weight + b * (1 - foreground_weight))
        for a, b in zip(fg, bg, strict=True)
    ]
    return "#" + "".join(f"{value:02x}" for value in values)


def _safe_typeface(value: str | None) -> str | None:
    value = (value or "").strip()
    if not value or len(value) > 80:
        return None
    if any(not (character.isalnum() or character in " ._-") for character in value):
        return None
    return value


def _font_stack(face: str, fallback: str) -> str:
    return f"'{face}', {fallback}"


def _source_fonts(font_scheme: Any, label: str) -> tuple[FontSet | None, list[tuple[str, str]]]:
    major = font_scheme.find(f"{{{_DRAWING_NS}}}majorFont")
    minor = font_scheme.find(f"{{{_DRAWING_NS}}}minorFont")

    def face(block) -> str | None:
        if block is None:
            return None
        for kind in ("latin", "ea", "cs"):
            item = block.find(f"{{{_DRAWING_NS}}}{kind}")
            if item is not None and (value := _safe_typeface(item.get("typeface"))):
                return value
        return None

    heading, body = face(major), face(minor)
    if not heading and not body:
        return None, []
    base = get_font_set("calibri")
    heading = heading or body
    body = body or heading
    assert heading and body
    fonts = FontSet(
        name="source",
        label=f"Fonts requested by {label}",
        body=_font_stack(body, base.body),
        head=_font_stack(heading, base.head),
        mono=base.mono,
    )
    return fonts, []


def drawingml_theme(
    package: OOXMLPackage,
    part: str,
    *,
    label: str,
) -> tuple[Theme | None, list[tuple[str, str]]]:
    """Map a DrawingML colour/font scheme onto Plate|Fold's theme tokens."""
    root = package.xml(part)
    if root is None:
        return None, [
            ("SOURCE_THEME_MISSING", f"{label} carries no DrawingML theme; using fallback theme")
        ]
    colour_scheme = root.find(f".//{{{_DRAWING_NS}}}clrScheme")
    font_scheme = root.find(f".//{{{_DRAWING_NS}}}fontScheme")
    if colour_scheme is None:
        return None, [
            ("SOURCE_THEME_MISSING", f"{label} carries no colour scheme; using fallback theme")
        ]
    colours = {
        child.tag.rsplit("}", 1)[-1]: value
        for child in colour_scheme
        if (value := _colour(child)) is not None
    }
    required = {"lt1", "dk1", "dk2", "lt2", "accent1", "accent2", "hlink"}
    if not required <= colours.keys():
        missing = ", ".join(sorted(required - colours.keys()))
        return None, [
            ("SOURCE_THEME_INCOMPLETE", f"{label} theme is missing {missing}; using fallback theme")
        ]
    page, ink = colours["lt1"], colours["dk1"]
    accent = colours["accent1"]
    surface = colours["lt2"]
    base = THEMES["default"]
    fonts, warnings = _source_fonts(font_scheme, label) if font_scheme is not None else (None, [])
    theme = base.merged(
        page=page,
        ink=ink,
        ink_strong=ink,
        title=colours["dk2"],
        heading=colours["dk2"],
        surface=surface,
        surface_alt=_mix(surface, page, 0.55),
        accent=accent,
        accent_soft=_mix(accent, page, 0.42),
        diagram_fill=_mix(accent, page, 0.20),
        diagram_stroke=accent,
        diagram_text=ink,
        diagram_alt_fill=colours["accent2"],
        diagram_note_fill=_mix(surface, page, 0.72),
        diagram_note_stroke=_mix(ink, page, 0.35),
        link=colours["hlink"],
        rule=_mix(ink, page, 0.22),
        ink_soft=_mix(ink, page, 0.58),
    )
    return Theme(
        **{
            **theme.__dict__,
            "name": "source",
            "label": f"Source theme from {label}",
            "mode": "dark"
            if sum(int(page[index : index + 2], 16) for index in (1, 3, 5)) < 384
            else "light",
            "description": "DrawingML palette and requested typefaces imported for semantic re-layout.",
            "source": str(package.path),
            "fonts": fonts,
        }
    ), warnings
