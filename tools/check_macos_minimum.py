"""Check that a Mac app declares a minimum macOS every binary inside it can run on.

macOS reads ``LSMinimumSystemVersion`` from the bundle's Info.plist to decide
whether to open the app. Each Mach-O file inside records the oldest macOS it
was built for, in an ``LC_BUILD_VERSION`` or ``LC_VERSION_MIN_MACOSX`` load
command. If any of those is newer than the declared minimum, older Macs open
the app and then fail to load its libraries. This check reads both with the
standard library only, so it runs on the Mac that builds the app and on the
Linux host that verifies the signed release.

    python tools/check_macos_minimum.py path/to/PlateFold.app
    python tools/check_macos_minimum.py path/to/PlateFold_1.0.0_macos-arm64.app.tar.gz
"""

from __future__ import annotations

import argparse
import plistlib
import re
import struct
import sys
import tarfile
from collections import Counter
from collections.abc import Iterable, Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

Version = tuple[int, int, int]

MH_MAGIC = 0xFEEDFACE
MH_MAGIC_64 = 0xFEEDFACF
FAT_MAGIC = 0xCAFEBABE
FAT_MAGIC_64 = 0xCAFEBABF
LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
PLATFORM_MACOS = 1
# Java class files share the fat magic; their version field reads as 45 or more.
MAX_FAT_ARCHES = 30
MAX_LOAD_COMMANDS_BYTES = 16 * 1024 * 1024
MAX_INFO_PLIST = 1024 * 1024


class MachOError(ValueError):
    """A file starts like a Mach-O binary but cannot be read as one."""


@dataclass(frozen=True)
class Finding:
    path: str
    minimum: Version


def parse_version(text: str) -> Version:
    """Read a dotted macOS version such as ``15.1`` as ``(15, 1, 0)``."""
    if not isinstance(text, str) or not re.fullmatch(r"\d{1,3}(\.\d{1,3}){0,2}", text):
        raise ValueError(f"Not a macOS version: {text!r}")
    major, minor, patch = [*map(int, text.split(".")), 0, 0][:3]
    return major, minor, patch


def format_version(version: Version) -> str:
    major, minor, patch = version
    return f"{major}.{minor}.{patch}" if patch else f"{major}.{minor}"


def _decode_version(value: int) -> Version:
    # Mach-O versions pack xxxx.yy.zz into 16, 8 and 8 bits.
    return value >> 16, (value >> 8) & 0xFF, value & 0xFF


def _read_exact(handle: BinaryIO, offset: int, size: int) -> bytes:
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise MachOError("truncated Mach-O data")
    return data


def _thin_minimums(handle: BinaryIO, offset: int, endian: str, wide: bool) -> list[Version]:
    header = _read_exact(handle, offset, 32 if wide else 28)
    _, _, _, _, ncmds, sizeofcmds, _ = struct.unpack(endian + "7I", header[:28])
    if sizeofcmds > MAX_LOAD_COMMANDS_BYTES or ncmds * 8 > sizeofcmds:
        raise MachOError("implausible load command table")
    commands = _read_exact(handle, offset + len(header), sizeofcmds)
    found = []
    position = 0
    for _ in range(ncmds):
        if position + 8 > sizeofcmds:
            raise MachOError("load command runs past its table")
        command, size = struct.unpack_from(endian + "2I", commands, position)
        if size < 8 or position + size > sizeofcmds:
            raise MachOError("load command has an invalid size")
        if command == LC_BUILD_VERSION and size >= 24:
            platform, minos = struct.unpack_from(endian + "2I", commands, position + 8)
            if platform == PLATFORM_MACOS:
                found.append(_decode_version(minos))
        elif command == LC_VERSION_MIN_MACOSX and size >= 16:
            (version,) = struct.unpack_from(endian + "I", commands, position + 8)
            found.append(_decode_version(version))
        position += size
    return found


def _thin_kind(magic: bytes) -> tuple[str, bool] | None:
    for endian in ("<", ">"):
        (value,) = struct.unpack(endian + "I", magic)
        if value in (MH_MAGIC, MH_MAGIC_64):
            return endian, value == MH_MAGIC_64
    return None


def macho_minimums(handle: BinaryIO) -> list[Version] | None:
    """Return each macOS minimum recorded in a Mach-O file, or None if it is not one.

    A universal (fat) file contributes one entry per architecture slice that
    records a macOS minimum. ``handle`` must be readable and seekable.
    """
    magic = handle.read(4)
    if len(magic) != 4:
        return None
    if thin := _thin_kind(magic):
        return _thin_minimums(handle, 0, *thin)
    for endian in (">", "<"):
        (value,) = struct.unpack(endian + "I", magic)
        if value in (FAT_MAGIC, FAT_MAGIC_64):
            wide = value == FAT_MAGIC_64
            break
    else:
        return None
    (count,) = struct.unpack(endian + "I", _read_exact(handle, 4, 4))
    if not 0 < count <= MAX_FAT_ARCHES:
        return None
    entry = 32 if wide else 20
    table = _read_exact(handle, 8, count * entry)
    found = []
    for index in range(count):
        if wide:
            _, _, offset, _, _, _ = struct.unpack_from(endian + "2I2Q2I", table, index * entry)
        else:
            _, _, offset, _, _ = struct.unpack_from(endian + "5I", table, index * entry)
        thin = _thin_kind(_read_exact(handle, offset, 4))
        if thin is None:
            raise MachOError("universal binary slice is not a Mach-O file")
        found.extend(_thin_minimums(handle, offset, *thin))
    return found


