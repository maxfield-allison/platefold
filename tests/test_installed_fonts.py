"""Installed catalogs are bounded; saved choices survive removal safely."""

import subprocess

import pytest

from platefold import installed_fonts
from platefold.config import make_settings
from platefold.server_models import ExportOptions
from platefold.theme import available_font_sets, get_font_set
from platefold.worker import request_from


@pytest.mark.parametrize(
    "platform,reader",
    [
        ("win32", "_windows_families"),
        ("darwin", "_macos_families"),
        ("linux", "_linux_families"),
    ],
)
def test_platform_catalog_deduplicates_and_rejects_css(monkeypatch, platform, reader):
    monkeypatch.setattr(installed_fonts.sys, "platform", platform)
    monkeypatch.setattr(
        installed_fonts, reader, lambda: ["Calibri", "calibri", "Noto 日本語", "bad; color:red", ""]
    )
    result = installed_fonts.discover_installed_fonts()
    assert result.available
    assert result.families == ("calibri", "Noto 日本語")
    entries = available_font_sets()
    assert any(font.name == "system:Noto 日本語" for font in entries)
    assert any(font.name == "sora" for font in entries)


def test_missing_font_discovery_is_nonfatal(monkeypatch):
    monkeypatch.setattr(installed_fonts.sys, "platform", "linux")

    def absent():
        raise FileNotFoundError("fc-list")

    monkeypatch.setattr(installed_fonts, "_linux_families", absent)
    assert not installed_fonts.discover_installed_fonts().available
    assert any(font.name == "sora" for font in available_font_sets())


def test_linux_command_is_bounded_and_lists_user_font_aliases(monkeypatch):
    def run(command, **kwargs):
        assert command == ["fc-list", "--format=%{family}\\n"]
        assert kwargs["timeout"] == 5 and kwargs["check"]
        return subprocess.CompletedProcess(command, 0, "Family,Localized\nUser Font\n")

    monkeypatch.setattr(subprocess, "run", run)
    assert installed_fonts._linux_families() == ["Family", "Localized", "User Font"]


def test_saved_removed_font_keeps_request_and_bundled_fallback():
    fonts = get_font_set("system:Previously Installed")
    assert fonts.body.startswith("'Previously Installed', Sora,")
    assert "JetBrains Mono" in fonts.mono
    assert make_settings(font_set=fonts.name).fonts == fonts
    assert ExportOptions(font_set=fonts.name).font_set == fonts.name


@pytest.mark.parametrize(
    "value", ["system:", "system:A; color:red", "system:A\nB", "system:" + "A" * 129]
)
def test_arbitrary_css_cannot_become_font_choice(value):
    with pytest.raises(ValueError):
        get_font_set(value)
    with pytest.raises(ValueError):
        ExportOptions(font_set=value)


def test_worker_source_font_override_and_browser_contract(tmp_path):
    source = tmp_path / "source.md"
    source.write_text("# Example")
    message = {
        "input_path": str(source),
        "output_dir": str(tmp_path / "out"),
        "browser": "system",
        "settings": {"theme": "source", "font_set": "system:Local Font"},
    }
    request = request_from(message)
    assert request.browser == "system"
    assert not request.settings.source_fonts
    assert request.settings.fonts.name == "system:Local Font"
    message["browser"] = "/untrusted/executable"
    with pytest.raises(ValueError, match="renderer"):
        request_from(message)
