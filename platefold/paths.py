"""Read-only package resources and user-writable application directories."""

from __future__ import annotations

import contextlib
import os
import re
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

APP_NAME = "platefold"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def resource_root() -> str:
    return str(Path(__file__).resolve().parent)


def app_dir() -> str:
    return str(Path(sys.executable).parent) if is_frozen() else resource_root()


def config_dir() -> str:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or home / "AppData/Roaming")
    elif sys.platform == "darwin":
        base = home / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        if not base.is_absolute():
            base = home / ".config"
    return str(base / APP_NAME)


def user_theme_dir() -> Path:
    return Path(config_dir()) / "themes"


def cache_dir() -> str:
    home = Path.home()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData/Local")
    elif sys.platform == "darwin":
        base = home / "Library/Caches"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or home / ".cache")
        if not base.is_absolute():
            base = home / ".cache"
    return str(base / APP_NAME / "diagram-cache")


def _windows_downloads() -> Path | None:
    """Windows Known Folders honors user/administrator Downloads redirection."""
    import ctypes
    import uuid

    # WinDLL exists only on Windows; this resolver is selected only there.
    loader = getattr(ctypes, "WinDLL", None)
    if loader is None:
        return None
    shell = loader("shell32", use_last_error=True)
    ole = loader("ole32")
    folder_id = ctypes.create_string_buffer(
        uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le
    )
    result = ctypes.c_void_p()
    shell.SHGetKnownFolderPath.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    shell.SHGetKnownFolderPath.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    # DONT_VERIFY lets us create a deleted Downloads folder after resolution;
    # it still reads the user's actual known-folder registration.
    status = shell.SHGetKnownFolderPath(folder_id, 0x4000, None, ctypes.byref(result))
    try:
        if status != 0 or not result.value:
            return None
        return Path(ctypes.wstring_at(result.value))
    finally:
        if result.value:
            ole.CoTaskMemFree(result)


def _macos_downloads() -> Path | None:
    """Ask Foundation for NSDownloadsDirectory in the current user domain."""
    import ctypes

    foundation = ctypes.CDLL("/System/Library/Frameworks/Foundation.framework/Foundation")
    cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_autoreleasePoolPush.restype = ctypes.c_void_p
    objc.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
    objc.objc_autoreleasePoolPop.restype = None
    foundation.NSSearchPathForDirectoriesInDomains.argtypes = [
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_bool,
    ]
    foundation.NSSearchPathForDirectoriesInDomains.restype = ctypes.c_void_p
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
    pool = objc.objc_autoreleasePoolPush()
    try:
        values = foundation.NSSearchPathForDirectoriesInDomains(15, 1, True)
        if not values or cf.CFArrayGetCount(values) == 0:
            return None
        value = cf.CFArrayGetValueAtIndex(values, 0)
        buffer = ctypes.create_string_buffer(8192)
        if not cf.CFStringGetCString(value, buffer, len(buffer), 0x08000100):
            return None
        return Path(buffer.value.decode("utf-8"))
    finally:
        objc.objc_autoreleasePoolPop(pool)


def _xdg_downloads() -> Path | None:
    """Read localized/redirected XDG user directories without executing shell."""
    home = Path.home()
    config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    if not config.is_absolute():
        config = home / ".config"
    path = config / "user-dirs.dirs"
    try:
        with path.open(encoding="utf-8") as stream:
            text = stream.read(65537)
    except FileNotFoundError:
        return home / "Downloads"
    if len(text) > 65536:
        return None
    return _xdg_download_value(text, home)


def _xdg_download_value(text: str, home: Path) -> Path | None:
    import shlex

    value = None
    for line in text.splitlines():
        key, separator, raw = line.partition("=")
        if not separator or key.strip() != "XDG_DOWNLOAD_DIR":
            continue
        pieces = shlex.split(raw, comments=True)
        if len(pieces) != 1:
            return None
        value = pieces[0]
    if value is None:
        return home / "Downloads"
    # Refuse any other variable or command substitution before HOME is put in:
    # a home folder may itself contain "$".
    rest = value
    for prefix in ("$HOME", "${HOME}"):
        if value == prefix or value.startswith(prefix + "/"):
            rest = value[len(prefix) :]
            value = str(home) + rest
            break
    if not value or "$" in rest or "`" in rest:
        return None
    resolved = Path(value)
    # XDG uses HOME to indicate a disabled user directory. Do not silently
    # export into Home when the user has disabled Downloads.
    return resolved if resolved.is_absolute() and resolved != home else None


