"""Render mermaid fences to SVG using the bundled mermaid.js in a real browser.

Mermaid needs DOM text measurement to lay out nodes, so this drives a headless
Chromium page rather than trying to fake it with jsdom. Results are cached on
disk by source hash, so re-exporting an unchanged document is instant.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .fonts import font_css, font_signature
from .limits import MAX_CONFIG_BYTES, ResourceLimitError, read_limited_text
from .timing import Timings

FENCE = re.compile(r"^[ \t]*```mermaid[ \t]*\n(.*?)^[ \t]*```[ \t]*$", re.S | re.M)

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval'; style-src 'unsafe-inline'; img-src data:; font-src data:; base-uri 'none'; form-action 'none'">
<style>
%(font_css)s
body{margin:0;font-family:%(font)s;}
#probe{position:absolute;visibility:hidden;left:-9999px;}
</style></head><body><div id="probe"></div>
<script>%(lib)s</script>
<script>
mermaid.initialize({
  startOnLoad:false, theme:'base', securityLevel:'strict',
  fontFamily:"%(font)s",
  themeVariables:{
    fontSize:'%(size)dpx', fontFamily:"%(font)s",
    primaryColor:'%(fill)s', primaryBorderColor:'%(stroke)s', primaryTextColor:'%(text)s',
    lineColor:'%(stroke)s', secondaryColor:'%(alt)s', tertiaryColor:'%(alt)s',
    clusterBkg:'%(alt)s', clusterBorder:'%(stroke)s',
    edgeLabelBackground:'%(page)s', textColor:'%(text)s', titleColor:'%(text)s',
    actorBkg:'%(fill)s', actorBorder:'%(stroke)s', actorTextColor:'%(text)s',
    signalColor:'%(text)s', signalTextColor:'%(text)s',
    labelBoxBkgColor:'%(fill)s', labelBoxBorderColor:'%(stroke)s',
    noteBkgColor:'%(note_fill)s', noteBorderColor:'%(note_stroke)s', noteTextColor:'%(text)s'
  },
  flowchart:{ htmlLabels:true, curve:'basis', nodeSpacing:45, rankSpacing:55,
              padding:12, useMaxWidth:false, wrappingWidth:%(wrap)d },
  sequence:{ useMaxWidth:false, wrap:true, width:170, boxMargin:10,
             actorMargin:55, messageFontSize:14, noteFontSize:13, actorFontSize:14 }
});
window.__mermaidReady = true;
async function renderOne(id, code){ const { svg } = await mermaid.render(id, code); return svg; }
function measure(svg){
  const d = document.getElementById('probe');
  d.innerHTML = svg;
  const s = d.querySelector('svg');
  const bb = s.getBBox();
  d.innerHTML = '';
  return {x:bb.x, y:bb.y, w:bb.width, h:bb.height};
}
</script></body></html>"""


def find_lib(project_root: str) -> str:
    """Locate the licensed renderer shipped inside the package."""
    path = os.path.join(project_root, "vendor", "mermaid.min.js")
    if not os.path.isfile(path):
        raise RuntimeError(
            "mermaid.js not found at %s\nReinstall Plate|Fold; the package is incomplete." % path
        )
    return path


def digest(*parts: str) -> str:
    """Short content hash.

    sha256 rather than sha1: a FIPS-enforcing Windows build refuses sha1
    outright, and this is a corporate-desktop tool. The value is a cache key,
    never a security boundary.
    """
    h = hashlib.sha256()
    for part in parts:
        h.update(part.encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:16]


