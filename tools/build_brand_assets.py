#!/usr/bin/env python3
"""Generate the Plate|Fold brand assets from one palette definition.

Every colour in every generated asset comes from ``BRAND_TOKENS`` below, which is
the same eight-token set declared in ``docs/stylesheets/brand.css``. That is the
point: ``tests/test_brand_assets.py`` reads the CSS, reads the SVGs, and fails on
anything outside the set. A generator plus a test is why the palette cannot drift
the way it did on 2026-09-03, when it grew an eighth token and the artboard that
stated the rule kept claiming seven.

Wordmark text is converted to outlines with fontTools, so no delivered asset
depends on a font being installed or fetched. The source face is the same Sora
the package already bundles for its ``sora`` font set, under SIL OFL 1.1.

Rasterisation uses cairosvg. Both it and fontTools are documentation-build
dependencies only; see ``requirements-docs.txt``. Application code does not
import either.

Usage:
    python tools/build_brand_assets.py            # write docs/assets/brand/
    python tools/build_brand_assets.py --check    # fail if anything would change
"""

from __future__ import annotations

import argparse
import filecmp
import io
import os
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BRAND_DIR = os.path.join(REPO, "docs", "assets", "brand")
SOURCE_DIR = os.path.join(BRAND_DIR, "source")
FONT = os.path.join(REPO, "docs", "stylesheets", "fonts", "sora-variable.woff2")

# ---------------------------------------------------------------- the palette

# Eight tokens. Grounds are shared with dnsweaver, which is what makes the two
# projects read as a family; the inks rotate dnsweaver's cyan and teal into
# amber and ember. Contrast measured against paper-50 and navy-900 in
# docs/brand.md; amber needs two dark steps because 700 reaches only 3.92 on
# paper, which clears the 3.0 graphics bar and not the 4.5 body-text one.
BRAND_TOKENS = {
    "navy-950": "#0A1422",
    "navy-900": "#0F1D31",
    "paper-50": "#FAF7F2",
    "amber-400": "#F2B457",
    "ember-400": "#EF7B55",
    "amber-700": "#A9700E",
    "ember-700": "#B4482A",
    "amber-800": "#845407",
}
T = BRAND_TOKENS

TAGLINE = "Documents to PDF with every figure on a page that fits"
WORDMARK = "Plate|Fold"

# ------------------------------------------------------------------- the mark

# Two leaves on one crease, from the 2026-09-03 design: the tall amber leaf is
# the body page, the wide ember leaf is the plate. 64px grid, stroke 5.5, round
# caps and joins. Geometry is reproduced from the design archive unchanged.
CREASE = "M32 8 V56"
LEAF_BODY = "M32 14 H17 a4 4 0 0 0 -4 4 V46 a4 4 0 0 0 4 4 H32"
LEAF_PLATE = "M32 21 H51 a4 4 0 0 1 4 4 V39 a4 4 0 0 1 -4 4 H32"


def mark_paths(crease_ink: str, plate_ink: str) -> str:
    common = 'stroke-width="5.5" fill="none" stroke-linecap="round" stroke-linejoin="round"'
    return (
        f'<path d="{CREASE}" stroke="{crease_ink}" {common}/>'
        f'<path d="{LEAF_BODY}" stroke="{crease_ink}" {common}/>'
        f'<path d="{LEAF_PLATE}" stroke="{plate_ink}" {common}/>'
    )


BRIGHT = (T["amber-400"], T["ember-400"])  # on navy grounds
DEEP = (T["amber-700"], T["ember-700"])  # on paper grounds


def mark_svg(inks: tuple[str, str]) -> str:
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">{mark_paths(*inks)}</svg>'


def mark_mono_svg() -> str:
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
        f"{mark_paths('currentColor', 'currentColor')}</svg>"
    )


# The tile is the shipped application icon (desktop/app.svg), reproduced here at
# the 64 grid. Solid leaves rather than strokes, because the stroked mark loses
# its crease below about 20px. Anything smaller than that uses this.
TILE_BODY = 'x="14.5" y="12" width="14.75" height="40" rx="2"'
TILE_PLATE = 'x="34.25" y="20" width="15.25" height="24" rx="2"'