def downloads_dir() -> str | None:
    """The OS user's Downloads folder, or None when selection is required.

    Resolves afresh at application launch, independently of application version.
    No source-relative or home-directory fallback is used after an OS failure.
    """
    try:
        if sys.platform == "win32":
            path = _windows_downloads()
        elif sys.platform == "darwin":
            path = _macos_downloads()
        else:
            path = _xdg_downloads()
        if path is None or not path.is_absolute():
            return None
        path.mkdir(parents=True, exist_ok=True)
        return str(path) if path.is_dir() else None
    except (OSError, ValueError, AttributeError, UnicodeError):
        return None


# A root is replaced only as a whole path segment: "/home/al" leaves
# "/home/alice" and "/var/home/al" alone.
_SEGMENT_BEFORE = r"(?<![\w.~-])"
_SEGMENT_AFTER = r"(?![\w-])"


def _spellings(root: str | os.PathLike[str]) -> set[str]:
    """How one directory can appear in a message: as given, resolved, either slash."""
    text = os.fspath(root)
    names = {text}
    if os.path.isabs(text):
        with contextlib.suppress(OSError, ValueError):
            names.add(os.path.realpath(text))
    names |= {name.replace("\\", "/") for name in names}
    return {
        name.rstrip("/\\")
        for name in names
        # A filesystem root or drive carries no information and would match
        # every absolute path.
        if name and Path(name).parent != Path(name)
    }


def _root_pattern(root: str) -> str:
    """Match one root even when a Windows producer mixes slash styles."""
    if sys.platform != "win32":
        return re.escape(root)
    # Windows APIs and Python libraries can describe the same path with either
    # separator, or even mix them in one diagnostic.  Treat separators as the
    # path syntax they are instead of requiring one spelling for the full root.
    # ``repr(path)`` doubles backslashes inside an exception or warning.  Match
    # that spelling too; the matched root is normalized again for label lookup.
    separator = "(?:/|\\\\+)"
    return "".join(separator if char in "/\\" else re.escape(char) for char in root)


def _humanize_redacted_windows_path(original: str, redacted: str) -> str:
    if sys.platform != "win32" or redacted == original:
        return redacted
    # Exception text uses repr(path), so separators after the replaced root
    # remain doubled unless they are humanized too.
    return re.sub(r"\\{2,}", lambda _match: "\\", redacted)


def redact_host_paths(value: Any, roots: Iterable[tuple[str | os.PathLike[str], str]]) -> Any:
    """Replace the given host directories in every string of ``value`` with labels.

    Reports and diagnostics that leave the machine should say where something
    was relative to a folder the reader knows about (``<output>/notes.pdf``,
    ``~/themes``) without disclosing the user name, home, project layout or
    temporary locations behind it. Longer roots win, so a folder inside the
    home directory keeps its own label. Windows paths match case-insensitively.
    """
    labels: dict[str, str] = {}
    for root, label in roots:
        for spelling in _spellings(root):
            labels.setdefault(spelling, label)
    if not labels:
        return value
    ordered = sorted(labels, key=len, reverse=True)
    pattern = re.compile(
        _SEGMENT_BEFORE
        + "(?:"
        + "|".join(_root_pattern(name) for name in ordered)
        + ")"
        + _SEGMENT_AFTER,
        re.IGNORECASE if sys.platform == "win32" else 0,
    )

    def windows_key(name: str) -> str:
        return re.sub(r"[\\/]+", "/", name).casefold()

    folded = {windows_key(name): label for name, label in labels.items()}

    def label_for(match: re.Match[str]) -> str:
        found = match[0]
        return labels.get(found) or folded.get(windows_key(found), found)

    def redact(item: Any) -> Any:
        if isinstance(item, str):
            return _humanize_redacted_windows_path(item, pattern.sub(label_for, item))
        if isinstance(item, dict):
            return {redact(key): redact(entry) for key, entry in item.items()}
        if isinstance(item, list | tuple):
            return type(item)(redact(entry) for entry in item)
        return item

    return redact(value)
