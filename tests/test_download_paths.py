"""Downloads follows OS user configuration, never the source or app version."""

from pathlib import Path

import pytest

from platefold import paths


@pytest.mark.parametrize(
    "platform,resolver",
    [("win32", "_windows_downloads"), ("darwin", "_macos_downloads"), ("linux", "_xdg_downloads")],
)
def test_os_resolved_redirected_downloads_is_created(tmp_path, monkeypatch, platform, resolver):
    redirected = tmp_path / "redirected profile" / "Téléchargements"
    monkeypatch.setattr(paths.sys, "platform", platform)
    monkeypatch.setattr(paths, resolver, lambda: redirected)
    assert paths.downloads_dir() == str(redirected)
    assert redirected.is_dir()


@pytest.mark.parametrize("missing", [None, Path("relative/downloads")])
def test_unresolvable_downloads_requires_explicit_selection(monkeypatch, missing):
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setattr(paths, "_xdg_downloads", lambda: missing)
    assert paths.downloads_dir() is None


def test_unavailable_registered_path_does_not_fall_back_to_home(tmp_path, monkeypatch):
    obstructed = tmp_path / "Downloads"
    obstructed.write_text("not a folder")
    monkeypatch.setattr(paths.sys, "platform", "linux")
    monkeypatch.setattr(paths, "_xdg_downloads", lambda: obstructed)
    assert paths.downloads_dir() is None


@pytest.mark.parametrize(
    "setting,suffix",
    [
        ('XDG_DOWNLOAD_DIR="$HOME/Téléchargements"', "Téléchargements"),
        ('XDG_DOWNLOAD_DIR="${HOME}/my downloads" # comment', "my downloads"),
    ],
)
def test_xdg_localized_user_folder_is_resolved(tmp_path, monkeypatch, setting, suffix):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    config = tmp_path / "settings"
    config.mkdir()
    (config / "user-dirs.dirs").write_text(setting, encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    assert paths._xdg_downloads() == tmp_path / suffix


def test_xdg_absolute_redirection(tmp_path):
    # Parsing only: nothing is created, so any absolute path will do.
    target = Path(tmp_path.anchor) / "other volume" / "Incoming"
    assert paths._xdg_download_value(f'XDG_DOWNLOAD_DIR="{target}"', tmp_path / "home") == target


@pytest.mark.parametrize(
    "setting",
    [
        'XDG_DOWNLOAD_DIR="$HOME"',
        'XDG_DOWNLOAD_DIR="relative"',
        'XDG_DOWNLOAD_DIR="$(touch /tmp/never-execute)"',
        'XDG_DOWNLOAD_DIR="$OTHER/Downloads"',
    ],
)
def test_disabled_or_shell_expression_directory_is_not_used(tmp_path, setting):
    assert paths._xdg_download_value(setting, tmp_path) is None


def test_windows_known_folder_api_uses_redirected_downloads_guid(monkeypatch):
    import ctypes
    import uuid
    from types import SimpleNamespace

    buffer = ctypes.create_unicode_buffer(r"D:\Profiles\Max\Incoming")
    freed = []

    class Function:
        def __init__(self, call):
            self.call = call

        def __call__(self, *args):
            return self.call(*args)

    def known_folder(folder_id, flags, token, result):
        assert (
            ctypes.string_at(folder_id, 16)
            == uuid.UUID("374DE290-123F-4565-9164-39C4925E467B").bytes_le
        )
        assert flags == 0x4000 and token is None
        ctypes.cast(result, ctypes.POINTER(ctypes.c_void_p))[0] = ctypes.addressof(buffer)
        return 0

    libraries = {
        "shell32": SimpleNamespace(SHGetKnownFolderPath=Function(known_folder)),
        "ole32": SimpleNamespace(CoTaskMemFree=Function(lambda value: freed.append(value.value))),
    }
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_: libraries[name], raising=False)
    assert paths._windows_downloads() == Path(r"D:\Profiles\Max\Incoming")
    assert freed == [ctypes.addressof(buffer)]