def tile_svg(size: int = 64) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="{size}"'
        f' height="{size}"><rect width="64" height="64" rx="13" fill="{T["navy-900"]}"/>'
        f'<rect {TILE_BODY} fill="{T["amber-400"]}"/>'
        f'<rect {TILE_PLATE} fill="{T["ember-400"]}"/></svg>'
    )


# ------------------------------------------------------- wordmark as outlines


def _instance(weight: int):
    from fontTools.ttLib import TTFont
    from fontTools.varLib import instancer

    font = TTFont(FONT)
    return instancer.instantiateVariableFont(font, {"wght": weight}, inplace=False)


def text_path(
    text: str, size: float, weight: int = 600, tracking: float = -0.015
) -> tuple[str, float]:
    """Return (SVG path data, advance width) for `text` set at `size` px.

    The baseline sits at y=0 and the text starts at x=0. `tracking` is in em,
    matching the CSS letter-spacing the brand guide specifies for the wordmark.
    """
    from fontTools.misc.transform import Transform
    from fontTools.pens.svgPathPen import SVGPathPen
    from fontTools.pens.transformPen import TransformPen

    font = _instance(weight)
    upem = font["head"].unitsPerEm
    scale = size / upem
    cmap = font.getBestCmap()
    glyphset = font.getGlyphSet()
    hmtx = font["hmtx"]

    pen = SVGPathPen(glyphset, ntos=lambda v: f"{v:.2f}")
    x = 0.0
    for char in text:
        name = cmap.get(ord(char))
        if name is None:
            raise SystemExit(f"font has no glyph for {char!r}")
        # Flip Y: font units go up, SVG user units go down.
        tpen = TransformPen(pen, Transform(scale, 0, 0, -scale, x, 0))
        glyphset[name].draw(tpen)
        x += hmtx[name][0] * scale + size * tracking
    return pen.getCommands(), x - size * tracking


def _metrics(size: float, weight: int = 600) -> tuple[float, float]:
    font = _instance(weight)
    upem = font["head"].unitsPerEm
    os2 = font["OS/2"]
    return os2.sTypoAscender / upem * size, -os2.sTypoDescender / upem * size


# ----------------------------------------------------------------- lockups

MARK_GAP = 1 / 3  # of the mark's height, per the brand guide


def lockup_h(inks: tuple[str, str], ink_text: str) -> str:
    mark = 64.0
    size = 40.0
    gap = mark * MARK_GAP
    path, width = text_path(WORDMARK, size)
    ascent, _ = _metrics(size)
    x = mark + gap
    baseline = 32 + ascent / 2 - size * 0.06
    total = x + width
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total:.0f} 64">'
        f"{mark_paths(*inks)}"
        f'<path transform="translate({x:.2f} {baseline:.2f})" fill="{ink_text}" d="{path}"/>'
        "</svg>"
    )


def lockup_stack(inks: tuple[str, str], ink_text: str) -> str:
    size = 32.0
    path, width = text_path(WORDMARK, size)
    ascent, descent = _metrics(size)
    total_w = max(width, 64.0)
    mark_x = (total_w - 64) / 2
    baseline = 64 + 18 + ascent
    height = baseline + descent
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total_w:.0f} {height:.0f}">'
        f'<g transform="translate({mark_x:.2f} 0)">{mark_paths(*inks)}</g>'
        f'<path transform="translate({(total_w - width) / 2:.2f} {baseline:.2f})"'
        f' fill="{ink_text}" d="{path}"/>'
        "</svg>"
    )


def lockup_tagline(inks: tuple[str, str], ink_text: str, ink_tag: str) -> str:
    mark = 64.0
    size = 38.0
    tag_size = 15.0
    gap = mark * MARK_GAP
    path, width = text_path(WORDMARK, size)
    tag_path, tag_width = text_path(TAGLINE, tag_size, weight=400, tracking=0.0)
    x = mark + gap
    total = x + max(width, tag_width)
    word_baseline = 30.0
    tag_baseline = 52.0
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {total:.0f} 64">'
        f"{mark_paths(*inks)}"
        f'<path transform="translate({x:.2f} {word_baseline:.2f})" fill="{ink_text}" d="{path}"/>'
        f'<path transform="translate({x:.2f} {tag_baseline:.2f})" fill="{ink_tag}" d="{tag_path}"/>'
        "</svg>"
    )


