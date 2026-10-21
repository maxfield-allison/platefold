"""Finding LibreOffice and Pandoc where their installers put them.

The layouts are built as fake installs in temporary directories; the platform,
filesystem root, home directory and environment are passed in or patched, so
Windows and macOS discovery is exercised on any host. The registry test runs
only on Windows, against keys it creates under HKEY_CURRENT_USER.
"""

from __future__ import annotations

import contextlib
import sys
import uuid
from pathlib import Path

import pytest

from platefold.config import Settings
from platefold.ingest import Ingested, converter, locate


def install(path: Path, body: str = "") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body or "#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def test_path_is_searched_first(tmp_path):
    # ``shutil.which("soffice")`` applies PATHEXT on a real Windows host, so
    # the fake executable needs the same suffix the installer would provide.
    name = "soffice.exe" if sys.platform == "win32" else "soffice"
    on_path = install(tmp_path / "bin" / name)
    program_files = tmp_path / "Program Files"
    install(program_files / "LibreOffice" / "program" / "soffice.exe")
    found = locate.libreoffice(
        str(on_path.parent), platform="win32", environ={"ProgramFiles": str(program_files)}
    )
    assert found[0] == str(on_path.resolve())
    assert len(found) == 2


@pytest.mark.parametrize("variable", ["ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"])
def test_windows_program_files_installs_are_found(tmp_path, variable):
    soffice = install(tmp_path / variable / "LibreOffice" / "program" / "soffice.exe")
    environ = {variable: str(tmp_path / variable)}
    assert locate.libreoffice("", platform="win32", environ=environ) == [str(soffice.resolve())]


def test_windows_pandoc_per_user_install_comes_before_machine_wide(tmp_path):
    per_user = install(tmp_path / "Local" / "Pandoc" / "pandoc.exe")
    machine = install(tmp_path / "Program Files" / "Pandoc" / "pandoc.exe")
    environ = {
        "LOCALAPPDATA": str(tmp_path / "Local"),
        "ProgramFiles": str(tmp_path / "Program Files"),
    }
    found = locate.pandoc("", platform="win32", environ=environ)
    assert found == [str(per_user.resolve()), str(machine.resolve())]


def test_macos_applications_folders_are_found(tmp_path):
    root, home = tmp_path / "root", tmp_path / "home"
    system = install(root / "Applications" / locate.MACOS_SOFFICE)
    personal = install(home / "Applications" / locate.MACOS_SOFFICE)
    found = locate.libreoffice("", platform="darwin", environ={}, root=root, home=home)
    assert found == [str(system.resolve()), str(personal.resolve())]


def test_macos_pandoc_from_homebrew_and_macports(tmp_path):
    root = tmp_path / "root"
    arm = install(root / "opt" / "homebrew" / "bin" / "pandoc")
    intel = install(root / "usr" / "local" / "bin" / "pandoc")
    ports = install(root / "opt" / "local" / "bin" / "pandoc")
    found = locate.pandoc("", platform="darwin", environ={}, root=root)
    assert found == [str(arm.resolve()), str(intel.resolve()), str(ports.resolve())]


def test_linux_searches_path_only(tmp_path):
    root = tmp_path / "root"
    install(root / "Applications" / locate.MACOS_SOFFICE)
    install(root / "usr" / "local" / "bin" / "pandoc")
    environ = {"ProgramFiles": str(tmp_path)}
    install(tmp_path / "LibreOffice" / "program" / "soffice.exe")
    assert locate.libreoffice("", platform="linux", environ=environ, root=root) == []
    assert locate.pandoc("", platform="linux", environ=environ, root=root) == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute permission")
def test_files_that_cannot_run_are_skipped(tmp_path):
    root = tmp_path / "root"
    install(root / "opt" / "homebrew" / "bin" / "pandoc").chmod(0o600)
    assert locate.pandoc("", platform="darwin", environ={}, root=root) == []


@pytest.mark.skipif(sys.platform == "win32", reason="symbolic links need privileges on Windows")
def test_one_install_reached_twice_is_probed_once(tmp_path):
    root = tmp_path / "root"
    target = install(root / "opt" / "homebrew" / "bin" / "pandoc")
    (root / "usr" / "local" / "bin").mkdir(parents=True)
    (root / "usr" / "local" / "bin" / "pandoc").symlink_to(target)
    assert locate.pandoc("", platform="darwin", environ={}, root=root) == [str(target.resolve())]


