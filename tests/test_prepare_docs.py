"""Staging the docs for Zensical keeps what the MkDocs hooks used to guarantee.

Zensical runs no MkDocs hooks and builds a page the nav leaves out without a
warning, so tools/prepare_docs.py expands the generated reference, adds the
vendored Mermaid and checks the nav before the build.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import prepare_docs


def docs_tree(root: Path) -> Path:
    docs = root / "docs"
    (docs / "reference").mkdir(parents=True)
    (docs / "samples").mkdir()
    (docs / "__pycache__").mkdir()
    (docs / "index.md").write_text("# Home\n")
    (docs / "reference" / "cli.md").write_text("# CLI\n\n<!-- platefold:generate cli-help -->\n")
    (docs / "samples" / "snippet.md").write_text("snippet source\n")
    (docs / "__pycache__" / "x.pyc").write_bytes(b"")
    return docs


def mermaid(root: Path) -> Path:
    path = root / "mermaid.min.js"
    path.write_text("window.mermaid = {};\n")
    return path


def test_staging_expands_markers_adds_mermaid_and_leaves_out_samples(tmp_path):
    docs = docs_tree(tmp_path)
    output = tmp_path / "build" / "docs-site"
    (output / "stale").mkdir(parents=True)

    pages = prepare_docs.stage(docs, output, mermaid(tmp_path))

    assert [page.relative_to(output).as_posix() for page in pages] == [
        "index.md",
        "reference/cli.md",
    ]
    cli = (output / "reference" / "cli.md").read_text(encoding="utf-8")
    assert "platefold:generate" not in cli
    assert "usage: platefold" in cli
    assert (output / prepare_docs.MERMAID).read_text() == "window.mermaid = {};\n"
    assert not (output / "samples").exists()
    assert not (output / "__pycache__").exists()
    assert not (output / "stale").exists()
    # The source tree is read, never rewritten.
    assert "platefold:generate cli-help" in (docs / "reference" / "cli.md").read_text()


@pytest.mark.parametrize(
    ("line", "message"),
    [
        ("<!-- platefold:generate no-such-table -->", "unknown generation marker"),
        ("See <!-- platefold:generate cli-help --> here.", "shares its line"),
    ],
)
def test_a_marker_that_would_survive_stops_staging(tmp_path, line, message):
    docs = docs_tree(tmp_path)
    (docs / "index.md").write_text(f"# Home\n\n{line}\n")
    with pytest.raises(prepare_docs.PrepareError, match=message) as raised:
        prepare_docs.stage(docs, tmp_path / "out", mermaid(tmp_path))
    assert str(raised.value).startswith("index.md: ")


def test_nav_and_pages_must_agree_in_both_directions(tmp_path):
    prepare_docs.check_nav({"index.md", "guide.md"}, {"index.md", "guide.md"})
    with pytest.raises(prepare_docs.PrepareError) as raised:
        prepare_docs.check_nav({"index.md", "orphan.md"}, {"index.md", "gone.md"})
    assert str(raised.value).splitlines() == [
        "the nav lists gone.md, which doesn't exist",
        "orphan.md isn't in the nav",
    ]


def test_the_nav_reader_handles_sections_titles_links_and_python_tags(tmp_path):
    config = tmp_path / "mkdocs.yml"
    config.write_text(
        "markdown_extensions:\n"
        "  - pymdownx.superfences:\n"
        "      custom_fences:\n"
        "        - format: !!python/name:pymdownx.superfences.fence_code_format\n"
        "nav:\n"
        "  - Home: index.md\n"
        "  - Guides:\n"
        "      - guides/index.md\n"
        "      - Fonts: guides/fonts.md\n"
        "  - Elsewhere: https://example.invalid/\n"
    )
    assert prepare_docs.nav_pages(config) == {"index.md", "guides/index.md", "guides/fonts.md"}


def test_every_real_docs_page_is_in_the_real_nav():
    docs = prepare_docs.DOCS
    pages = {
        page.relative_to(docs).as_posix()
        for page in docs.rglob("*.md")
        if page.relative_to(docs).parts[0] not in prepare_docs.LEFT_OUT
    }
    prepare_docs.check_nav(pages, prepare_docs.nav_pages(prepare_docs.CONFIG))