# ------------------------------------------------------------- print motifs


def crop_marks(w: float, h: float, ink: str, gap: float = 10.0, tick: float = 18.0) -> str:
    """Four corner crop marks, drawn inside the page but never touching it.

    From the design's motif set: a hairline that says "this will be cut here".
    Used on banners around the whole composition, never around a button.
    """
    a = f'stroke="{ink}" stroke-width="1.5" fill="none"'
    parts = []
    for cx, sx in ((gap, 1), (w - gap, -1)):
        for cy, sy in ((gap, 1), (h - gap, -1)):
            parts.append(f'<path d="M{cx} {cy} h{sx * tick}" {a}/>')
            parts.append(f'<path d="M{cx} {cy} v{sy * tick}" {a}/>')
    return "".join(parts)


# ------------------------------------------------------------------- banners


def banner(w: int, h: int, *, with_tagline: bool, with_pills: bool) -> str:
    mark_scale = h / 64 * 0.30
    mark_x = w * 0.085
    mark_y = h * 0.24
    size = h * 0.135
    path, _width = text_path(WORDMARK, size)
    gap = 64 * mark_scale * MARK_GAP
    text_x = mark_x + 64 * mark_scale + gap
    baseline = mark_y + 64 * mark_scale * 0.72

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
        f'<rect width="{w}" height="{h}" fill="{T["navy-950"]}"/>',
        crop_marks(w, h, T["ember-400"], gap=h * 0.055, tick=h * 0.045),
        f'<g transform="translate({mark_x:.1f} {mark_y:.1f}) scale({mark_scale:.4f})">'
        f"{mark_paths(*BRIGHT)}</g>",
        f'<path transform="translate({text_x:.1f} {baseline:.1f})" fill="{T["paper-50"]}" d="{path}"/>',
    ]
    if with_tagline:
        tag_size = h * 0.055
        tag_path, _ = text_path(TAGLINE, tag_size, weight=400, tracking=0.0)
        out.append(
            f'<path transform="translate({mark_x:.1f} {h * 0.62:.1f})"'
            f' fill="{T["amber-400"]}" d="{tag_path}"/>'
        )
    if with_pills:
        pill_size = h * 0.038
        y = h * 0.78
        x = mark_x
        for label in ("vector Mermaid", "per-figure pages", "offline exports"):
            p, pw = text_path(label, pill_size, weight=500, tracking=0.0)
            pad = pill_size * 0.9
            out.append(
                f'<rect x="{x:.1f}" y="{y - pill_size:.1f}" width="{pw + pad * 2:.1f}"'
                f' height="{pill_size * 2.1:.1f}" rx="{pill_size * 1.05:.1f}"'
                f' fill="none" stroke="{T["ember-400"]}" stroke-width="1.5"/>'
            )
            out.append(
                f'<path transform="translate({x + pad:.1f} {y + pill_size * 0.42:.1f})"'
                f' fill="{T["ember-400"]}" d="{p}"/>'
            )
            x += pw + pad * 2 + pill_size
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------- the placement ladder

# (label, note, page width mm, page height mm). The first three are the real
# default pages from platefold/config.py; the custom page is one example shape,
# since its size is cut to each figure and only its 900 mm width cap is fixed.
LADDER_RUNGS = [
    ("text column", "inline at 80% or more", 215.9, 279.4),
    ("Letter landscape", "plate at 85% or more", 279.4, 215.9),
    ("A3 landscape", "plate at 85% or more", 420.0, 297.0),
    ("custom page", "cut to the figure, max 900 mm", 640.0, 230.0),
]
LADDER_MM = 0.36  # drawing scale, px per mm, shared by every rung


