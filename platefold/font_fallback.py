"""Find the characters a document's fonts lack, and what the renderer drew instead.

A font stack names the faces a document asks for. When the face setting the
text has no glyph for a character, Chromium draws that character with another
face: a later one in the stack or one the operating system offers. When no
installed face has it, Chromium draws the primary face's missing-glyph box.

This module asks the renderer rather than reading font files. Chromium's
DevTools ``CSS.getPlatformFontsForNode`` reports the faces that shaped an
element's text on the machine doing the export, so the answer follows the
Windows, macOS and Linux fallback rules as they are, including composed
combining marks, emoji sequences and faces that are not files PlateFold can
open. It cannot say which characters in an element used which face, so the
distinct characters of each styled run are probed in ranges, and a range drawn
by more than one face is split, until every character is attributed to one.

A missing-glyph box is reported as the primary face, because it is that face's
glyph. Candidates whose width matches the box are therefore drawn next to a
code point no font maps, and the pixels compared.
"""

from __future__ import annotations

import base64
import contextlib
import io
import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from .cdp import CDPError

# Distinct characters (grapheme clusters) checked per document. Documents in
# CJK scripts use a few thousand; the limit bounds the probe on hostile input.
MAX_CHARACTERS = 20_000
# DevTools font queries per document. Attribution needs about SPLIT queries per
# face boundary per level, so this is reached only by adversarial text.
MAX_QUERIES = 4_000
SAMPLE_SIZE = 12
WARNING_SAMPLE = 8  # of the sample, shown in warning text
# Each probed range that more than one face drew is split this many ways. Wide
# splits mean fewer rounds of layout, which cost more than the extra queries.
SPLIT = 16
# Chromium on macOS can draw unmapped code points with Apple's placeholder face.
PLACEHOLDER_FACES = frozenset({"lastresort", ".lastresort"})
_BASELINE = "Hamburgefonstiv 0123456789"
_UNMAPPED = "\U00050000"  # plane 5 is unassigned; no font maps it

_SCRIPTS = [
    "Latin",
    "Greek",
    "Cyrillic",
    "Armenian",
    "Hebrew",
    "Arabic",
    "Syriac",
    "Thaana",
    "Nko",
    "Devanagari",
    "Bengali",
    "Gurmukhi",
    "Gujarati",
    "Oriya",
    "Tamil",
    "Telugu",
    "Kannada",
    "Malayalam",
    "Sinhala",
    "Thai",
    "Lao",
    "Tibetan",
    "Myanmar",
    "Georgian",
    "Hangul",
    "Ethiopic",
    "Cherokee",
    "Khmer",
    "Mongolian",
    "Hiragana",
    "Katakana",
    "Bopomofo",
    "Han",
    "Yi",
    "Javanese",
    "Balinese",
    "Tifinagh",
    "Vai",
    "Canadian_Aboriginal",
    "Ogham",
    "Runic",
    "Braille",
]

_SCAN = r"""({id, limit, running, scripts, exclude}) => {
    const segmenter = new Intl.Segmenter(undefined, {granularity: 'grapheme'});
    const invisible = /^[\p{White_Space}\p{Cc}\p{Cf}\p{Zl}\p{Zp}]*$/u;
    const scriptTests = scripts.map(name => [name, new RegExp(`\\p{Script=${name}}`, 'u')]);
    const scriptOf = text => {
        for (const [name, test] of scriptTests) if (test.test(text)) return name;
        if (/\p{Extended_Pictographic}|\p{Regional_Indicator}/u.test(text)) return 'Emoji';
        if (/^\p{Mn}|\p{Script=Inherited}/u.test(text)) return 'Combining marks';
        if (/\p{Script=Common}/u.test(text)) return 'Symbols and punctuation';
        return 'Other scripts';
    };
    const groups = new Map();
    let distinct = 0, truncated = false;
    const add = (style, text) => {
        const key = JSON.stringify(style);
        let group = groups.get(key);
        if (!group) groups.set(key, group = {style, graphemes: new Map()});
        for (const {segment} of segmenter.segment(text)) {
            if (invisible.test(segment)) continue;
            const seen = group.graphemes.get(segment);
            if (seen) { seen[1]++; continue; }
            if (distinct >= limit) { truncated = true; continue; }
            group.graphemes.set(segment, [segment, 1, distinct++, scriptOf(segment)]);
        }
    };
    const hidden = 'script,style,noscript,template,title,desc,metadata';
    const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
    const styles = new Map();
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const element = node.parentElement;
        if (!element || invisible.test(node.data) || element.closest(hidden)
            || element.closest('#' + id) || (exclude && element.closest(exclude))) continue;
        let style = styles.get(element);
        if (style === undefined) {
            style = null;
            if (element.checkVisibility({visibilityProperty: true})) {
                const computed = getComputedStyle(element);
                style = {
                    family: computed.fontFamily,
                    weight: computed.fontWeight,
                    italic: computed.fontStyle !== 'normal',
                    transform: computed.textTransform,
                    lang: element.closest('[lang]')?.getAttribute('lang') || '',
                };
            }
            styles.set(element, style);
        }
        if (style) add(style, node.data);
    }
    for (const {family, text} of running) {
        add({family, weight: '400', italic: false, transform: 'none',
             lang: document.documentElement.lang || ''}, text);
    }
    return {
        truncated,
        groups: Array.from(groups.values(), group => ({
            style: group.style, graphemes: Array.from(group.graphemes.values()),
        })),
    };
}"""