def check_bundle(
    declared: str | None, files: Iterable[tuple[str, AbstractContextManager[BinaryIO]]]
) -> tuple[Version, list[Finding], list[Finding]]:
    """Compare every Mach-O file's minimum with the declared bundle minimum.

    Returns the declared version, every Mach-O file's highest minimum, and the
    files that need a newer macOS than the bundle declares.
    """
    if declared is None:
        raise ValueError("Info.plist has no LSMinimumSystemVersion")
    minimum = parse_version(declared)
    binaries = []
    for name, opener in files:
        with opener as handle:
            try:
                versions = macho_minimums(handle)
            except (MachOError, struct.error, OSError) as error:
                raise ValueError(f"{name}: cannot read Mach-O load commands ({error})") from None
        if versions:
            binaries.append(Finding(name, max(versions)))
    too_new = [finding for finding in binaries if finding.minimum > minimum]
    return minimum, binaries, too_new


def _declared_minimum(data: bytes) -> str | None:
    info = plistlib.loads(data)
    if not isinstance(info, dict):
        raise ValueError("Info.plist is not a dictionary")
    value = info.get("LSMinimumSystemVersion")
    if value is not None and not isinstance(value, str):
        raise ValueError("LSMinimumSystemVersion is not a string")
    return value


def check_app(app: Path) -> tuple[Version, list[Finding], list[Finding]]:
    """Check an unpacked ``.app`` directory."""
    plist = app / "Contents/Info.plist"
    declared = _declared_minimum(plist.read_bytes())
    files = (
        (path.relative_to(app).as_posix(), path.open("rb"))
        for path in sorted(app.rglob("*"))
        if path.is_file() and not path.is_symlink()
    )
    return check_bundle(declared, files)


def check_app_archive(archive_path: Path) -> tuple[Version, list[Finding], list[Finding]]:
    """Check an updater archive holding ``PlateFold.app`` or another single app."""
    with tarfile.open(archive_path, "r:*") as archive:
        members = [member for member in archive.getmembers() if member.isfile()]
        plists = [
            member
            for member in members
            if PurePosixPath(member.name).parts[1:] == ("Contents", "Info.plist")
            and PurePosixPath(member.name).parts[0].endswith(".app")
        ]
        if len(plists) != 1:
            raise ValueError("Archive must hold exactly one app bundle Info.plist")
        if plists[0].size > MAX_INFO_PLIST:
            raise ValueError("Info.plist is too large")
        declared = _declared_minimum(_member_bytes(archive, plists[0]))
        return check_bundle(declared, _members(archive, members))


def _member_bytes(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    with _member(archive, member) as handle:
        return handle.read()


def _member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> AbstractContextManager[BinaryIO]:
    handle = archive.extractfile(member)
    if handle is None:
        raise ValueError(f"{member.name}: cannot be read")
    return handle


def _members(
    archive: tarfile.TarFile, members: list[tarfile.TarInfo]
) -> Iterator[tuple[str, AbstractContextManager[BinaryIO]]]:
    for member in members:
        name = PurePosixPath(member.name)
        yield PurePosixPath(*name.parts[1:]).as_posix(), _member(archive, member)


def summary(minimum: Version, binaries: list[Finding], too_new: list[Finding]) -> str:
    counts = Counter(finding.minimum for finding in binaries)
    spread = ", ".join(
        f"{count} at {format_version(version)}"
        for version, count in sorted(counts.items(), reverse=True)
    )
    lines = [
        f"LSMinimumSystemVersion {format_version(minimum)}; "
        f"{len(binaries)} Mach-O files record a macOS minimum ({spread or 'none'})"
    ]
    for finding in too_new:
        lines.append(f"  requires macOS {format_version(finding.minimum)}: {finding.path}")
    return "\n".join(lines)


def require_supported_minimum(path: Path) -> tuple[Version, str]:
    """Check an app or updater archive; raise ValueError naming every offender.

    Returns the declared minimum and a one-line summary for build logs.
    """
    check = check_app if path.is_dir() else check_app_archive
    minimum, binaries, too_new = check(path)
    report = summary(minimum, binaries, too_new)
    if not binaries:
        raise ValueError(f"{report}\nNo Mach-O file records a macOS minimum; nothing was checked")
    if too_new:
        highest = format_version(max(finding.minimum for finding in too_new))
        raise ValueError(
            f"{report}\nThe app declares macOS {format_version(minimum)} but needs {highest}. "
            "Raise bundle.macOS.minimumSystemVersion in desktop/src-tauri/tauri.conf.json "
            "and the documented system requirement, or build the runtime for an older macOS."
        )
    return minimum, report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("bundle", type=Path, help="A .app directory or an .app.tar.gz archive")
    args = parser.parse_args(argv)
    try:
        print(require_supported_minimum(args.bundle)[1])
    except (OSError, ValueError, tarfile.TarError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