def ladder_svg(dark: bool) -> str:
    """The placement ladder, with every page drawn at the same scale.

    The rectangles are in true proportion to one another, so the drawing shows
    how much larger each rung is rather than only that it is larger.
    """
    ground = T["navy-900"] if dark else T["paper-50"]
    ink = T["paper-50"] if dark else T["navy-900"]
    soft = T["amber-400"] if dark else T["amber-800"]
    edge = T["ember-400"] if dark else T["ember-700"]
    arrow_w, pad, col_min = 44, 36, 150
    cols = [max(pw * LADDER_MM, col_min) for _n, _t, pw, _ph in LADDER_RUNGS]
    w = int(pad * 2 + sum(cols) + arrow_w * (len(cols) - 1))
    tallest = max(ph for *_x, ph in LADDER_RUNGS) * LADDER_MM
    top, h = 70, int(70 + tallest + 80)
    base = top + tallest

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}">',
        f'<rect width="{w}" height="{h}" rx="10" fill="{ground}"/>',
    ]
    title, _ = text_path("each figure stops at the first page that holds it", 15, 500, 0.0)
    out.append(f'<path transform="translate({pad} 40)" fill="{ink}" d="{title}"/>')

    x = pad
    for i, ((name, note, pw_mm, ph_mm), col) in enumerate(zip(LADDER_RUNGS, cols, strict=True)):
        pw, ph = pw_mm * LADDER_MM, ph_mm * LADDER_MM
        px = x + (col - pw) / 2
        out.append(
            f'<rect x="{px:.1f}" y="{base - ph:.1f}" width="{pw:.1f}" height="{ph:.1f}"'
            f' rx="3" fill="none" stroke="{edge}" stroke-width="2"/>'
        )
        inset = 7
        out.append(
            f'<rect x="{px + inset:.1f}" y="{base - ph + inset:.1f}" width="{pw - 2 * inset:.1f}"'
            f' height="{ph - 2 * inset:.1f}" rx="2" fill="none" stroke="{soft}" stroke-width="1.5"/>'
        )
        label, lw = text_path(name, 15, 500, 0.0)
        out.append(
            f'<path transform="translate({x + (col - lw) / 2:.1f} {base + 28:.1f})"'
            f' fill="{ink}" d="{label}"/>'
        )
        note_path, nw = text_path(note, 12, 400, 0.0)
        out.append(
            f'<path transform="translate({x + (col - nw) / 2:.1f} {base + 48:.1f})"'
            f' fill="{soft}" d="{note_path}"/>'
        )
        x += col
        if i == len(LADDER_RUNGS) - 1:
            break
        ay = base - 40
        out.append(
            f'<path d="M{x + 10:.1f} {ay:.1f} h{arrow_w - 20} m-7 -6 l7 6 l-7 6"'
            f' stroke="{edge}" stroke-width="2" fill="none"'
            ' stroke-linecap="round" stroke-linejoin="round"/>'
        )
        x += arrow_w
    out.append("</svg>")
    return "".join(out)


# ------------------------------------------------------------------ rendering


def render_png(svg: str, path: str, width: int | None = None, height: int | None = None) -> None:
    import cairosvg

    kwargs = {}
    if width:
        kwargs["output_width"] = width
    if height:
        kwargs["output_height"] = height
    cairosvg.svg2png(bytestring=svg.encode(), write_to=path, **kwargs)


def render_ico(svg: str, path: str, sizes=(16, 32, 48)) -> None:
    import cairosvg
    from PIL import Image

    frames = []
    for size in sizes:
        buf = io.BytesIO()
        cairosvg.svg2png(
            bytestring=svg.encode(), write_to=buf, output_width=size, output_height=size
        )
        buf.seek(0)
        frames.append(Image.open(buf).convert("RGBA"))
    # Pillow only writes sizes up to the base image, so save from the largest frame.
    largest = max(frames, key=lambda frame: frame.width)
    others = [frame for frame in frames if frame is not largest]
    largest.save(path, format="ICO", sizes=[(s, s) for s in sizes], append_images=others)


# ----------------------------------------------------------------- the build