_PLACE = r"""({id, spans, grid}) => {
    let container = document.getElementById(id);
    if (!container) {
        container = document.createElement('div');
        container.id = id;
        container.setAttribute('aria-hidden', 'true');
        document.body.appendChild(container);
    }
    container.style.cssText = grid
        ? 'position:absolute;left:0;top:0;z-index:2147483647;background:#fff;color:#000;'
          + 'margin:0;padding:0;border:0;pointer-events:none;'
          + `width:${grid.columns * grid.width}px;height:${grid.rows * grid.height}px`
        : 'position:fixed;left:-20000px;top:0;pointer-events:none;contain:layout style';
    container.replaceChildren();
    const widths = [];
    spans.forEach(({style, text}, index) => {
        const span = document.createElement('span');
        span.style.cssText = 'white-space:pre;font-size:32px;line-height:48px;letter-spacing:0;'
            + 'word-spacing:0;font-kerning:none;font-variant:normal;font-feature-settings:normal';
        span.style.fontFamily = style.family;
        span.style.fontWeight = style.weight;
        span.style.fontStyle = style.italic ? 'italic' : 'normal';
        span.style.textTransform = style.transform;
        if (style.lang) span.lang = style.lang;
        span.textContent = text;
        if (grid) {
            const cell = document.createElement('div');
            cell.style.cssText = 'position:absolute;overflow:hidden;padding:0 0 0 8px;'
                + `left:${(index % grid.columns) * grid.width}px;`
                + `top:${Math.floor(index / grid.columns) * grid.height}px;`
                + `width:${grid.width - 8}px;height:${grid.height}px`;
            cell.appendChild(span);
            container.appendChild(cell);
        } else {
            container.appendChild(span);
        }
    });
    if (grid) {
        // Where the grid landed, in document coordinates, for the screenshot clip.
        const rect = container.getBoundingClientRect();
        return [rect.left + window.scrollX, rect.top + window.scrollY];
    }
    for (const span of container.children) widths.push(span.getBoundingClientRect().width);
    return widths;
}"""


# Failures that leave the check unverified rather than failing the export.
FALLBACK_ERRORS = (CDPError, KeyError, AttributeError, TypeError, ValueError, OSError)

_PRIMARY = "\0primary"  # attribution: the face that sets the group's Latin text


@dataclass
class _Group:
    style: dict
    graphemes: list[list]  # [text, occurrences, first-seen order, script]
    primary: dict[str, str] = field(default_factory=dict)  # casefolded -> reported name
    notdef: str = ""  # casefolded face that draws a missing-glyph box
    notdef_width: float = 0.0
    faces: dict[int, str] = field(default_factory=dict)  # grapheme index -> face

    @property
    def font(self) -> str:
        return ", ".join(sorted(self.primary.values())) or _first_family(self.style["family"])

    def draws_notdef(self, face: str) -> bool:
        return (face == _PRIMARY and self.notdef in self.primary) or face.casefold() == self.notdef