class MermaidRenderer:
    """Renders mermaid source to SVG, caching by content hash."""

    def __init__(
        self,
        browser,
        project_root: str,
        cache_dir: str,
        settings: Settings,
        timings: Timings | None = None,
        cancel_check: Callable[[], None] | None = None,
    ):
        self.settings = settings
        self.timings = timings or Timings()
        self.cache_dir = cache_dir
        self.cancel_check = cancel_check or (lambda: None)
        os.makedirs(cache_dir, exist_ok=True)
        self.index_path = os.path.join(cache_dir, "index.json")
        self.index: dict = {}
        if os.path.isfile(self.index_path):
            try:
                self.index = json.loads(
                    read_limited_text(
                        self.index_path, MAX_CONFIG_BYTES, label="Diagram cache index"
                    )
                )
                if not isinstance(self.index, dict):
                    self.index = {}
            except Exception:
                self.index = {}
        with open(find_lib(project_root), encoding="utf-8") as fh:
            lib = fh.read()
        self.lib_signature = hashlib.sha256(lib.encode("utf-8")).hexdigest()
        t = settings.theme
        self.page = browser.new_page(viewport={"width": 2400, "height": 1600})
        self.page.set_content(
            _PAGE
            % {
                "lib": lib,
                "font_css": font_css(settings.head_font),
                "font": settings.head_font.replace('"', "'"),
                "size": settings.mermaid_font_px,
                "wrap": settings.mermaid_wrap_px,
                # Edge labels sit on the page, not on a node, and Mermaid's own
                # default for their backing box is white. Note text and cluster
                # titles likewise default to fixed greys. Each must follow the
                # theme or a dark palette prints light text on white.
                "page": t.page,
                "fill": t.diagram_fill,
                "stroke": t.diagram_stroke,
                "text": t.diagram_text,
                "alt": t.diagram_alt_fill,
                "note_fill": t.diagram_note_fill,
                "note_stroke": t.diagram_note_stroke,
            },
            wait_until="load",
        )
        self.page.wait_for_function("window.__mermaidReady === true")
        self.page.evaluate(
            "async (font) => { await document.fonts.load('16px ' + font); await document.fonts.ready; }",
            settings.head_font,
        )
        self._n = 0

    def key(self, code: str) -> str:
        """Cache key for one diagram under the current settings.

        The signature matters as much as the source: font family and size
        change a diagram's measured geometry, and that geometry decides which
        page size the figure lands on. Keying on source alone would silently
        reuse SVGs measured under a different theme.
        """
        return digest(
            code,
            self.settings.diagram_signature(),
            self.lib_signature,
            font_signature(self.settings.head_font),
        )

    def _svg_path(self, relative: object) -> Path:
        """Resolve a cache record without allowing it to escape the cache."""
        if not isinstance(relative, str):
            raise ResourceLimitError("Diagram cache contains an invalid SVG path")
        root = Path(self.cache_dir).resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise ResourceLimitError("Diagram cache SVG resolves outside the cache directory")
        return path

    def render(self, code: str) -> dict:
        """Return {'svg_path', 'viewBox', 'w', 'h'} or {'error': msg}."""
        self.cancel_check()
        source_bytes = len(code.encode("utf-8"))
        if source_bytes > self.settings.limits.max_mermaid_source_bytes:
            raise ResourceLimitError(
                "Mermaid source exceeds the "
                f"{self.settings.limits.max_mermaid_source_bytes}-byte limit"
            )
        key = self.key(code)
        cached = self.index.get(key)
        if isinstance(cached, dict) and "error" not in cached:
            try:
                cached_path = self._svg_path(cached.get("svg_path"))
                if (
                    cached_path.is_file()
                    and cached_path.stat().st_size <= self.settings.limits.max_mermaid_svg_bytes
                ):
                    return cached
            except (OSError, ResourceLimitError):
                pass
        self._n += 1
        elid = "mmd%d" % self._n
        with self.timings.phase("render diagram"):
            try:
                svg = self.page.evaluate("([id, code]) => renderOne(id, code)", [elid, code])
            except Exception as exc:
                self.cancel_check()
                # Deliberately not cached: a diagram that failed once should be
                # retried on the next run rather than needing --clear-cache.
                return {"error": str(exc).split("\n")[0][:300]}
            self.cancel_check()
            if not isinstance(svg, str):
                raise RuntimeError("Mermaid returned no SVG document")
            svg_bytes = len(svg.encode("utf-8"))
            if svg_bytes > self.settings.limits.max_mermaid_svg_bytes:
                raise ResourceLimitError(
                    "Rendered Mermaid SVG exceeds the "
                    f"{self.settings.limits.max_mermaid_svg_bytes}-byte limit"
                )
            box = self.page.evaluate("(svg) => measure(svg)", svg)
            self.cancel_check()
        pad = 8
        rec = {
            "svg_path": key + ".svg",
            "viewBox": "%.2f %.2f %.2f %.2f"
            % (box["x"] - pad, box["y"] - pad, box["w"] + 2 * pad, box["h"] + 2 * pad),
            "w": box["w"] + 2 * pad,
            "h": box["h"] + 2 * pad,
            "root_id": elid,
        }
        with self._svg_path(key + ".svg").open("w", encoding="utf-8") as fh:
            fh.write(svg)
        self.index[key] = rec
        return rec

    def svg_text(self, rec: dict) -> str:
        return read_limited_text(
            self._svg_path(rec.get("svg_path")),
            self.settings.limits.max_mermaid_svg_bytes,
            label="Rendered Mermaid SVG",
        )

    def save(self) -> None:
        with open(self.index_path, "w", encoding="utf-8") as fh:
            json.dump(self.index, fh, indent=1)

    def close(self) -> None:
        """Persist the cache and release the theme-specific browser page."""
        self.save()
        self.page.close()