def build(target: str) -> None:
    brand = target
    source = os.path.join(brand, "source")
    os.makedirs(source, exist_ok=True)

    def write(rel: str, text: str) -> None:
        with open(os.path.join(brand, rel), "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")

    tile = tile_svg()
    hero = banner(1280, 400, with_tagline=True, with_pills=True)
    og = banner(1200, 630, with_tagline=True, with_pills=True)
    social = banner(1280, 640, with_tagline=True, with_pills=True)

    write("mark-bright.svg", mark_svg(BRIGHT))
    write("mark-deep.svg", mark_svg(DEEP))
    write("mark-mono.svg", mark_mono_svg())
    write("tile.svg", tile)
    write("favicon.svg", tile)
    write("lockup-h-bright.svg", lockup_h(BRIGHT, T["paper-50"]))
    write("lockup-h-deep.svg", lockup_h(DEEP, T["navy-900"]))
    write("lockup-stack-bright.svg", lockup_stack(BRIGHT, T["paper-50"]))
    write("lockup-stack-deep.svg", lockup_stack(DEEP, T["navy-900"]))
    write("lockup-tagline-bright.svg", lockup_tagline(BRIGHT, T["paper-50"], T["amber-400"]))
    write("lockup-tagline-deep.svg", lockup_tagline(DEEP, T["navy-900"], T["amber-800"]))
    write("ladder-light.svg", ladder_svg(dark=False))
    write("ladder-dark.svg", ladder_svg(dark=True))
    write("hero-banner.svg", hero)

    with open(os.path.join(source, "og-image.svg"), "w", encoding="utf-8", newline="\n") as h:
        h.write(og + "\n")
    with open(os.path.join(source, "social-banner.svg"), "w", encoding="utf-8", newline="\n") as h:
        h.write(social + "\n")

    render_png(hero, os.path.join(brand, "hero-banner.png"), 1280, 400)
    render_png(og, os.path.join(brand, "og-image.png"), 1200, 630)
    render_png(social, os.path.join(brand, "social-banner.png"), 1280, 640)
    render_png(tile, os.path.join(brand, "apple-touch-icon.png"), 180, 180)
    render_png(tile, os.path.join(brand, "icon-192.png"), 192, 192)
    render_png(tile, os.path.join(brand, "icon-512.png"), 512, 512)
    render_ico(tile, os.path.join(brand, "favicon.ico"))


# Rasterisers differ in anti-aliasing. Measured 2026-09-13, cairo 1.16 (the CI
# image) against 1.18 changed at most 0.0011% of banner pixels by one level out
# of 255, so rasters are compared as pixels with this tolerance while vector and
# icon files must match byte for byte.
RASTER_TOLERANCE = 2
RASTER_SUFFIXES = (".png",)


def same_asset(fresh: str, committed: str) -> bool:
    if not os.path.exists(committed):
        return False
    if filecmp.cmp(fresh, committed, shallow=False):
        return True
    if not fresh.endswith(RASTER_SUFFIXES):
        return False
    from PIL import Image, ImageChops

    with Image.open(fresh) as a, Image.open(committed) as b:
        if a.size != b.size:
            return False
        extrema = ImageChops.difference(a.convert("RGBA"), b.convert("RGBA")).getextrema()
    return max(high for _low, high in extrema) <= RASTER_TOLERANCE


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="build into a temporary directory and fail if the committed assets differ",
    )
    args = parser.parse_args()

    if not args.check:
        build(BRAND_DIR)
        print(f"wrote brand assets to {os.path.relpath(BRAND_DIR, REPO)}")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        build(tmp)
        differing = []
        for root, _dirs, files in os.walk(tmp):
            for name in files:
                fresh = os.path.join(root, name)
                rel = os.path.relpath(fresh, tmp)
                if not same_asset(fresh, os.path.join(BRAND_DIR, rel)):
                    differing.append(rel)
        if differing:
            print("brand assets are stale; run tools/build_brand_assets.py:", file=sys.stderr)
            for rel in sorted(differing):
                print(f"  {rel}", file=sys.stderr)
            return 1
    print("brand assets match the generator")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