@dataclass
class _Probe:
    page: Any
    probe_id: str
    queries: int = 0
    complete: bool = True
    container: int | None = None

    def place(self, specs: list[tuple[dict, str]], grid: dict | None = None) -> list:
        arg: dict[str, Any] = {
            "id": self.probe_id,
            "spans": [{"style": style, "text": text} for style, text in specs],
        }
        if grid:
            arg["grid"] = grid
        widths = self.page.evaluate(_PLACE, arg)
        return widths if isinstance(widths, list) else []

    def fonts(self, specs: list[tuple[dict, str]]) -> list[dict[str, int]]:
        """The faces, with glyph counts, that shaped each probe text."""
        self.place(specs)
        if self.container is None:  # node ids stay valid while the DOM domain is enabled
            root = self.page._send("DOM.getDocument", {"depth": 0})["root"]["nodeId"]
            self.container = self.page._send(
                "DOM.querySelector", {"nodeId": root, "selector": f"#{self.probe_id}"}
            )["nodeId"]
        nodes = self.page._send(
            "DOM.querySelectorAll", {"nodeId": self.container, "selector": ":scope > span"}
        )["nodeIds"]
        if len(nodes) != len(specs):
            raise CDPError("Font probe elements did not render")
        result = []
        for node in nodes:
            self.queries += 1
            fonts = self.page._send("CSS.getPlatformFontsForNode", {"nodeId": node})
            result.append(
                {
                    str(font["familyName"]): int(font.get("glyphCount", 0))
                    for font in fonts.get("fonts", [])
                    if font.get("familyName") and font.get("glyphCount", 0) > 0
                }
            )
        return result


def _baseline(probe: _Probe, groups: list[_Group]) -> None:
    specs = [(group.style, text) for group in groups for text in (_BASELINE, _UNMAPPED)]
    fonts = probe.fonts(specs)
    for index, group in enumerate(groups):
        latin, notdef = fonts[2 * index], fonts[2 * index + 1]
        requested = _first_family(group.style["family"]).casefold()
        # The faces that set Latin text, plus the requested face when it has
        # no Latin letters (a symbol or single-script choice).
        group.primary = {name.casefold(): name for name in latin}
        group.primary.update(
            {name.casefold(): name for name in notdef if name.casefold() == requested}
        )
        group.notdef = next(iter(notdef), "").casefold()


