"""Read font families exposed by the operating system, without crawling files."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from typing import Any, cast

MAX_FAMILIES = 4096
MAX_FAMILY_LENGTH = 128


def valid_family(value: str) -> bool:
    """A font choice is a family name, never a CSS fragment or file path."""
    return (
        0 < len(value) <= MAX_FAMILY_LENGTH
        and value == value.strip()
        and not any(c in value for c in "'\"\\,;{}<>/\n\r")
        and not any(unicodedata.category(c).startswith("C") for c in value)
    )


@dataclass(frozen=True)
class FontDiscovery:
    families: tuple[str, ...]
    available: bool


def _windows_families() -> list[str]:
    # GDI enumerates families available to this process, including current-user
    # installations; registry filenames and Office-private caches are not fonts.
    from ctypes import wintypes

    class LogFont(ctypes.Structure):
        _fields_ = (
            [
                (name, wintypes.LONG)
                for name in ("height", "width", "escapement", "orientation", "weight")
            ]
            + [
                (name, wintypes.BYTE)
                for name in (
                    "italic",
                    "underline",
                    "strikeout",
                    "charset",
                    "outprecision",
                    "clipprecision",
                    "quality",
                    "pitch",
                )
            ]
            + [("face", wintypes.WCHAR * 32)]
        )

    # Windows-only ctypes APIs are absent from the Linux type stubs.
    windows_ctypes = cast(Any, ctypes)
    gdi = windows_ctypes.WinDLL("gdi32", use_last_error=True)
    user = windows_ctypes.WinDLL("user32", use_last_error=True)
    user.GetDC.argtypes = [wintypes.HWND]
    user.GetDC.restype = wintypes.HDC
    user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    callback_type = windows_ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.POINTER(LogFont),
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_ssize_t,
    )
    gdi.EnumFontFamiliesExW.argtypes = [
        wintypes.HDC,
        ctypes.POINTER(LogFont),
        callback_type,
        ctypes.c_ssize_t,
        wintypes.DWORD,
    ]
    gdi.EnumFontFamiliesExW.restype = ctypes.c_int
    found: set[str] = set()

    @callback_type
    def collect(font, _metric, _kind, _data):
        family = font.contents.face
        if family and not family.startswith("@"):  # vertical aliases
            found.add(family)
        return int(len(found) < MAX_FAMILIES)

    dc = user.GetDC(None)
    if not dc:
        raise OSError("Cannot enumerate installed fonts")
    try:
        query = LogFont()
        query.charset = 1  # DEFAULT_CHARSET: every installed character set
        gdi.EnumFontFamiliesExW(dc, ctypes.byref(query), collect, 0, 0)
    finally:
        user.ReleaseDC(None, dc)
    return list(found)


def _macos_families() -> list[str]:
    core = ctypes.CDLL("/System/Library/Frameworks/CoreText.framework/CoreText")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    core.CTFontManagerCopyAvailableFontFamilyNames.restype = ctypes.c_void_p
    cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
    cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    names = core.CTFontManagerCopyAvailableFontFamilyNames()
    if not names:
        raise OSError("Cannot enumerate installed fonts")
    try:
        result = []
        for index in range(min(cf.CFArrayGetCount(names), MAX_FAMILIES)):
            value = cf.CFArrayGetValueAtIndex(names, index)
            buffer = ctypes.create_string_buffer(1024)
            if cf.CFStringGetCString(value, buffer, len(buffer), 0x08000100):  # UTF-8
                result.append(buffer.value.decode("utf-8"))
        return result
    finally:
        cf.CFRelease(names)


def _linux_families() -> list[str]:
    result = subprocess.run(
        ["fc-list", "--format=%{family}\\n"],
        capture_output=True,
        check=True,
        timeout=5,
        encoding="utf-8",
        errors="replace",
    )
    if len(result.stdout) > 2 * 1024 * 1024:
        raise OSError("Font catalog exceeded its size limit")
    return [family.strip() for line in result.stdout.splitlines() for family in line.split(",")]


def discover_installed_fonts() -> FontDiscovery:
    """Refresh on catalog requests so removed/added fonts do not stay cached."""
    try:
        if sys.platform == "win32":
            found = _windows_families()
        elif sys.platform == "darwin":
            found = _macos_families()
        else:
            found = _linux_families()
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return FontDiscovery((), False)
    # Fonts may have localized aliases; keep every safe OS-reported family.
    unique = {name.casefold(): name for name in found if valid_family(name)}
    return FontDiscovery(tuple(sorted(unique.values(), key=str.casefold)[:MAX_FAMILIES]), True)
