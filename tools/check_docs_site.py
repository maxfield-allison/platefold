#!/usr/bin/env python3
"""Check a built documentation site for broken local references and remote loads.

`zensical build --strict` validates links between Markdown pages. It doesn't see
what the theme adds, what raw HTML references, or whether a page makes the
reader's browser fetch something from another host. This checks the output:

* every local `href` and `src` in every page, and every `url()` in every
  stylesheet, resolves to a file in the site
* no page loads a script, stylesheet, image, font or frame from another host.
  Ordinary outbound links are allowed and counted, not fetched
* with `--sources`, every Markdown page in that directory was built. A
  generator that can't read its input can still report success with an empty
  site

    python tools/check_docs_site.py site --sources build/docs-site

Absolute local URLs carry the path of `site_url` in mkdocs.yml (the site is
served from a subpath), which is read by default; `--url-prefix` overrides it.

Exits 1 and lists each problem when anything fails. Standard library only.
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
SITE_URL_RE = re.compile(r"^site_url:\s*['\"]?([^'\"\s#]+)", re.MULTILINE)

# Attributes a browser fetches automatically, by tag. An <a href> is navigation,
# so it's checked for local existence but a remote one isn't a load.
LOADED = {
    ("script", "src"),
    ("img", "src"),
    ("iframe", "src"),
    ("source", "src"),
    ("video", "src"),
    ("audio", "src"),
    ("embed", "src"),
    ("object", "data"),
}
LINK_LOADS = {"stylesheet", "icon", "preload", "modulepreload", "apple-touch-icon", "manifest"}
CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)")


class References(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.refs: list[tuple[str, str, bool]] = []  # (tag, url, loaded automatically)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {name: value for name, value in attrs if value is not None}
        if tag == "link" and "href" in values:
            rels = set(values.get("rel", "").lower().split())
            self.refs.append((tag, values["href"], bool(rels & LINK_LOADS)))
            return
        for attribute in ("href", "src", "data"):
            if attribute in values:
                self.refs.append((tag, values[attribute], (tag, attribute) in LOADED))


def is_remote(url: str) -> bool:
    parts = urlsplit(url)
    return bool(parts.scheme in ("http", "https") or url.startswith("//"))


def is_ignorable(url: str) -> bool:
    scheme = urlsplit(url).scheme
    return url.startswith("#") or scheme in ("mailto", "data", "javascript", "tel")


def resolve(site: Path, page: Path, url: str, url_prefix: str = "") -> Path:
    path = unquote(urlsplit(url).path)
    if path.startswith("/") and url_prefix:
        prefix = "/" + url_prefix.strip("/")
        if path == prefix:
            path = "/"
        elif path.startswith(prefix + "/"):
            path = path[len(prefix) :]
    base = site if path.startswith("/") else page.parent
    target = (base / path.lstrip("/")).resolve()
    if target.is_dir():
        target = target / "index.html"
    return target


def check_page(
    site: Path, page: Path, problems: list[str], outbound: set[str], url_prefix: str = ""
) -> None:
    parser = References()
    parser.feed(page.read_text(encoding="utf-8"))
    rel = page.relative_to(site).as_posix()
    for tag, url, loaded in parser.refs:
        if is_ignorable(url):
            continue
        if is_remote(url):
            if loaded:
                problems.append(f"{rel}: <{tag}> loads a remote resource: {url}")
            else:
                outbound.add(url)
            continue
        target = resolve(site, page, url, url_prefix)
        if not target.is_relative_to(site.resolve()):
            problems.append(f"{rel}: <{tag}> points outside the site: {url}")
        elif not target.exists():
            problems.append(f"{rel}: <{tag}> missing: {url}")


def check_stylesheet(site: Path, sheet: Path, problems: list[str], url_prefix: str = "") -> None:
    rel = sheet.relative_to(site).as_posix()
    for url in CSS_URL_RE.findall(sheet.read_text(encoding="utf-8", errors="replace")):
        if is_ignorable(url):
            continue
        if is_remote(url):
            problems.append(f"{rel}: stylesheet loads a remote resource: {url}")
            continue
        if not resolve(site, sheet, url, url_prefix).exists():
            problems.append(f"{rel}: stylesheet reference missing: {url}")


def built_page(source: Path) -> Path:
    """Where a Markdown page lands in the site, with directory URLs."""
    if source.name == "index.md":
        return source.with_suffix(".html")
    return source.with_suffix("") / "index.html"


def check_sources(site: Path, sources: Path, problems: list[str]) -> None:
    markdown = sorted(sources.rglob("*.md"))
    if not markdown:
        problems.append(f"{sources}: no Markdown pages to compare with the site")
    for source in markdown:
        name = source.relative_to(sources)
        page = built_page(name)
        if not (site / page).is_file():
            problems.append(f"{page.as_posix()}: not built from {name.as_posix()}")


def configured_prefix(config: Path = ROOT / "mkdocs.yml") -> str:
    """The path part of mkdocs.yml's site_url, which prefixes absolute local URLs."""
    match = SITE_URL_RE.search(config.read_text(encoding="utf-8")) if config.is_file() else None
    return urlsplit(match.group(1)).path if match else ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("site", type=Path, help="the built site directory")
    parser.add_argument(
        "--url-prefix",
        default=None,
        help="public path prefix of absolute local URLs (default: the site_url path)",
    )
    parser.add_argument(
        "--sources",
        type=Path,
        help="the Markdown directory the site was built from; each page must be in the site",
    )
    args = parser.parse_args()
    site = args.site
    url_prefix = configured_prefix() if args.url_prefix is None else args.url_prefix
    if not (site / "index.html").is_file():
        print(f"{site} doesn't look like a built site (no index.html)", file=sys.stderr)
        return 1

    problems: list[str] = []
    outbound: set[str] = set()
    pages = sorted(site.rglob("*.html"))
    sheets = sorted(site.rglob("*.css"))
    for page in pages:
        check_page(site, page, problems, outbound, url_prefix)
    for sheet in sheets:
        check_stylesheet(site, sheet, problems, url_prefix)
    if args.sources is not None:
        check_sources(site, args.sources, problems)

    if problems:
        print(f"{len(problems)} problem(s) in the built site:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    print(
        f"checked {len(pages)} pages and {len(sheets)} stylesheets: every local reference "
        f"resolves and nothing loads from another host ({len(outbound)} outbound links)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
