"""Adversarial source boundaries use real files, links and ZIP headers."""

from __future__ import annotations

import io
import os
import stat
import struct
import sys
import zipfile
from dataclasses import replace

import pytest

pytest.importorskip("pydantic")

from platefold.server_models import JobLimits
from platefold.server_sources import InvalidSource, extract_zip, snapshot_mount

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux server contract")


def bundle(tmp_path, entries, *, compression=zipfile.ZIP_STORED):
    path = tmp_path / "input.zip"
    with zipfile.ZipFile(path, "w", compression=compression) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return path


@pytest.mark.parametrize(
    "name",
    [
        "../escape.md",
        "/escape.md",
        "C:/escape.md",
        "folder/../../escape.md",
        "folder\\escape.md",
        "./escape.md",
        "folder//escape.md",
        "folder/escape.md ",
        "folder/\x01.md",
        "folder/\u202e.md",
        "a/" * 9 + "doc.md",
        "x" * 101 + ".md",
        "config.json",
        "export.toml",
        "nested.zip",
        "script.py",
    ],
)
def test_hostile_archive_paths(tmp_path, name):
    archive = bundle(tmp_path, [(name, "# Document")])
    with pytest.raises(InvalidSource):
        extract_zip(archive, tmp_path / "source", JobLimits())
    assert not (tmp_path / "escape.md").exists()


@pytest.mark.parametrize("kind", [stat.S_IFLNK, stat.S_IFCHR, stat.S_IFIFO, stat.S_IFSOCK])
def test_archive_special_files(tmp_path, kind):
    info = zipfile.ZipInfo("document.md")
    info.create_system = 3
    info.external_attr = (kind | 0o777) << 16
    archive = bundle(tmp_path, [(info, "../secret")])
    with pytest.raises(InvalidSource):
        extract_zip(archive, tmp_path / "source", JobLimits())


@pytest.mark.parametrize(
    "entries",
    [
        [("Doc.md", "a"), ("doc.md", "b")],
        [("doc.md", "a"), ("doc.md", "b")],
        [("\uff21.md", "a"), ("A.md", "b")],
        [("a.md", "a"), ("a.md/b.md", "b")],
    ],
)
def test_archive_ambiguous_names(tmp_path, entries):
    archive = bundle(tmp_path, entries)
    with pytest.raises(InvalidSource):
        extract_zip(archive, tmp_path / "source", JobLimits())


def test_zip_bombs_entry_count_and_central_directory_are_bounded(tmp_path):
    archive = bundle(tmp_path, [("doc.md", "0" * 100_000)], compression=zipfile.ZIP_DEFLATED)
    with pytest.raises(InvalidSource):
        extract_zip(archive, tmp_path / "source", JobLimits())
    archive = bundle(tmp_path, [("a.md", "x"), ("b.md", "y")])
    with pytest.raises(InvalidSource):
        extract_zip(archive, tmp_path / "source", replace(JobLimits(), files=1))
    data = bytearray(archive.read_bytes())
    struct.pack_into("<L", data, len(data) - 10, 0xFFFFFFFF)
    archive.write_bytes(data)
    with pytest.raises(InvalidSource):
        extract_zip(archive, tmp_path / "source", JobLimits())


def test_zip_encryption_crc_and_actual_extracted_limit(tmp_path):
    archive = bundle(tmp_path, [("doc.md", "# Test document")])
    with pytest.raises(InvalidSource):
        extract_zip(archive, tmp_path / "source", replace(JobLimits(), source_bytes=1))
    raw = bytearray(archive.read_bytes())
    header = raw.index(b"PK\x01\x02")
    raw[header + 8] |= 1
    archive.write_bytes(raw)
    with pytest.raises(InvalidSource):
        extract_zip(archive, tmp_path / "source2", JobLimits())
    archive = bundle(tmp_path, [("doc.md", "# Test document")])
    raw = archive.read_bytes().replace(b"# Test document", b"# Best document")
    archive.write_bytes(raw)
    with pytest.raises(InvalidSource):
        extract_zip(archive, tmp_path / "source3", JobLimits())


def test_valid_nested_zip_and_mount_snapshot(tmp_path):
    entries = [
        ("folder/doc.md", "# Document"),
        ("folder/table.csv", "A,B\n1,2"),
        ("folder/raw.tsv", "A\tB\n1\t2"),
        ("folder/image.svg", "<svg/>"),
    ]
    archive = bundle(tmp_path, entries)
    extract_zip(archive, tmp_path / "source", JobLimits())
    snapshot_mount(tmp_path / "source", "", tmp_path / "copy", JobLimits())
    assert (tmp_path / "copy/folder/doc.md").read_text() == "# Document"
    assert (tmp_path / "copy/folder/table.csv").read_text() == "A,B\n1,2"
    (tmp_path / "source/folder/doc.md").write_text("Changed after snapshot")
    assert (tmp_path / "copy/folder/doc.md").read_text() == "# Document"


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "dirlink"])
def test_mounted_links_and_special_files(tmp_path, kind):
    source = tmp_path / "mount"
    source.mkdir()
    outside = tmp_path / "private.md"
    outside.write_text("PRIVATE SENTINEL")
    entry = source / "doc.md"
    if kind == "symlink":
        entry.symlink_to(outside)
    elif kind == "hardlink":
        os.link(outside, entry)
    elif kind == "dirlink":
        entry.symlink_to(tmp_path, target_is_directory=True)
    else:
        os.mkfifo(entry)
    with pytest.raises(InvalidSource):
        snapshot_mount(source, "", tmp_path / "copy", JobLimits())
    assert outside.read_text() == "PRIVATE SENTINEL"


def test_mount_component_swap_does_not_follow_symlink(tmp_path, monkeypatch):
    source = tmp_path / "mount"
    (source / "child").mkdir(parents=True)
    (source / "child/doc.md").write_text("permitted")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "doc.md").write_text("PRIVATE")
    real_open = os.open

    def racing_open(path, flags, *args, **kwargs):
        if path == "child":
            (source / "child").rename(source / "old")
            (source / "child").symlink_to(outside, target_is_directory=True)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises(InvalidSource):
        snapshot_mount(source, "child/doc.md", tmp_path / "copy", JobLimits())
    assert not (tmp_path / "copy/doc.md").exists()


def test_declared_sizes_do_not_replace_actual_read_limits(tmp_path):
    from platefold.server_sources import SourceBudget

    budget = SourceBudget(replace(JobLimits(), file_bytes=4))
    with pytest.raises(InvalidSource):
        budget.copy(io.BytesIO(b"12345"), tmp_path / "doc.md")
