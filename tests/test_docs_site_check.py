"""The built-site checker resolves absolute URLs under the site_url path and
notices a page the build left out."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import check_docs_site


def site(tmp_path: Path, link: str) -> Path:
    root = tmp_path / "site"
    (root / "guide").mkdir(parents=True)
    (root / "guide/index.html").write_text("<p>guide</p>")
    (root / "index.html").write_text(f'<a href="{link}">guide</a>')
    return root


def test_absolute_links_resolve_under_the_prefix(tmp_path):
    root = site(tmp_path, "/platefold/guide/")
    problems: list[str] = []
    check_docs_site.check_page(root, root / "index.html", problems, set(), "/platefold/")
    assert problems == []
    check_docs_site.check_page(root, root / "index.html", problems, set(), "")
    assert problems and "missing" in problems[0]


def test_prefix_defaults_to_the_site_url_path(tmp_path):
    config = tmp_path / "mkdocs.yml"
    config.write_text("site_name: x\nsite_url: https://example.invalid/platefold/\n")
    assert check_docs_site.configured_prefix(config) == "/platefold/"
    config.write_text("site_name: x\n")
    assert check_docs_site.configured_prefix(config) == ""
    assert check_docs_site.configured_prefix(tmp_path / "absent.yml") == ""


def test_every_source_page_must_be_built(tmp_path):
    sources = tmp_path / "sources"
    (sources / "guide").mkdir(parents=True)
    for name in ("index.md", "guide/index.md", "guide/fonts.md"):
        (sources / name).write_text("# page\n")
    root = site(tmp_path, "guide/")
    problems: list[str] = []
    check_docs_site.check_sources(root, sources, problems)
    assert problems == ["guide/fonts/index.html: not built from guide/fonts.md"]
    (root / "guide/fonts").mkdir()
    (root / "guide/fonts/index.html").write_text("<p>fonts</p>")
    problems.clear()
    check_docs_site.check_sources(root, sources, problems)
    assert problems == []


def test_an_empty_source_directory_is_a_problem_not_a_pass(tmp_path):
    problems: list[str] = []
    check_docs_site.check_sources(site(tmp_path, "guide/"), tmp_path / "absent", problems)
    assert problems and "no Markdown pages" in problems[0]
