"""Bounded ZIP extraction and descriptor-based snapshots of configured mounts.

Only ordinary supported documents/images enter a private job source tree. Configuration,
themes, links and archive nesting are deliberately outside the upload contract.
"""

from __future__ import annotations

import contextlib
import os
import stat
import struct
import unicodedata
import zipfile
from pathlib import Path
from typing import BinaryIO

from platefold.server_models import JobLimits

DOCUMENT_SUFFIXES = {".md", ".doc", ".docx", ".odt", ".rtf", ".xlsx", ".csv", ".tsv"}
SUFFIXES = DOCUMENT_SUFFIXES | {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


class InvalidSource(ValueError):
    pass


def parts_for(name: str, *, empty: bool = False) -> tuple[str, ...]:
    if empty and name == "":
        return ()
    parts = tuple(name.split("/"))
    if (
        len(name.encode("utf-8")) > 512
        or len(parts) > 8
        or any(c in name for c in "\\:\x00")
        or any(unicodedata.category(c).startswith("C") for c in name)
        or any(
            p in {"", ".", ".."} or len(p.encode("utf-8")) > 100 or p.endswith((".", " "))
            for p in parts
        )
    ):
        raise InvalidSource("Source paths must be bounded relative file names")
    return parts


class SourceBudget:
    def __init__(self, limits: JobLimits):
        self.limits = limits
        self.size = self.files = self.documents = 0
        self.names: set[str] = set()

    def entry(self, name: str, directory: bool = False) -> None:
        parts_for(name)
        key = unicodedata.normalize("NFKC", name).casefold()
        if key in self.names:
            raise InvalidSource("Duplicate or ambiguous source path")
        self.names.add(key)
        self.files += 1
        if self.files > self.limits.files:
            raise InvalidSource("Source has too many entries")
        if directory:
            return
        if Path(name).suffix.lower() not in SUFFIXES:
            raise InvalidSource("Only supported documents and image files are accepted")
        if Path(name).suffix.lower() in DOCUMENT_SUFFIXES:
            self.documents += 1
            if self.documents > self.limits.documents:
                raise InvalidSource("Source has too many documents")

    def copy(self, source: BinaryIO, destination: Path) -> None:
        size = 0
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("xb") as target:
            while chunk := source.read(64 * 1024):
                size += len(chunk)
                self.size += len(chunk)
                if size > self.limits.file_bytes or self.size > self.limits.source_bytes:
                    raise InvalidSource("Expanded source exceeds its byte limit")
                target.write(chunk)

    def finish(self) -> None:
        if not self.documents:
            raise InvalidSource("Source must contain at least one readable document")


def check_zip_directory(path: Path, limits: JobLimits) -> None:
    """Reject huge central directories before ZipFile allocates one object per entry.

    ZIP64, split archives, prepended executables and archive comments are not part
    of v1. This also prevents a small compressed body allocating millions of names.
    """
    size = path.stat().st_size
    if size < 22 or size > limits.upload_bytes:
        raise InvalidSource("Invalid ZIP size")
    with path.open("rb") as handle:
        handle.seek(-22, os.SEEK_END)
        end = handle.read(22)
        signature, disk, cd_disk, count_disk, count, cd_size, offset, comment = struct.unpack(
            "<4s4H2LH", end
        )
        if (
            signature != b"PK\x05\x06"
            or disk
            or cd_disk
            or comment
            or count_disk != count
            or not 0 < count <= limits.files
            or cd_size > limits.files * 1024
            or offset + cd_size != size - 22
        ):
            raise InvalidSource("ZIP directory exceeds limits or uses an unsupported format")
        handle.seek(0)
        if handle.read(4) != b"PK\x03\x04":
            raise InvalidSource("ZIP must begin with a file header")


def extract_zip(path: Path, destination: Path, limits: JobLimits) -> None:
    check_zip_directory(path, limits)
    budget = SourceBudget(limits)
    try:
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                _extract_entry(archive, info, destination, budget)
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, OSError) as exc:
        raise InvalidSource("Invalid or unsupported ZIP contents") from exc
    budget.finish()


def _extract_entry(archive, info, destination, budget) -> None:
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    directory = info.is_dir()
    if (
        info.flag_bits & 1
        or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}
        or file_type not in {0, stat.S_IFDIR if directory else stat.S_IFREG}
        or info.orig_filename != info.filename
        or info.file_size > budget.limits.file_bytes
        or info.file_size > max(1, info.compress_size) * 200
    ):
        raise InvalidSource("ZIP links, special files, encryption or excessive compression")
    name = info.filename[:-1] if directory else info.filename
    budget.entry(name, directory)
    target = destination.joinpath(*parts_for(name))
    if directory:
        target.mkdir(parents=True, exist_ok=True)
    else:
        with archive.open(info) as source:
            budget.copy(source, target)


def open_directory(path: Path) -> int:
    """Open every component without following links, including configured roots."""
    if not path.is_absolute():
        raise InvalidSource("Configured mounts must be absolute paths")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def snapshot_mount(root: Path, relative: str, destination: Path, limits: JobLimits) -> None:
    budget = SourceBudget(limits)
    parts = parts_for(relative, empty=True)
    try:
        with contextlib.ExitStack() as stack:
            fd = open_directory(root)
            stack.callback(os.close, fd)
            for part in parts[:-1]:
                fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                stack.callback(os.close, fd)
            if parts:
                _snapshot_entry(fd, parts[-1], destination, parts[-1], budget)
            else:
                _snapshot_directory(fd, destination, "", budget)
    except OSError as exc:
        raise InvalidSource("Mount source is unavailable or is not a regular file tree") from exc
    budget.finish()


def _snapshot_directory(fd: int, destination: Path, prefix: str, budget: SourceBudget) -> None:
    # scandir is lazy, unlike listdir: a huge directory is rejected without a huge allocation.
    with os.scandir(fd) as entries:
        for entry in entries:
            name = f"{prefix}/{entry.name}" if prefix else entry.name
            _snapshot_entry(fd, entry.name, destination, name, budget)


def _snapshot_entry(fd: int, name: str, destination: Path, relative: str, budget) -> None:
    child = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW, dir_fd=fd)
    try:
        info = os.fstat(child)
        directory = stat.S_ISDIR(info.st_mode)
        if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
            raise InvalidSource("Mounted links and special files are not accepted")
        budget.entry(relative, directory)
        if directory:
            _snapshot_directory(child, destination, relative, budget)
        else:
            with os.fdopen(os.dup(child), "rb") as source:
                budget.copy(source, destination.joinpath(*parts_for(relative)))
    finally:
        os.close(child)
