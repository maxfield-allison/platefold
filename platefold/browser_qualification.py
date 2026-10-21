"""Small, local capability check before reusing an independently updated browser.

This is a rendering smoke test, not a security audit or full release qualification.
It runs against a fresh private profile and never reads a user's browsing profile.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from pypdf import PdfReader

from .cdp import Browser, CancelledError

SMOKE_HTML = """<!doctype html><html lang="en"><head><style>
@page { size: 100mm 140mm; margin: 8mm; }
@page wide { size: 180mm 100mm; margin: 8mm; }
body { font-family: sans-serif; }
.wide { page: wide; break-before: page; }
</style></head><body><h1 id="font-probe">PlateFold browser check</h1>
<p>Readable text and semantic structure.</p>
<section class="wide"><h2>Wide page</h2><p>Second page.</p></section>
</body></html>"""


@dataclass(frozen=True)
class QualificationReceipt:
    browser_version: str
    elapsed_seconds: float
    page_sizes_points: list[list[float]]
    platform_fonts: list[str]
    tagged_pdf: bool
    cancellation_seconds: float
    scope: str = "Local rendering capability smoke; not full release qualification"

    def as_dict(self) -> dict:
        return asdict(self)


class ProbeCancelled(CancelledError):
    """Synthetic interruption, distinct from a real user cancellation."""


def _render_proof(page) -> tuple[list[list[float]], list[str]]:
    page.emulate_media("print")
    page.set_content(SMOKE_HTML)
    page._send("DOM.enable")
    page._send("CSS.enable")
    document = page._send("DOM.getDocument")
    node = page._send(
        "DOM.querySelector",
        {"nodeId": document["root"]["nodeId"], "selector": "#font-probe"},
    )
    fonts = page._send("CSS.getPlatformFontsForNode", {"nodeId": node["nodeId"]})
    platform_fonts = sorted(
        {
            font["familyName"]
            for font in fonts.get("fonts", [])
            if font.get("glyphCount", 0) > 0 and not font.get("isCustomFont", False)
        }
    )
    if not platform_fonts:
        raise RuntimeError("The browser could not resolve a local platform font")
    data = page.pdf(prefer_css_page_size=True, print_background=True, max_bytes=2_000_000)
    reader = PdfReader(io.BytesIO(data))
    sizes = [[float(p.mediabox.width), float(p.mediabox.height)] for p in reader.pages]
    expected = [[100 * 72 / 25.4, 140 * 72 / 25.4], [180 * 72 / 25.4, 100 * 72 / 25.4]]
    if len(sizes) != 2 or any(
        abs(actual - wanted) > 1.5
        for size, target in zip(sizes, expected, strict=False)
        for actual, wanted in zip(size, target, strict=True)
    ):
        raise RuntimeError("The browser did not preserve mixed CSS page sizes")
    root = reader.trailer["/Root"]
    marked = root.get("/MarkInfo") if isinstance(root, dict) else None
    if (
        not isinstance(root, dict)
        or not root.get("/StructTreeRoot")
        or not isinstance(marked, dict)
        or not marked.get("/Marked")
    ):
        raise RuntimeError("The browser did not produce a tagged PDF")
    text = " ".join((reader.pages[0].extract_text() or "").split())
    if "PlateFold browser check" not in text:
        raise RuntimeError("The browser PDF did not preserve readable text")
    return sizes, platform_fonts


def _check_cancellation(browser: Browser, page, check: Callable[[], bool]) -> float:
    # Exercise cancellation during an outstanding response, not just before
    # sending a command. The fresh qualification page is discarded afterward.
    cancellation_started = time.monotonic()

    def probe_cancel() -> bool:
        check()
        if time.monotonic() - cancellation_started >= 0.15:
            raise ProbeCancelled("Synthetic browser qualification cancellation")
        return False

    browser.set_cancel_callback(probe_cancel)
    try:
        page.evaluate("new Promise(resolve => setTimeout(() => resolve(true), 2000))")
    except ProbeCancelled:
        cancellation_seconds = time.monotonic() - cancellation_started
    else:
        raise RuntimeError("The browser connection did not cancel an active wait")
    finally:
        browser.set_cancel_callback(check)
    if cancellation_seconds > 1.0:
        raise RuntimeError("The browser connection did not cancel within one second")
    return cancellation_seconds


def qualify(
    browser: Browser, *, cancel_check: Callable[[], None] | None = None
) -> QualificationReceipt:
    """Check the actual engine each launch, so an update cannot reuse stale proof."""
    started = time.monotonic()

    def check() -> bool:
        if cancel_check:
            cancel_check()
        if time.monotonic() - started >= 10.0:
            raise RuntimeError("Browser rendering check exceeded ten seconds")
        return False

    browser.set_cancel_callback(check)
    page = None
    completed = False
    try:
        check()
        version = browser.version
        check()  # Browser.version deliberately suppresses transport exceptions.
        if version == "unknown":
            raise RuntimeError("The browser did not report its version")
        page = browser.new_page()
        sizes, platform_fonts = _render_proof(page)

        cancellation_seconds = _check_cancellation(browser, page, check)
        check()
        completed = True
        return QualificationReceipt(
            version,
            round(time.monotonic() - started, 3),
            sizes,
            platform_fonts,
            True,
            round(cancellation_seconds, 3),
        )
    finally:
        browser.set_cancel_callback(check if completed else lambda: True)
        if page is not None:
            with contextlib.suppress(Exception):
                page.close()
        browser.set_cancel_callback(None)


def _diagnose(choice: str) -> dict:
    from .browser import find_bundled, find_system, launch

    browser = None
    try:
        if choice == "system":
            browser, selected_engine = launch("system")
            evidence = browser.qualification_receipt
        else:
            path = find_bundled() if choice == "bundled" else find_system(choice)
            if path is None and Path(choice).is_file():
                path = str(Path(choice).resolve())
            if path is None:
                raise RuntimeError(f"Browser was not found: {choice}")
            browser = Browser(path)
            evidence = qualify(browser).as_dict()
            selected_engine = f"{choice} ({evidence['browser_version']})"
        if evidence is None:
            raise RuntimeError("The selected browser has no qualification evidence")
        return {
            **evidence,
            "status": "passed",
            "requested_browser": choice,
            "selected_engine": selected_engine,
            "system_browser_failures": getattr(browser, "system_browser_failures", None) or [],
        }
    finally:
        if browser is not None:
            browser.set_cancel_callback(lambda: True)
            with contextlib.suppress(Exception):
                browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", required=True, help="Browser name, system, or executable path")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = _diagnose(args.browser)
        result = 0
    except Exception as exc:
        receipt = {
            "status": "failed",
            "requested_browser": args.browser,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }
        result = 1
    args.output.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
