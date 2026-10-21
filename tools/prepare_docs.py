#!/usr/bin/env python3
"""Stage the documentation site's Markdown for Zensical.

Zensical reads mkdocs.yml but runs no MkDocs hooks, so the site's two
build-time jobs happen here first, into build/docs-site, which mkdocs.yml names
as docs_dir:

* Expand `<!-- platefold:generate NAME -->` markers with reference tables read
  from the installed package (see docs_reference.py), so configuration, theme
  and CLI reference cannot drift from the code.
* Serve the Mermaid build the package already vendors. The theme fetches
  Mermaid from a CDN unless a `mermaid` global exists, and a docs site for a
  tool whose exports never touch the network should not make its readers'
  browsers do so either.

docs/samples holds snippet sources rather than pages, so it is left out; the
snippet base path in mkdocs.yml still reads it in place. `zensical build
--strict` fails on a broken link or anchor but builds a page the nav leaves out
without a word, so staging also checks that the nav and the pages agree.

    python tools/prepare_docs.py
    zensical build --clean --strict
    python tools/check_docs_site.py site --sources build/docs-site

Exits 1 with the reason when a marker is unknown or survives expansion, the nav
and the pages disagree, or the vendored Mermaid is missing.
"""

from __future__ import annotations

import shutil
import sys
from collections.abc import Iterator
from pathlib import Path

import docs_reference
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "mkdocs.yml"
DOCS = ROOT / "docs"
STAGED = ROOT / "build" / "docs-site"
LEFT_OUT = frozenset({"samples"})  # top-level docs/ directories that aren't site input
MERMAID = Path("assets/javascripts/mermaid.min.js")
MARKER_START = "<!-- platefold:generate"


class PrepareError(RuntimeError):
    """The docs can't be staged as they are."""


class _Config(yaml.SafeLoader):
    """Reads mkdocs.yml without constructing its `!!python/name:` tags."""


_Config.add_multi_constructor("tag:yaml.org,2002:python/", lambda _loader, _suffix, _node: None)


def nav_pages(config: Path) -> set[str]:
    """The Markdown pages the nav in a config file lists."""

    def walk(items: list) -> Iterator[str]:
        for item in items:
            if isinstance(item, dict):
                for value in item.values():
                    yield from walk(value if isinstance(value, list) else [value])
            elif "://" not in item:
                yield item

    return set(walk(yaml.load(config.read_text(encoding="utf-8"), Loader=_Config)["nav"]))


def check_nav(pages: set[str], nav: set[str]) -> None:
    problems = [f"the nav lists {page}, which doesn't exist" for page in sorted(nav - pages)]
    problems += [f"{page} isn't in the nav" for page in sorted(pages - nav)]
    if problems:
        raise PrepareError("\n".join(problems))


def vendored_mermaid() -> Path:
    import platefold

    path = Path(platefold.__file__).parent / "vendor" / "mermaid.min.js"
    if not path.is_file():
        raise PrepareError(f"vendored Mermaid not found at {path}")
    return path


def _site_input(docs: Path):
    def ignore(directory: str, names: list[str]) -> set[str]:
        skipped = {name for name in names if name == "__pycache__"}
        if Path(directory) == docs:
            skipped |= LEFT_OUT.intersection(names)
        return skipped

    return ignore


def stage(docs: Path, output: Path, mermaid: Path) -> list[Path]:
    """Replace output with the site input from docs; return the staged pages."""
    if output.exists():
        shutil.rmtree(output)
    shutil.copytree(docs, output, ignore=_site_input(docs))
    pages = sorted(output.rglob("*.md"))
    for page in pages:
        name = page.relative_to(output).as_posix()
        text = page.read_text(encoding="utf-8")
        try:
            expanded = docs_reference.expand(text)
        except docs_reference.DocsReferenceError as exc:
            raise PrepareError(f"{name}: {exc}") from exc
        # expand() only replaces a marker on a line of its own.
        if MARKER_START in expanded:
            raise PrepareError(f"{name}: a generation marker shares its line with other text")
        if expanded != text:
            page.write_text(expanded, encoding="utf-8")
    target = output / MERMAID
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(mermaid, target)
    return pages


def main() -> int:
    try:
        pages = stage(DOCS, STAGED, vendored_mermaid())
        check_nav({page.relative_to(STAGED).as_posix() for page in pages}, nav_pages(CONFIG))
    except PrepareError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(
        f"staged {len(pages)} pages and vendored Mermaid in {STAGED.relative_to(ROOT).as_posix()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
