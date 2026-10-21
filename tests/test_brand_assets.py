"""The brand palette is a test, not a paragraph.

The palette drifted from seven tokens to eight on 2026-09-03 and the artboard
stating the rule kept claiming seven; nothing noticed because nothing checked.
These tests read the token set from docs/stylesheets/brand.css, then fail on any
colour in a brand asset, stylesheet or the asset generator that isn't in it,
naming the file, line and colour.

Document themes are deliberately out of scope. They carry eighteen colour tokens
each, and the brand rule governs the mark and the documentation site only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BRAND_CSS = REPO / "docs" / "stylesheets" / "brand.css"
GENERATOR = REPO / "tools" / "build_brand_assets.py"
BRAND_DIR = REPO / "docs" / "assets" / "brand"
APP_ICON = REPO / "desktop" / "app.svg"

TOKEN_COUNT = 8
TOKEN_RE = re.compile(r"--pf-([a-z]+-\d+)\s*:\s*(#[0-9a-fA-F]{6})\s*;")
HEX_RE = re.compile(r"#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b")
FUNC_RE = re.compile(r"\b(rgba?|hsla?)\(\s*([^)]*)\)")
NAMED_RE = re.compile(
    # A bare word after a colour property; a word followed by "(" is a function.
    r"""(?:\b(?:fill|stroke|stop-color|flood-color|color)\s*[:=]\s*["']?)([a-zA-Z]+)\b(?!\s*\()"""
)

# Values that aren't colours in the palette sense.
KEYWORDS = {"none", "currentcolor", "transparent", "inherit", "initial", "unset"}

# One-off colours that are genuinely needed, each with its reason. An exemption
# nobody can explain is worse than a failing test, so every entry needs one.
# Keyed by (path relative to the repository, lowercase colour).
EXEMPTIONS: dict[tuple[str, str], str] = {}


def css_tokens() -> dict[str, str]:
    root = BRAND_CSS.read_text(encoding="utf-8")
    block = re.search(r":root\s*\{(.*?)\}", root, re.S)
    assert block, "brand.css has no :root block defining the palette"
    return {name: value.lower() for name, value in TOKEN_RE.findall(block.group(1))}


def generator_tokens() -> dict[str, str]:
    tree = ast.parse(GENERATOR.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "BRAND_TOKENS" for t in node.targets
        ):
            value = ast.literal_eval(node.value)
            return {name: colour.lower() for name, colour in value.items()}
    raise AssertionError("tools/build_brand_assets.py defines no BRAND_TOKENS")


def rgb_of(hex_colour: str) -> tuple[int, int, int]:
    value = hex_colour.lstrip("#")
    if len(value) in (3, 4):
        value = "".join(ch * 2 for ch in value[:3])
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def scanned_files() -> list[Path]:
    files = sorted(BRAND_DIR.rglob("*.svg"))
    files += sorted((REPO / "docs" / "stylesheets").glob("*.css"))
    files += [GENERATOR, APP_ICON]
    return files


def colour_violations(path: Path, palette: set[tuple[int, int, int]]) -> list[str]:
    rel = path.relative_to(REPO).as_posix()
    return text_violations(path.read_text(encoding="utf-8"), rel, palette)


def literals_in(line: str) -> list[tuple[str, tuple[int, ...] | None]]:
    """Every colour literal on a line, with its RGB channels when they can be read."""
    found: list[tuple[str, tuple[int, ...] | None]] = [
        (m.group(0), rgb_of(m.group(0))) for m in HEX_RE.finditer(line)
    ]
    for match in FUNC_RE.finditer(line):
        if match.group(1).startswith("hsl"):
            found.append((match.group(0), None))
            continue
        channels = [part.strip() for part in match.group(2).split(",")]
        try:
            rgb: tuple[int, ...] | None = tuple(int(float(c)) for c in channels[:3])
        except ValueError:
            rgb = None
        found.append((match.group(0), rgb))
    for match in NAMED_RE.finditer(line):
        if match.group(1).lower() not in KEYWORDS:
            found.append((match.group(1), None))
    return found


def text_violations(text: str, rel: str, palette: set[tuple[int, int, int]]) -> list[str]:
    """Return 'file:line: colour' for every colour literal outside the palette."""
    problems = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for literal, rgb in literals_in(line):
            if (rel, literal.lower()) in EXEMPTIONS:
                continue
            if rgb is not None and rgb in palette:
                continue
            problems.append(f"{rel}:{lineno}: {literal}")
    return problems


def test_palette_has_the_documented_token_count():
    tokens = css_tokens()
    assert len(tokens) == TOKEN_COUNT, (
        f"brand.css defines {len(tokens)} tokens: {sorted(tokens)}. A new token needs a job "
        "the others don't do; update TOKEN_COUNT and the brand guide together"
    )


def test_generator_uses_the_stylesheet_palette():
    assert generator_tokens() == css_tokens()


def test_every_exemption_has_a_reason():
    assert all(reason.strip() for reason in EXEMPTIONS.values())


@pytest.mark.parametrize("path", scanned_files(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_brand_colours_stay_in_the_palette(path):
    problems = colour_violations(path, palette_rgb())
    assert not problems, "colours outside the brand palette:\n" + "\n".join(problems)


def palette_rgb() -> set[tuple[int, int, int]]:
    return {rgb_of(value) for value in css_tokens().values()}


def test_check_catches_an_off_palette_colour():
    # The case that motivated this check: a stray slate grey in a mark asset.
    text = '<svg>\n<path stroke="#0F1D31"/>\n<path stroke="#3F4B5E" fill="red"/>\n</svg>'
    assert text_violations(text, "mark-idle.svg", palette_rgb()) == [
        "mark-idle.svg:3: #3F4B5E",
        "mark-idle.svg:3: red",
    ]


def test_translucent_token_is_allowed_and_translucent_stranger_is_not():
    text = "a { color: rgba(15, 29, 49, 0.5); }\nb { color: rgba(1, 2, 3, 0.5); }"
    assert text_violations(text, "probe.css", palette_rgb()) == ["probe.css:2: rgba(1, 2, 3, 0.5)"]


def test_keywords_are_not_colours():
    text = '<path fill="none" stroke="currentColor"/> a { color: var(--pf-navy-900); }'
    assert text_violations(text, "probe.svg", palette_rgb()) == []


def test_svg_assets_scale_and_stand_alone():
    for path in sorted(BRAND_DIR.rglob("*.svg")):
        text = path.read_text(encoding="utf-8")
        assert 'xmlns="http://www.w3.org/2000/svg"' in text, path.name
        assert "viewBox=" in text, path.name
        # Outlined text only: an asset must not depend on a font being installed.
        assert "<text" not in text, path.name
        assert "http" not in text.replace("http://www.w3.org/2000/svg", ""), path.name


def test_application_icon_and_tile_share_their_inks():
    def inks(path: Path) -> list[str]:
        return [m.lower() for m in HEX_RE.findall(path.read_text(encoding="utf-8"))]

    assert inks(APP_ICON) == inks(BRAND_DIR / "tile.svg")


def test_raster_icons_have_their_declared_sizes():
    from PIL import Image

    expected = {
        "apple-touch-icon.png": (180, 180),
        "icon-192.png": (192, 192),
        "icon-512.png": (512, 512),
        "og-image.png": (1200, 630),
        "social-banner.png": (1280, 640),
        "hero-banner.png": (1280, 400),
    }
    for name, size in expected.items():
        with Image.open(BRAND_DIR / name) as image:
            assert image.size == size, name
    with Image.open(BRAND_DIR / "favicon.ico") as icon:
        assert {(16, 16), (32, 32), (48, 48)} <= set(icon.info.get("sizes", set()))


def test_fonts_ship_with_their_licences():
    fonts = REPO / "docs" / "stylesheets" / "fonts"
    for face, licence in (("sora", "Sora-OFL.txt"), ("jetbrains-mono", "JetBrains-Mono-OFL.txt")):
        assert any(fonts.glob(f"{face}*.woff2")), face
        assert "SIL Open Font License" in (fonts / licence).read_text(encoding="utf-8")
