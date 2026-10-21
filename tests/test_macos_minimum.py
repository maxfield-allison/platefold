"""The Mac app's declared minimum macOS covers every binary inside it."""

from __future__ import annotations

import io
import json
import plistlib
import struct
import tarfile
from pathlib import Path

import pytest

from tools import build_desktop
from tools.check_macos_minimum import (
    MachOError,
    format_version,
    macho_minimums,
    main,
    parse_version,
    require_supported_minimum,
)

LC_VERSION_MIN_MACOSX = 0x24
LC_BUILD_VERSION = 0x32
LC_UUID = 0x1B
MACOS, IOS, MACCATALYST = 1, 2, 6
ARM64, X86_64 = 0x0100000C, 0x01000007


def packed(version: str) -> int:
    major, minor, patch = parse_version(version)
    return major << 16 | minor << 8 | patch


def build_version(minos: str, platform: int = MACOS) -> tuple[int, bytes]:
    # platform, minos, sdk, ntools
    return LC_BUILD_VERSION, struct.pack("<4I", platform, packed(minos), packed("15.5"), 0)


def version_min(minos: str) -> tuple[int, bytes]:
    return LC_VERSION_MIN_MACOSX, struct.pack("<2I", packed(minos), packed("10.15"))


def thin(*commands: tuple[int, bytes], wide: bool = True, big_endian: bool = False) -> bytes:
    """A synthetic Mach-O header and load command table, padded like a real file."""
    endian = ">" if big_endian else "<"
    body = b""
    for command, payload in (*commands, (LC_UUID, bytes(16))):
        if big_endian:
            payload = b"".join(
                struct.pack(">I", value) for (value,) in struct.iter_unpack("<I", payload)
            )
        body += struct.pack(endian + "2I", command, 8 + len(payload)) + payload
    magic = 0xFEEDFACF if wide else 0xFEEDFACE
    header = struct.pack(endian + "7I", magic, ARM64, 0, 6, len(commands) + 1, len(body), 0)
    if wide:
        header += bytes(4)
    return header + body + bytes(64)