def fake_soffice(path: Path) -> Path:
    return install(
        path,
        f"""#!{sys.executable}
import pathlib, sys
args = sys.argv[1:]
if "--version" in args:
    print("LibreOffice 25.8.1.1")
    raise SystemExit(0)
out = pathlib.Path(args[args.index("--outdir") + 1])
source = pathlib.Path(args[-1])
(out / f"{{source.stem}}.docx").write_bytes(b"converted-docx")
""",
    )


@pytest.mark.skipif(sys.platform == "win32", reason="the fake install is a POSIX script")
def test_a_standard_macos_install_converts_without_path(tmp_path, monkeypatch):
    root = tmp_path / "root"
    soffice = fake_soffice(root / "Applications" / locate.MACOS_SOFFICE)
    monkeypatch.setattr(locate, "_platform", lambda: "darwin")
    monkeypatch.setattr(locate, "_root", lambda: root)
    monkeypatch.setattr(locate, "_home", lambda: tmp_path / "home")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        converter,
        "ingest_docx",
        lambda path, settings, cancel_check=None: Ingested(body_html="<p>Proof</p>"),
    )
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"synthetic-doc")

    result = converter.ingest(str(source), Settings())

    assert result.conversion["name"] == "libreoffice"
    assert result.conversion["version"] == "LibreOffice 25.8.1.1"
    assert soffice.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the fake install is a POSIX script")
def test_a_broken_first_candidate_falls_through_to_the_next(tmp_path, monkeypatch):
    root = tmp_path / "root"
    install(root / "Applications" / locate.MACOS_SOFFICE, "#!/bin/sh\nexit 3\n")
    fake_soffice(tmp_path / "home" / "Applications" / locate.MACOS_SOFFICE)
    monkeypatch.setattr(locate, "_platform", lambda: "darwin")
    monkeypatch.setattr(locate, "_root", lambda: root)
    monkeypatch.setattr(locate, "_home", lambda: tmp_path / "home")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(
        converter,
        "ingest_docx",
        lambda path, settings, cancel_check=None: Ingested(body_html="<p>Proof</p>"),
    )
    source = tmp_path / "legacy.odt"
    source.write_bytes(b"synthetic-odt")

    result = converter.ingest(str(source), Settings())

    assert result.conversion["version"] == "LibreOffice 25.8.1.1"


@pytest.mark.skipif(sys.platform != "win32", reason="reads the Windows registry")
def test_windows_registry_entries_are_found(tmp_path, monkeypatch):
    import winreg

    base = rf"SOFTWARE\PlateFoldTest-{uuid.uuid4().hex}"
    monkeypatch.setattr(locate, "APP_PATHS_KEY", base + r"\App Paths")
    monkeypatch.setattr(locate, "LIBREOFFICE_KEY", base + r"\LibreOffice")
    app_path = install(tmp_path / "app" / "soffice.exe")
    uno = install(tmp_path / "uno" / "soffice.exe")
    versioned = install(tmp_path / "versioned" / "soffice.exe")
    created: list[str] = []

    def write(key: str, name: str, value: str, kind: int = winreg.REG_SZ) -> None:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_WRITE) as handle:
            winreg.SetValueEx(handle, name, 0, kind, value)
        created.append(key)

    try:
        write(base + r"\App Paths\soffice.exe", "", f'"{app_path}"')
        write(base + r"\LibreOffice\UNO\InstallPath", "", str(uno.parent))
        write(base + r"\LibreOffice\LibreOffice\25.8", "Path", str(versioned))
        found = [str(path.resolve()) for path in locate.registry_libreoffice()]
        for expected in (app_path, uno, versioned):
            assert str(expected.resolve()) in found
        # Nothing on PATH and no Program Files: the registry alone finds it.
        assert str(app_path.resolve()) in locate.libreoffice("", platform="win32", environ={})
    finally:
        keys = {base}
        for key in created:
            parts = key.split("\\")
            keys.update(
                "\\".join(parts[:i]) for i in range(len(base.split("\\")) + 1, len(parts) + 1)
            )
        for key in sorted(keys, key=len, reverse=True):
            with contextlib.suppress(OSError):
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
