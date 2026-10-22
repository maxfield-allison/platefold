"""Public documents must describe the release the tools actually build.

The generated reference tables cannot drift from the code, but prose can: a
download name, a platform guide or a release-notes limit is written by hand.
These checks tie the hand-written claims that users act on to the code that
decides them: the release file rules in tools/assemble_release.py, the SVG
measurement in platefold/images.py and the font fallback report.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from platefold.images import ImageLoader
from tools import assemble_release as release

REPO = Path(__file__).resolve().parents[1]
DOCS = REPO / "docs"
PUBLIC_PROSE = sorted(
    [
        REPO / "README.md",
        REPO / "CHANGELOG.md",
        REPO / "SECURITY.md",
        REPO / "THEMING.md",
        REPO / "CONTRIBUTING.md",
        REPO / "packaging" / "linux" / "README.md",
        *(path for path in DOCS.rglob("*.md") if "samples" not in path.parts),
    ]
)
# A release download name as documents write it: a concrete version or <version>.
DOWNLOAD_NAME = re.compile(
    r"(?:PlateFold_|platefold[_-])(?:<version>|\d+\.\d+\.\d+)[\w.+-]*"
    r"\.(?:exe|zip|dmg|app\.tar\.gz|AppImage|deb|whl|tar\.gz)"
)
PLATFORM_PAGES = ("windows", "macos", "linux")


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def published(name: str) -> bool:
    for rule in release.RULES:
        version = re.search(r"\d+\.\d+\.\d+", name)
        concrete = name.replace("<version>", "1.0.0")
        escaped = re.escape(version.group(0) if version else "1.0.0")
        if re.fullmatch(rule.pattern.format(v=escaped), concrete):
            return True
    return False


def documented_download_names() -> list[tuple[str, str]]:
    return [
        (str(path.relative_to(REPO)), match.group(0))
        for path in PUBLIC_PROSE
        for match in DOWNLOAD_NAME.finditer(text(path))
    ]


def test_documents_name_release_downloads():
    names = {name for _, name in documented_download_names()}
    # The guard is only meaningful while documents actually name downloads.
    assert any(name.endswith(".dmg") for name in names)
    assert any(name.endswith(".deb") for name in names)
    assert any(name.endswith("-online-setup.exe") for name in names)


@pytest.mark.parametrize(("source", "name"), documented_download_names())
def test_every_documented_download_is_a_published_file_name(source, name):
    assert published(name), f"{source} names {name}, which no release publishes"


def test_release_notes_do_not_deny_a_package_the_release_requires():
    notes = text(REPO / "CHANGELOG.md")
    latest = next(
        section for section in notes.split("\n## [") if re.match(r"\d+\.\d+\.\d+\]", section)
    )
    limits = latest.split("### Known limits", 1)[1]
    denial = next(line for line in limits.splitlines() if re.search(r"\bNo .*packages", line))
    labels = {
        "linux-deb": "`.deb`",
        "linux-appimage": "AppImage",
        "macos-disk-image": "DMG",
        "windows-portable": "ZIP",
        "python-wheel": "wheel",
    }
    for kind, label in labels.items():
        assert kind in release.REQUIRED_KINDS
        assert label not in denial, f"Known limits deny {label}, which every release requires"


def test_svg_limit_is_online_only(tmp_path):
    svg = tmp_path / "wide.svg"
    svg.write_bytes(b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 400"></svg>')
    loader = ImageLoader()
    # Online keeps the column-width fallback; Print and Tiled print measure.
    assert loader.natural_size(str(svg))[0] == 0
    assert loader.print_size(str(svg)) == (1600, 400)
    unmeasured = re.compile(r"SVG[^.\n]*(?:aren't|are not|isn't|never) measured", re.IGNORECASE)
    for path in PUBLIC_PROSE:
        for line in text(path).splitlines():
            if unmeasured.search(line):
                assert "Online" in line, f"{path.relative_to(REPO)}: {line}"


def test_no_document_calls_font_fallback_silent():
    silent = re.compile(r"fall(?:s)? ?back[^.\n]*silently|silent(?:ly)? fall ?back", re.IGNORECASE)
    for path in PUBLIC_PROSE:
        assert not silent.search(text(path)), f"{path.relative_to(REPO)} calls fallback silent"


@pytest.mark.parametrize("page", PLATFORM_PAGES)
def test_platform_guide_covers_the_desktop_app_before_source(page):
    guide = text(DOCS / "get-started" / f"{page}.md")
    desktop = guide.find("## Install the desktop app")
    if page == "linux":
        desktop = guide.find("## Install the .deb")
        assert "## Use the AppImage" in guide
    source = guide.find("## Run from source")
    assert 0 <= desktop < source, (
        f"{page}.md must describe the desktop app before the source install"
    )
    route = guide[desktop:source].lower()
    for step in (
        "sha256sums",
        "first-export.md#in-the-desktop-app",
        "update",
        "remove" if page == "linux" else "uninstall",
    ):
        assert step in route, f"{page}.md desktop route has no {step!r}"
    assert "updates.json" in guide or "update check choices" in guide.lower()


def test_macos_guide_keeps_its_native_testing_limit():
    guide = text(DOCS / "get-started" / "macos.md")
    assert "hasn't been tested on macOS" in guide
    assert "Open Anyway" in guide  # named only to say not to use it
    assert "don't open it" in guide


def test_first_export_serves_desktop_users():
    guide = text(DOCS / "get-started" / "first-export.md")
    assert guide.index("## In the desktop app") < guide.index("## From the command line")
    assert "Run these from the repository folder" not in guide.split("## From the command line")[0]


def test_launch_facing_pages_make_no_prelaunch_claims():
    # Wording that is false once the release is signed and on PyPI.
    stale = re.compile(
        r"which Plate\|Fold isn't yet|Plate\|Fold (?:isn't|is not) (?:yet )?on PyPI"
        r"|signed or supported release\. There isn't one"
    )
    for path in PUBLIC_PROSE:
        match = stale.search(text(path))
        assert not match, f"{path.relative_to(REPO)}: {match.group(0) if match else ''}"
