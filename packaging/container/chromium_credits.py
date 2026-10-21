"""Write the bundled Chromium's third-party credits to a compressed HTML file.

Chromium compiles its license inventory into the browser and shows it at
chrome://credits. Neither Playwright Chromium archive the image installs ships
that inventory as a file, so the image build renders the page from the
installed browser and keeps it beside the other notices. Each architecture's
file therefore describes that architecture's own browser.

Build use only: the build runs as root, which Chromium's sandbox refuses, and
the page is a built-in one that needs no network. Every host lookup is refused.
It drives the browser through PlateFold's own DevTools client, because the
runtime image has no Playwright package.
"""

from __future__ import annotations

import gzip
import sys
from pathlib import Path

from platefold.browser import find_bundled
from platefold.cdp import Browser

# Components the image's PDF path depends on; a page without them is not the
# inventory, whatever its length.
REQUIRED = ("PDFium", "FreeType", "harfbuzz", "Skia")
MINIMUM_ENTRIES = 300


def render() -> tuple[str, str]:
    executable = find_bundled()
    if not executable:
        raise SystemExit("no bundled Chromium found under PLAYWRIGHT_BROWSERS_PATH")
    # A build container's /dev/shm is 64 MB; the page's DOM is several times that.
    flags = ["--no-sandbox", "--disable-dev-shm-usage", "--host-resolver-rules=MAP * ~NOTFOUND"]
    browser = Browser(executable, flags)
    try:
        page = browser.new_page()
        page._send("Page.navigate", {"url": "chrome://credits/"})
        page.wait_for_function(
            "location.href === 'chrome://credits/' && document.readyState === 'complete'"
        )
        return browser.version, page.evaluate("document.documentElement.outerHTML")
    finally:
        browser.close()


def check(html: str) -> int:
    entries = html.count('<span class="title">')
    missing = [name for name in REQUIRED if f'<span class="title">{name}<' not in html]
    if entries < MINIMUM_ENTRIES or missing:
        raise SystemExit(
            f"chrome://credits is incomplete: {entries} entries, missing {missing or 'none'}"
        )
    return entries


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: chromium_credits.py OUTPUT.html.gz")
    target = Path(argv[1])
    version, html = render()
    entries = check(html)
    target.write_bytes(gzip.compress(html.encode("utf-8"), compresslevel=9, mtime=0))
    print(f"{version}: {entries} credited components written to {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
