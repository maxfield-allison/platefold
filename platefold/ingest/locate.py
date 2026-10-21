"""Find operator-installed LibreOffice and Pandoc where their installers put them.

``PATH`` comes first, so an operator's explicit choice wins. The LibreOffice
installers for Windows and macOS do not add themselves to ``PATH``, and a
desktop app started from Finder gets a ``PATH`` without Homebrew, so the
standard install locations follow:

* Windows: ``%ProgramFiles%`` and ``%ProgramFiles(x86)%`` for LibreOffice, then
  the registry entries its installer writes (``App Paths``, ``UNO\\InstallPath``
  and the per-version ``Path``) in both hives and both registry views. Pandoc's
  per-user installer uses ``%LOCALAPPDATA%\\Pandoc``, its machine-wide one
  ``%ProgramFiles%\\Pandoc``.
* macOS: ``LibreOffice.app`` in ``/Applications`` or ``~/Applications``, and
  Pandoc from Homebrew (``/opt/homebrew``, ``/usr/local``) or MacPorts.
* Linux: ``PATH`` only. Distribution packages put ``soffice`` on it. Snap and
  Flatpak launchers are not searched: their sandboxes keep a private ``/tmp``
  and home, so a converter that answers ``--version`` may still be unable to
  read the private conversion workspace.

Only existence is decided here. Every candidate must still pass the
converter's version and capability probes before it is used.

Locations come from the real process environment, not the reduced environment
the converter itself runs in, which points ``LOCALAPPDATA`` into its workspace.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path

LIBREOFFICE_NAMES = ("soffice", "libreoffice")

# Registry locations, relative to HKEY_LOCAL_MACHINE and HKEY_CURRENT_USER.
APP_PATHS_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths"
LIBREOFFICE_KEY = r"SOFTWARE\LibreOffice"

# Absolute macOS locations, below `root` so tests can build a fake tree.
MACOS_APPLICATIONS = "Applications"
MACOS_PANDOC_DIRS = ("opt/homebrew/bin", "usr/local/bin", "opt/local/bin")
MACOS_SOFFICE = Path("LibreOffice.app", "Contents", "MacOS", "soffice")


def _platform() -> str:
    return sys.platform


def _root() -> Path:
    return Path("/")


def _home() -> Path:
    return Path.home()


def libreoffice(
    path: str | None,
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    root: Path | None = None,
) -> list[str]:
    """LibreOffice executables worth probing, most specific first."""
    platform = platform or _platform()
    environ = os.environ if environ is None else environ
    found: list[str | Path | None] = [shutil.which(name, path=path) for name in LIBREOFFICE_NAMES]
    if platform == "win32":
        found += [
            Path(environ[variable], "LibreOffice", "program", "soffice.exe")
            for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
            if environ.get(variable)
        ]
        found += registry_libreoffice()
    elif platform == "darwin":
        root = root or _root()
        home = home or _home()
        found += [root / MACOS_APPLICATIONS / MACOS_SOFFICE, home / "Applications" / MACOS_SOFFICE]
    return _existing(found, platform)


def pandoc(
    path: str | None,
    *,
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    root: Path | None = None,
) -> list[str]:
    """Pandoc executables worth probing, most specific first."""
    platform = platform or _platform()
    environ = os.environ if environ is None else environ
    found: list[str | Path | None] = [shutil.which("pandoc", path=path)]
    if platform == "win32":
        found += [
            Path(environ[variable], "Pandoc", "pandoc.exe")
            for variable in ("LOCALAPPDATA", "ProgramFiles", "ProgramW6432", "ProgramFiles(x86)")
            if environ.get(variable)
        ]
    elif platform == "darwin":
        root = root or _root()
        found += [root / directory / "pandoc" for directory in MACOS_PANDOC_DIRS]
    return _existing(found, platform)


def registry_libreoffice() -> list[Path]:
    """soffice.exe locations recorded by LibreOffice's Windows installer."""
    if sys.platform != "win32":
        return []
    import winreg

    found: list[Path] = []
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            executable = _registry_value(hive, APP_PATHS_KEY + r"\soffice.exe", "", view)
            if executable:
                found.append(Path(executable))
            program = _registry_value(hive, LIBREOFFICE_KEY + r"\UNO\InstallPath", "", view)
            if program:
                found.append(Path(program, "soffice.exe"))
            versions = LIBREOFFICE_KEY + r"\LibreOffice"
            for version in _registry_subkeys(hive, versions, view):
                executable = _registry_value(hive, versions + "\\" + version, "Path", view)
                if executable:
                    found.append(Path(executable))
    return found


def _registry_value(hive, key: str, name: str, view: int) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
            value, kind = winreg.QueryValueEx(handle, name)
    except OSError:
        return None
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(value, str):
        return None
    if kind == winreg.REG_EXPAND_SZ:
        value = winreg.ExpandEnvironmentStrings(value)
    return value.strip().strip('"') or None


def _registry_subkeys(hive, key: str, view: int) -> list[str]:
    if sys.platform != "win32":
        return []
    import winreg

    names: list[str] = []
    try:
        with winreg.OpenKey(hive, key, 0, winreg.KEY_READ | view) as handle:
            index = 0
            while True:
                try:
                    names.append(winreg.EnumKey(handle, index))
                except OSError:
                    return sorted(names, reverse=True)
                index += 1
    except OSError:
        return []


def _existing(candidates: Iterable[str | Path | None], platform: str) -> list[str]:
    """Resolved, de-duplicated candidates that are runnable files."""
    seen: dict[str, None] = {}
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(candidate)
        try:
            if not path.is_file():
                continue
            if platform != "win32" and not os.access(path, os.X_OK):
                continue
            resolved = str(path.resolve())
        except OSError:
            continue
        seen.setdefault(resolved)
    return list(seen)