def fat(*slices: bytes, wide: bool = False) -> bytes:
    """A universal binary holding each slice at a 4 KiB boundary."""
    table = b""
    offset = 4096
    for index, data in enumerate(slices):
        cpu = ARM64 if index % 2 else X86_64
        if wide:
            table += struct.pack(">2I2Q2I", cpu, 0, offset, len(data), 12, 0)
        else:
            table += struct.pack(">5I", cpu, 0, offset, len(data), 12)
        offset += 4096 * (1 + len(data) // 4096)
    header = struct.pack(">2I", 0xCAFEBABF if wide else 0xCAFEBABE, len(slices)) + table
    image = bytearray(header.ljust(4096, b"\0"))
    for data in slices:
        image += data.ljust(4096 * (1 + len(data) // 4096), b"\0")
    return bytes(image)


def minimums(data: bytes):
    result = macho_minimums(io.BytesIO(data))
    return result if result is None else [format_version(version) for version in result]


@pytest.mark.parametrize(
    "data,expected",
    [
        (thin(build_version("15.1")), ["15.1"]),
        (thin(build_version("13.0"), wide=False), ["13.0"]),
        (thin(build_version("11.0"), big_endian=True), ["11.0"]),
        (thin(version_min("10.9")), ["10.9"]),
        (thin(version_min("10.13.4")), ["10.13.4"]),
        (thin(build_version("17.0", IOS)), []),
        (thin(build_version("15.1"), build_version("18.0", MACCATALYST)), ["15.1"]),
        (thin(), []),
        (fat(thin(build_version("10.13")), thin(build_version("11.0"))), ["10.13", "11.0"]),
        (fat(thin(version_min("10.9")), thin(build_version("15.1")), wide=True), ["10.9", "15.1"]),
    ],
    ids=[
        "thin-64",
        "thin-32",
        "big-endian",
        "version-min",
        "patch-level",
        "other-platform",
        "zippered",
        "no-version",
        "universal",
        "universal-64",
    ],
)
def test_reads_the_macos_minimum_from_each_slice(data, expected):
    assert minimums(data) == expected


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"#!/bin/sh\n",
        b"\xca\xfe\xba\xbe\x00\x00\x00\x41" + bytes(64),  # A Java class file.
        b"PK\x03\x04" + bytes(64),
    ],
    ids=["empty", "script", "java-class", "zip"],
)
def test_other_files_are_not_mach_o(data):
    assert minimums(data) is None


def test_damaged_mach_o_is_an_error_not_a_pass():
    whole = thin(build_version("15.1"))
    with pytest.raises(MachOError):
        minimums(whole[:40])
    bad_size = bytearray(whole)
    struct.pack_into("<I", bad_size, 36, 4)  # First load command claims 4 bytes.
    with pytest.raises(MachOError):
        minimums(bytes(bad_size))
    with pytest.raises(MachOError):
        minimums(fat(b"not a slice" * 4))


def test_versions_parse_and_print_as_macos_writes_them():
    assert parse_version("15.1") == (15, 1, 0)
    assert parse_version("10.13.4") == (10, 13, 4)
    assert format_version((15, 0, 0)) == "15.0"
    for text in ("", "15.x", "15.1.2.3", "v15"):
        with pytest.raises(ValueError):
            parse_version(text)


def make_app(root: Path, declared: str | None, binaries: dict[str, bytes]) -> Path:
    app = root / "PlateFold.app"
    (app / "Contents/MacOS").mkdir(parents=True)
    info = {"CFBundleExecutable": "platefold-desktop"}
    if declared is not None:
        info["LSMinimumSystemVersion"] = declared
    (app / "Contents/Info.plist").write_bytes(plistlib.dumps(info))
    (app / "Contents/Resources").mkdir()
    (app / "Contents/Resources/README.txt").write_text("not a binary\n")
    for name, data in binaries.items():
        path = app / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return app


def archive(app: Path) -> Path:
    target = app.parent / "PlateFold_1.0.0_macos-arm64.app.tar.gz"
    with tarfile.open(target, "w:gz") as handle:
        handle.add(app, arcname=app.name)
    return target


# The distribution measured in the 1.0.0 build: frozen Python at 15.1, its
# OpenSSL at 15.0, the headless browser at 13.0 and the app itself at 11.0.
RUNTIME = {
    "Contents/MacOS/platefold-desktop": thin(build_version("11.0")),
    "Contents/Resources/runtime/_internal/libpython3.12.dylib": thin(build_version("15.1")),
    "Contents/Resources/runtime/_internal/libssl.3.dylib": thin(build_version("15.0")),
    "Contents/Resources/runtime/browser/chrome-headless-shell": thin(build_version("13.0")),
    "Contents/Resources/runtime/platefold": fat(thin(build_version("11.0"))),
}


@pytest.mark.parametrize("packed_as", ["app", "archive"])
def test_app_declaring_what_its_binaries_need_passes(tmp_path, packed_as):
    app = make_app(tmp_path, "15.1", RUNTIME)
    minimum, report = require_supported_minimum(app if packed_as == "app" else archive(app))
    assert minimum == (15, 1, 0)
    assert report.startswith("LSMinimumSystemVersion 15.1; 5 Mach-O files")
    assert "1 at 15.1, 1 at 15.0, 1 at 13.0, 2 at 11.0" in report


@pytest.mark.parametrize("packed_as", ["app", "archive"])
def test_app_declaring_the_tauri_default_fails_naming_each_binary(tmp_path, packed_as):
    app = make_app(tmp_path, "10.13", RUNTIME)
    with pytest.raises(ValueError) as refused:
        require_supported_minimum(app if packed_as == "app" else archive(app))
    message = str(refused.value)
    assert "declares macOS 10.13 but needs 15.1" in message
    assert all(name in message for name in RUNTIME)
    assert "README.txt" not in message


def test_one_binary_above_the_declared_minimum_fails(tmp_path):
    app = make_app(
        tmp_path,
        "15.1",
        {**RUNTIME, "Contents/Frameworks/libnew.dylib": thin(build_version("15.2"))},
    )
    with pytest.raises(ValueError, match=r"needs 15\.2") as refused:
        require_supported_minimum(app)
    assert "requires macOS 15.2: Contents/Frameworks/libnew.dylib" in str(refused.value)
    assert "requires macOS 15.1" not in str(refused.value)


def test_universal_binary_is_judged_by_its_newest_slice(tmp_path):
    universal = fat(thin(build_version("10.13")), thin(build_version("15.4")))
    app = make_app(tmp_path, "15.1", {"Contents/MacOS/platefold-desktop": universal})
    with pytest.raises(ValueError, match=r"needs 15\.4"):
        require_supported_minimum(app)


def test_missing_declaration_or_binaries_is_not_a_pass(tmp_path):
    with pytest.raises(ValueError, match="no LSMinimumSystemVersion"):
        require_supported_minimum(make_app(tmp_path / "a", None, RUNTIME))
    with pytest.raises(ValueError, match="nothing was checked"):
        require_supported_minimum(make_app(tmp_path / "b", "15.1", {}))


def test_links_are_not_followed_out_of_the_app(tmp_path):
    outside = tmp_path / "outside.dylib"
    outside.write_bytes(thin(build_version("26.0")))
    app = make_app(tmp_path / "app", "15.1", RUNTIME)
    try:
        (app / "Contents/Frameworks").mkdir()
        (app / "Contents/Frameworks/link.dylib").symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    assert require_supported_minimum(app)[0] == (15, 1, 0)


def test_command_line_reports_and_sets_the_exit_status(tmp_path, capsys):
    good = make_app(tmp_path / "good", "15.1", RUNTIME)
    bad = make_app(tmp_path / "bad", "10.13", RUNTIME)
    assert main([str(good)]) == 0
    assert "LSMinimumSystemVersion 15.1" in capsys.readouterr().out
    assert main([str(bad)]) == 1
    assert "needs 15.1" in capsys.readouterr().err


def test_mac_build_stops_before_packaging_when_the_minimum_is_too_low(tmp_path):
    release = tmp_path / "release"
    make_app(release / "bundle/macos", "10.13", RUNTIME)
    with pytest.raises(SystemExit, match=r"macOS minimum version check failed for PlateFold\.app"):
        build_desktop.check_macos_app({"name": "macos"}, release, bundled=True)
    # Other platforms and unbundled Mac builds have no app to check.
    build_desktop.check_macos_app({"name": "linux"}, release, bundled=True)
    build_desktop.check_macos_app({"name": "macos"}, release, bundled=False)


def test_mac_build_prints_the_distribution_when_the_minimum_covers_it(tmp_path, capsys):
    release = tmp_path / "release"
    make_app(release / "bundle/macos", "15.1", RUNTIME)
    build_desktop.check_macos_app({"name": "macos"}, release, bundled=True)
    assert "LSMinimumSystemVersion 15.1" in capsys.readouterr().out


def test_tauri_declares_the_minimum_the_frozen_runtime_needs():
    # Tauri writes this to LSMinimumSystemVersion and MACOSX_DEPLOYMENT_TARGET.
    # The build check above proves it against the binaries on each Mac build.
    root = Path(__file__).parents[1]
    config = json.loads((root / "desktop/src-tauri/tauri.conf.json").read_text(encoding="utf-8"))
    assert config["bundle"]["macOS"]["minimumSystemVersion"] == "15.1"