def _attribute(probe: _Probe, groups: list[_Group]) -> None:
    """Split each group's characters until every range is shaped by one face."""
    pending = [(group, 0, len(group.graphemes)) for group in groups if group.graphemes]
    while pending:
        if probe.queries + len(pending) > MAX_QUERIES:
            probe.complete = False
            return
        specs = [
            (group.style, "".join(item[0] for item in group.graphemes[start:end]))
            for group, start, end in pending
        ]
        following = []
        for (group, start, end), fonts in zip(pending, probe.fonts(specs), strict=True):
            others = [name for name in fonts if name.casefold() not in group.primary]
            if not fonts:
                continue  # nothing drawn; left unattributed
            if not others:
                face = _PRIMARY
            elif len(fonts) == 1 or end - start == 1:
                # One face drew the whole range, or one character used several
                # faces and the one that drew most of it is named.
                face = max(others, key=lambda name: fonts[name])
            else:
                step = -(-(end - start) // SPLIT)
                following += [(group, low, min(low + step, end)) for low in range(start, end, step)]
                continue
            for index in range(start, end):
                group.faces[index] = face
        pending = following


def _missing_glyphs(probe: _Probe, groups: list[_Group]) -> set[tuple[int, int]]:
    """Characters drawn as a missing-glyph box: the box's width, then its pixels."""
    candidates = [
        (number, index)
        for number, group in enumerate(groups)
        if group.notdef and group.notdef not in PLACEHOLDER_FACES
        for index, face in group.faces.items()
        if group.draws_notdef(face)
    ]
    if not candidates:
        return set()
    specs = [(group.style, _UNMAPPED) for group in groups]
    specs += [(groups[n].style, groups[n].graphemes[i][0]) for n, i in candidates]
    widths = probe.place(specs)
    if len(widths) != len(specs):
        raise CDPError("Font probe elements did not measure")
    for group, width in zip(groups, widths, strict=False):
        group.notdef_width = float(width)
    suspects = []
    for (number, index), width in zip(candidates, widths[len(groups) :], strict=True):
        box = groups[number].notdef_width
        count = round(width / box) if box > 0 else 0  # a cluster can be a row of boxes
        if count >= 1 and abs(width - count * box) < 0.01:
            suspects.append((number, index, count))
    return _same_pixels(probe, groups, suspects) if suspects else set()


def _same_pixels(
    probe: _Probe, groups: list[_Group], suspects: list[tuple[int, int, int]]
) -> set[tuple[int, int]]:
    from PIL import Image

    references = sorted({(number, count) for number, _index, count in suspects})
    specs = [(groups[n].style, _UNMAPPED * count) for n, count in references]
    specs += [(groups[n].style, groups[n].graphemes[i][0]) for n, i, _count in suspects]
    width = int(max(groups[n].notdef_width * count for n, count in references)) + 24
    height, columns = 56, 32
    rows = -(-len(specs) // columns)
    origin = probe.place(
        specs, {"columns": columns, "width": width, "height": height, "rows": rows}
    )
    if len(origin) != 2:
        raise CDPError("Font probe grid did not render")
    shot = probe.page._send(
        "Page.captureScreenshot",
        {
            "format": "png",
            "clip": {
                "x": origin[0],
                "y": origin[1],
                "width": columns * width,
                "height": rows * height,
                "scale": 1,
            },
            "captureBeyondViewport": True,
        },
    )
    image = Image.open(io.BytesIO(base64.b64decode(shot["data"]))).convert("L")

    def cell(position: int) -> bytes:
        left, top = (position % columns) * width, (position // columns) * height
        return image.crop((left, top, left + width, top + height)).tobytes()

    boxes = {key: cell(position) for position, key in enumerate(references)}
    return {
        (number, index)
        for position, (number, index, count) in enumerate(suspects, start=len(references))
        if cell(position) == boxes[(number, count)]
    }


def _first_family(stack: str) -> str:
    return stack.split(",")[0].strip().strip("'\"")


@dataclass
class Attribution:
    """One print pass's characters and the faces that drew them."""

    groups: list[_Group]
    missing: set[tuple[int, int]]
    complete: bool


def check_font_fallback(page, running_text: list[tuple[str, str]] | None = None) -> dict:
    """Report, per fallback face, the characters the document's own faces lack.

    ``running_text`` lists ``(font stack, text)`` pairs printed outside the
    page body, such as running headers and footers.
    """
    # Heading markers are text only in the contents-page measuring pass.
    scan = scan_text(page, running_text, ".pgmark")
    return summarize_fallback([attribute_scan(page, scan)])


def scan_text(page, running_text: list[tuple[str, str]] | None = None, exclude: str = "") -> dict:
    """The loaded document's distinct characters, grouped by text style."""
    scan = page.evaluate(
        _SCAN,
        {
            "id": "pf-fallback-scan",
            "limit": MAX_CHARACTERS,
            "running": [{"family": stack, "text": text} for stack, text in running_text or []],
            "scripts": list(_SCRIPTS),
            "exclude": exclude,
        },
    )
    if not isinstance(scan, dict) or not isinstance(scan.get("groups"), list):
        raise CDPError("Font fallback scan returned no result")
    return scan


def merge_scans(scans: list[dict]) -> dict:
    """One scan for a document printed in several passes.

    A character counts once per style, its occurrences add up, and its order
    is that of the pass that met it first. The character limit applies to
    the whole document, as it does to one pass.
    """
    groups: dict[str, dict] = {}
    seen: dict[str, dict[str, list]] = {}
    distinct = 0
    truncated = any(scan.get("truncated") for scan in scans)
    for number, scan in enumerate(scans):
        for group in scan["groups"]:
            key = json.dumps(group["style"], sort_keys=True)
            target = groups.setdefault(key, {"style": group["style"], "graphemes": []})
            known = seen.setdefault(key, {})
            for text, occurrences, order, script in group["graphemes"]:
                if text in known:
                    known[text][1] += occurrences
                elif distinct >= MAX_CHARACTERS:
                    truncated = True
                else:
                    known[text] = [text, occurrences, number * 1_000_000_000 + order, script]
                    target["graphemes"].append(known[text])
                    distinct += 1
    return {"truncated": truncated, "groups": list(groups.values())}


_PRELOAD = """styles => Promise.all(styles.map(style => document.fonts.load(
    `${style.italic ? 'italic ' : ''}${style.weight} 16px ${style.family}`
).catch(() => null)))"""


def attribute_scan(page, scan: dict, *, preload: bool = False) -> Attribution:
    """Attribute every scanned character to the face that draws it.

    ``preload`` loads each style's fonts first, for a page other than the one
    the text was scanned on.
    """
    probe = _Probe(page, "pf-fallback-" + uuid.uuid4().hex)
    groups = [
        # Script order keeps each face's characters together, so halving
        # finds the boundaries in few queries.
        _Group(item["style"], sorted(item["graphemes"], key=lambda g: (g[3], g[0])))
        for item in scan["groups"]
    ]
    try:
        if preload and groups:
            page.evaluate(_PRELOAD, [group.style for group in groups])
        page._send("DOM.enable")
        page._send("CSS.enable")
        if groups:
            _baseline(probe, groups)
            _attribute(probe, groups)
        missing = _missing_glyphs(probe, groups) if probe.complete else set()
    finally:
        # Cancellation exceptions must propagate; cleanup cannot mask them.
        with contextlib.suppress(Exception):
            page.evaluate("id => document.getElementById(id)?.remove()", probe.probe_id)
        for domain in ("CSS", "DOM"):
            with contextlib.suppress(Exception):
                page._send(f"{domain}.disable")
    return Attribution(groups, missing, probe.complete and not scan.get("truncated"))


def summarize_fallback(passes: list[Attribution]) -> dict:
    """The report for one attribution (a list, as older callers pass it)."""
    (only,) = passes
    return _summarize(only.groups, only.missing, only.complete)


def record_font_fallback(info: dict, result: dict | None) -> None:
    """Store the fallback report and its warnings; None means the check failed."""
    if result is None:
        info["font_fallback"] = {"status": "unverified", "characters_checked": 0, "fallbacks": []}
        warnings = [
            "FONT_FALLBACK_UNVERIFIED: The renderer could not report which characters use "
            "fallback fonts. Check the PDF appearance before sharing it."
        ]
    else:
        info["font_fallback"] = result
        warnings = fallback_warnings(result)
    for warning in warnings:
        if warning not in info.setdefault("warnings", []):
            info["warnings"].append(warning)


def _summarize(groups: list[_Group], missing: set[tuple[int, int]], complete: bool) -> dict:
    merged: dict[tuple[str, str | None], dict] = {}
    checked = 0
    for number, group in enumerate(groups):
        for index, face in group.faces.items():
            checked += 1
            if (number, index) in missing or face.casefold() in PLACEHOLDER_FACES:
                fallback = None  # no installed face has it: a placeholder box prints
            elif face == _PRIMARY:
                continue
            else:
                fallback = face
            text, occurrences, order, script = group.graphemes[index]
            entry = merged.setdefault((group.font, fallback), {"characters": {}, "scripts": {}})
            first, count = entry["characters"].get(text, (order, 0))
            entry["characters"][text] = (min(first, order), count + occurrences)
            entry["scripts"][script] = entry["scripts"].get(script, 0) + 1
    fallbacks = []
    for (font, fallback), entry in merged.items():
        ordered = sorted(entry["characters"].items(), key=lambda pair: pair[1][0])
        scripts = entry["scripts"]
        fallbacks.append(
            {
                "font": font,
                "fallback": fallback,
                "characters": len(ordered),
                "occurrences": sum(count for _text, (_first, count) in ordered),
                "scripts": sorted(scripts, key=lambda name: (-scripts[name], name)),
                "sample": [text for text, _value in ordered[:SAMPLE_SIZE]],
            }
        )
    fallbacks.sort(key=lambda item: (item["fallback"] is not None, -item["characters"]))
    return {
        "status": "complete" if complete else "partial",
        "characters_checked": checked,
        "fallbacks": fallbacks,
    }


def _characters(entry: dict) -> str:
    shown = " ".join(entry["sample"][:WARNING_SAMPLE])
    more = entry["characters"] - min(entry["characters"], WARNING_SAMPLE)
    return shown + (f" and {more:,} more" if more else "")


def fallback_warnings(result: dict) -> list[str]:
    """One warning per requested face and fallback face, never one per character."""
    warnings = []
    for entry in result["fallbacks"]:
        count = entry["characters"]
        noun = "character" if count == 1 else "characters"
        scripts = ", ".join(entry["scripts"])
        if entry["fallback"] is None:
            warnings.append(
                f"FONT_GLYPHS_MISSING: {count:,} {noun} ({scripts}) "
                f"{'has' if count == 1 else 'have'} no glyph in {entry['font']} or any "
                f"installed font and {'prints as a placeholder box' if count == 1 else 'print as placeholder boxes'}: "
                f"{_characters(entry)}. Install a font that covers them."
            )
        else:
            warnings.append(
                f"FONT_FALLBACK: {entry['font']} has no glyph for {count:,} {noun} ({scripts}); "
                f"{'it prints' if count == 1 else 'they print'} in {entry['fallback']}: "
                f"{_characters(entry)}. To choose the face "
                "yourself, use a font set whose fonts cover these characters. Fallback can "
                "change page layout."
            )
    if result["status"] == "partial":
        warnings.append(
            "FONT_FALLBACK_INCOMPLETE: The fallback check stopped at its limit of "
            f"{MAX_CHARACTERS:,} distinct characters or {MAX_QUERIES:,} renderer queries. "
            "Characters it did not reach may print in fallback fonts without a warning.",
        )
    return warnings


def report_font_fallback(page, info: dict, running_text: list[tuple[str, str]]) -> None:
    """Record fallback evidence and its warnings once per document."""
    if "font_fallback" in info:
        return  # contents-page measuring and final passes print the same text
    try:
        result = check_font_fallback(page, running_text)
    except FALLBACK_ERRORS:
        result = None
    record_font_fallback(info, result)
