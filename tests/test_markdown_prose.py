"""Public Markdown keeps each prose paragraph on one source line.

tools/markdown_prose.py finds paragraphs split across source lines and joins
them. The unit cases pin what it must leave alone, and check with Python-Markdown
that joining changes the rendered HTML only in whitespace. The last tests hold
the repository's own public documents to the rule.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import markdown
import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "tools"))

import markdown_prose

REPO = Path(__file__).resolve().parents[1]
EXTENSIONS = ["abbr", "admonition", "attr_list", "def_list", "fenced_code", "footnotes", "tables"]


def render(text: str) -> str:
    html = markdown.markdown(text, extensions=EXTENSIONS)
    # Keep <pre> blocks exact; elsewhere any run of whitespace is one space.
    parts = re.split(r"(<pre>.*?</pre>)", html, flags=re.DOTALL)
    return "".join(part if part.startswith("<pre>") else " ".join(part.split()) for part in parts)


def assert_unchanged(text: str) -> None:
    assert markdown_prose.unwrap(text) == text
    assert markdown_prose.wrapped(text) == []


def assert_unwraps(text: str, expected: str) -> None:
    assert markdown_prose.unwrap(text) == expected
    assert markdown_prose.unwrap(expected) == expected, "a second unwrap must change nothing"
    assert markdown_prose.wrapped(expected) == []
    assert render(expected) == render(text)


def test_a_wrapped_paragraph_becomes_one_line_with_single_spaces():
    text = "# Title\n\nThe first line  \nsecond line\n   indented third.\n\nNext paragraph.\n"
    # Two trailing spaces are a hard break, so only the lines after it join.
    assert_unwraps(
        text, "# Title\n\nThe first line  \nsecond line indented third.\n\nNext paragraph.\n"
    )
    text = "One line that wraps \ninto a second\nand a third.\n"
    assert_unwraps(text, "One line that wraps into a second and a third.\n")
    assert markdown_prose.wrapped(text) == [(1, 3)]


def test_wording_and_line_endings_are_kept():
    assert_unwraps(
        "Keep *this* `code`\nand [a link](x.md).\r\n", "Keep *this* `code` and [a link](x.md).\r\n"
    )
    assert_unwraps(
        "Windows\r\nline endings\r\n\r\nstay.\r\n", "Windows line endings\r\n\r\nstay.\r\n"
    )
    assert_unwraps("No final\nnewline", "No final newline")


def test_fenced_code_is_left_alone():
    assert_unchanged("```console\nplatefold a.md\n-o pdfs\n\ntext that\nlooks like prose\n```\n")
    assert_unchanged("~~~~\n```\nstill\ncode\n```\n~~~~\n")
    # A paragraph after the fence still joins.
    assert_unwraps(
        "```\ncode\nmore\n```\nprose\nwrapped\n", "```\ncode\nmore\n```\nprose wrapped\n"
    )


def test_indented_code_is_left_alone():
    assert_unchanged("Prose.\n\n    indented code\n    continues\n\n    after a blank\n")


def test_list_items_become_one_line_each():
    text = (
        "- first item\n"
        "  wraps here\n"
        "- second item\n"
        "lazily continued\n"
        "  1. nested ordered\n"
        "     item wraps\n"
        "\n"
        "  A second paragraph\n"
        "  in the first item.\n"
        "\n"
        "Plain text.\n"
    )
    expected = (
        "- first item wraps here\n"
        "- second item lazily continued\n"
        "  1. nested ordered item wraps\n"
        "\n"
        "  A second paragraph in the first item.\n"
        "\n"
        "Plain text.\n"
    )
    assert_unwraps(text, expected)
    assert_unwraps("1) one\n   more\n2) two\n", "1) one more\n2) two\n")
    assert_unchanged("- item\n\n  ```sh\n  code in\n  the item\n  ```\n")
    assert_unchanged("-   **Card**\n\n    ---\n\n    Card text.\n")


def test_a_list_line_is_never_joined_onto_a_paragraph():
    # Python-Markdown reads this as one paragraph and CommonMark as a paragraph
    # and a list; leaving the line alone is right for both.
    assert_unchanged("A paragraph\n- then a list\n")
    assert_unchanged("A paragraph\n1. then a number\n")


def test_tables_are_left_alone():
    assert_unchanged("| a | b |\n| --- | --- |\n| 1 | 2 |\n")
    assert_unchanged("a | b\n--- | ---\n1 | 2\n")
    assert_unchanged("- in a list\n\n  | a |\n  | - |\n  | 1 |\n")


def test_headings_are_left_alone():
    assert_unchanged("# One\n## Two\nParagraph.\n")
    assert_unchanged("A setext heading\nover two lines\n---\n")
    assert_unchanged("Another\n===\n")
    assert_unwraps("Text\nwraps\n\n---\n\nAfter.\n", "Text wraps\n\n---\n\nAfter.\n")


def test_html_blocks_are_left_alone_and_comment_prose_joins():
    assert_unchanged("<details>\n<summary>More</summary>\nhidden\ntext\n</details>\n")
    assert_unchanged('<div class="grid" markdown>\n\nCard.\n\n</div>\n')
    assert_unchanged("<!-- platefold:generate cli-help -->\n")
    assert_unchanged("<!-- an inline comment\nthat wraps -->\n")
    assert_unwraps(
        "<!--\nA template hint that\nwraps.\n-->\n", "<!--\nA template hint that wraps.\n-->\n"
    )
    # An autolink at the start of a line is text, not HTML.
    assert_unwraps("See\n<https://example.com>.\n", "See <https://example.com>.\n")


def test_front_matter_is_left_alone():
    assert_unchanged("---\ntitle: A title\ndescription: long text\n  continued\n---\n\nBody.\n")
    assert_unwraps("---\ntitle: x\n---\n\nBody\nwraps.\n", "---\ntitle: x\n---\n\nBody wraps.\n")


def test_definitions_references_and_attributes_are_left_alone():
    assert_unchanged("[home]: https://example.com\n[docs]: https://example.com/docs\n")
    assert_unchanged('[home]:\n  https://example.com\n  "Title"\n')
    assert_unchanged("Text[^1].\n\n[^1]: A footnote\n    continued.\n")
    assert_unchanged("*[PDF]: Portable Document Format\n*[CLI]: command line\n")
    assert_unchanged("Paragraph.\n{: .note }\n")
    assert_unchanged('--8<-- "sample.md"\n--8<-- "other.md"\n')


def test_definition_lists_keep_terms_and_join_definitions():
    text = "`SHA256SUMS`\n`SHA256SUMS.minisig`\n: The hash of every\n  file in the release.\n"
    expected = "`SHA256SUMS`\n`SHA256SUMS.minisig`\n: The hash of every file in the release.\n"
    assert_unwraps(text, expected)


def test_admonitions_and_tabs_keep_their_indented_body():
    text = '!!! note "Title"\n\n    Body text\n    that wraps.\n\n    ```\n    code\n    lines\n    ```\n'
    expected = (
        '!!! note "Title"\n\n    Body text that wraps.\n\n    ```\n    code\n    lines\n    ```\n'
    )
    assert_unwraps(text, expected)
    assert_unwraps(
        "!!! note\n    Straight after\n    the header.\n",
        "!!! note\n    Straight after the header.\n",
    )
    tabs = '=== "Linux"\n\n    A match prints\n    OK.\n\n=== "macOS"\n\n    Same.\n'
    assert (
        markdown_prose.unwrap(tabs)
        == '=== "Linux"\n\n    A match prints OK.\n\n=== "macOS"\n\n    Same.\n'
    )


@pytest.mark.parametrize("ending", ["  ", "\\", "<br>"])
def test_explicit_hard_breaks_stay(ending):
    text = f"Line one{ending}\nline two\nline three{ending}\nline four\n"
    expected = f"Line one{ending}\nline two line three{ending}\nline four\n"
    assert_unwraps(text, expected)


def test_block_quotes_are_left_alone():
    assert_unchanged("> A quote\n> that wraps\nlazily.\n")


def test_images_alone_on_a_line_keep_their_lines():
    text = "![Light](a.svg#only-light){ .figure }\n![Dark](a.svg#only-dark){ .figure }\n"
    assert_unchanged(text)
    assert_unwraps(
        "Intro\ntext\n![Figure](f.svg)\nafter\nit\n", "Intro text\n![Figure](f.svg)\nafter it\n"
    )


def test_check_lists_each_wrapped_paragraph_and_unwrap_rewrites(tmp_path, capsys):
    page = tmp_path / "page.md"
    page.write_bytes(b"Ok.\n\nWrapped\nprose.\n\n```\nnot\nprose\n```\n")
    assert markdown_prose.main(["check", str(page)]) == 1
    assert capsys.readouterr().out == f"{page}:3: paragraph spans 2 source lines\n"
    assert markdown_prose.main(["unwrap", str(page)]) == 0
    assert page.read_bytes() == b"Ok.\n\nWrapped prose.\n\n```\nnot\nprose\n```\n"
    capsys.readouterr()
    assert markdown_prose.main(["check", str(page)]) == 0
    assert capsys.readouterr().out == ""


def test_public_set_holds_first_party_documents_only():
    files = {path.relative_to(REPO).as_posix() for path in markdown_prose.public_markdown()}
    for name in ("README.md", "SECURITY.md", "docs/index.md", "packaging/linux/README.md"):
        assert name in files
    for name in (
        "CODE_OF_CONDUCT.md",
        "SAMPLE.md",
        "docs/samples/ladder-tour.md",
        "desktop/src-tauri/vendor/tauri-plugin-updater/README.md",
    ):
        assert name not in files
    assert not any(name.startswith("tests/") for name in files)


def test_public_markdown_has_no_wrapped_paragraphs():
    found = [
        f"{path.relative_to(REPO).as_posix()}:{line}"
        for path in markdown_prose.public_markdown()
        for line, _count in markdown_prose.wrapped(path.read_text(encoding="utf-8"))
    ]
    assert found == [], "run `python tools/markdown_prose.py unwrap`: " + ", ".join(found)
