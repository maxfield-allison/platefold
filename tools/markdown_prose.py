#!/usr/bin/env python3
"""Keep the prose in public Markdown on one source line per paragraph.

A paragraph split across fixed-width source lines renders the same as one
written on a single line, but every edit reflows its neighbours and makes the
diff harder to read. This finds such paragraphs and joins them:

    python tools/markdown_prose.py check     # list wrapped paragraphs; exit 1 if any
    python tools/markdown_prose.py unwrap    # join them in place
    python tools/markdown_prose.py files     # list the files checked by default

`check` and `unwrap` take optional file paths; the default is the public
first-party set defined below. Joining puts one space between a paragraph's
lines and changes nothing else, so rendered HTML differs only in whitespace
inside paragraphs. Running `unwrap` a second time changes nothing.

What is joined: ordinary paragraphs, including those inside list items (each
item's paragraph becomes one line), admonitions, content tabs and multi-line
HTML comments whose `<!--` and `-->` sit on lines of their own. What is left
exactly as written: front matter, fenced and indented code, headings, tables,
thematic breaks, other HTML blocks, block quotes, link reference and
abbreviation definitions, attribute lists, snippet includes, definition-list
terms, images that stand alone on their line, and explicit hard breaks (two
trailing spaces, a trailing backslash or a trailing `<br>`), which end a
paragraph's line at the same place. When a line
might start another block in one Markdown dialect, it is not joined.

Standard library only.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterator, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# The public first-party Markdown: these files at the repository root, and every
# Markdown file under these directories apart from the exclusions below. The
# other Markdown at the root is left alone on purpose: CODE_OF_CONDUCT.md is the
# adopted Contributor Covenant text, and SAMPLE.md is an export input whose
# single line breaks Plate|Fold renders as written (soft breaks are on by
# default), so joining its lines would change the PDF.
ROOT_FILES = ("CHANGELOG.md", "CONTRIBUTING.md", "README.md", "SECURITY.md", "THEMING.md")
DIRECTORIES = (".github", "desktop", "docs", "packaging", "tools")
EXCLUDED = (
    # The updater crate copied from crates.io, and the permission reference its
    # build generates.
    "desktop/src-tauri/vendor/",
    # The sample PDF's export input. Its receipt pins the source bytes, and its
    # line breaks are rendered, like SAMPLE.md's.
    "docs/samples/",
)
# Build output and dependency trees, never first-party documents.
SKIPPED_DIRECTORIES = frozenset({"__pycache__", "build", "dist", "node_modules", "target"})

# Block starts. Each allows the up-to-three spaces of indentation Markdown does.
FENCE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})")
# Python-Markdown reads a heading without a space after the hashes, so any
# leading hash counts.
ATX_HEADING = re.compile(r"^ {0,3}#")
SETEXT_UNDERLINE = re.compile(r"^ {0,3}(?:=+|-+)[ \t]*$")
THEMATIC_BREAK = re.compile(r"^ {0,3}(?:(?:\*[ \t]*){3,}|(?:-[ \t]*){3,}|(?:_[ \t]*){3,})$")
LIST_ITEM = re.compile(r"^(?P<marker> {0,3}(?:[-*+]|\d{1,9}[.)]))(?P<space>[ \t]+|$)")
DEFINITION = re.compile(r"^(?P<marker> {0,3}:)(?P<space>[ \t]+)")
BLOCK_QUOTE = re.compile(r"^ {0,3}>")
# A tag, closing tag, comment, declaration or processing instruction. An
# autolink such as <https://example.com> is inline text.
HTML = re.compile(r"^ {0,3}<(?:[A-Za-z][A-Za-z0-9-]*(?:[\s/>]|$)|[/!?])")
TABLE_ROW = re.compile(r"^ {0,3}\|")
# Link references and footnotes ([id]: ...), and abbreviations (*[HTML]: ...).
DEFINITION_LINE = re.compile(r"^ {0,3}\*?\[[^\]]+\]:")
ATTRIBUTE_LIST = re.compile(r"^ {0,3}\{.*\}[ \t]*$")
SNIPPET = re.compile(r"^ {0,3}-{2,}8<-{2,}")
TOC_MARKER = re.compile(r"^ {0,3}\[TOC\][ \t]*$")
# Admonitions (!!!), collapsible blocks (??? and ???+) and content tabs (===).
# Their body is indented by four spaces.
CONTAINER = re.compile(r"^ {0,3}(?:!!!|\?\?\?\+?|===\+?)[ \t]")
CONTAINER_INDENT = 4
HARD_BREAK = re.compile(r"(?:  |\\|<br\s*/?>)$", re.IGNORECASE)
# An image alone on its line, such as a light and a dark variant stacked in one
# paragraph. Each keeps its own line.
IMAGE_LINE = re.compile(r"^ {0,3}!\[[^\]]*\]\([^)]*\)(?:\{[^}]*\})?[ \t]*$")

# One line of a block being scanned: its index in the file, and its text with the
# enclosing containers' indentation removed and tabs expanded.
Line = tuple[int, str]


def public_markdown(root: Path = ROOT) -> list[Path]:
    """The public first-party Markdown files under root, sorted."""
    found = [root / name for name in ROOT_FILES if (root / name).is_file()]
    for directory in DIRECTORIES:
        for current, subdirectories, filenames in os.walk(root / directory):
            subdirectories[:] = sorted(
                name
                for name in subdirectories
                if not name.startswith(".") and name not in SKIPPED_DIRECTORIES
            )
            for filename in filenames:
                path = Path(current, filename)
                relative = path.relative_to(root).as_posix()
                if filename.endswith(".md") and not relative.startswith(EXCLUDED):
                    found.append(path)
    return sorted(found)


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" "))


def _blank(text: str) -> bool:
    return not text.strip()


def _opens_block(text: str) -> bool:
    """Whether a line starts something other than paragraph text."""
    return any(
        pattern.match(text)
        for pattern in (
            ATX_HEADING,
            FENCE,
            THEMATIC_BREAK,
            LIST_ITEM,
            DEFINITION,
            BLOCK_QUOTE,
            HTML,
            TABLE_ROW,
            DEFINITION_LINE,
            ATTRIBUTE_LIST,
            SNIPPET,
            TOC_MARKER,
            CONTAINER,
        )
    )


def _is_table_delimiter(text: str) -> bool:
    return "|" in text and "-" in text and re.fullmatch(r"[ \t|:-]+", text) is not None


def _until_blank(lines: Sequence[Line], start: int) -> int:
    end = start + 1
    while end < len(lines) and not _blank(lines[end][1]):
        end += 1
    return end


def _fence_end(lines: Sequence[Line], start: int) -> int:
    match = FENCE.match(lines[start][1])
    assert match is not None
    fence = match["fence"]
    closing = re.compile(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}[ \t]*$")
    for end in range(start + 1, len(lines)):
        if closing.match(lines[end][1]):
            return end + 1
    return len(lines)


def _indented_code_end(lines: Sequence[Line], start: int) -> int:
    end = start
    while end < len(lines) and (_blank(lines[end][1]) or _indent(lines[end][1]) >= 4):
        end += 1
    return end


def _container_end(lines: Sequence[Line], start: int) -> int:
    """The end of the indented body that follows a container's header line."""
    end = start + 1
    for index in range(start + 1, len(lines)):
        text = lines[index][1]
        if _blank(text):
            continue
        if _indent(text) < CONTAINER_INDENT:
            break
        end = index + 1
    return end


def _item_width(text: str, match: re.Match[str]) -> int:
    """The column where a list item's or definition's content starts."""
    marker = len(match["marker"])
    space = len(match["space"])
    # No content on the marker line, or content that is itself indented code:
    # the content column is one space past the marker.
    if space == 0 or space > 4 or _blank(text[marker:]):
        return marker + 1
    return marker + space


def _item_end(lines: Sequence[Line], start: int, width: int) -> int:
    """The end of a list item or definition that starts at lines[start]."""
    end = start + 1
    fence: re.Pattern[str] | None = None
    previous_blank = False
    for index in range(start + 1, len(lines)):
        text = lines[index][1]
        if _blank(text):
            previous_blank = True
            continue
        if _indent(text) >= width:
            inner = text[width:]
            if fence is not None:
                if fence.match(inner):
                    fence = None
            elif match := FENCE.match(inner):
                mark = match["fence"]
                fence = re.compile(rf"^ {{0,3}}{re.escape(mark[0])}{{{len(mark)},}}[ \t]*$")
        # A less indented line continues the item only as a lazy continuation of
        # its paragraph: straight after text, and not the start of another block.
        elif fence is not None or previous_blank or _opens_block(text):
            break
        previous_blank = False
        end = index + 1
    return end


def _item_body(lines: Sequence[Line], start: int, end: int, width: int) -> list[Line]:
    body = [(lines[start][0], lines[start][1][width:])]
    for index, text in lines[start + 1 : end]:
        body.append((index, text[width:] if _indent(text) >= width else text.lstrip(" ")))
    return body


def _hard_break_groups(lines: Sequence[Line]) -> Iterator[list[int]]:
    """Split a paragraph at its explicit breaks and images; yield runs to join."""
    group: list[int] = []
    for index, text in lines:
        image = IMAGE_LINE.match(text) is not None
        if image or HARD_BREAK.search(text):
            if not image:
                group.append(index)
            if len(group) > 1:
                yield group
            group = []
        else:
            group.append(index)
    if len(group) > 1:
        yield group


def _paragraph_end(lines: Sequence[Line], start: int) -> int:
    end = start + 1
    while end < len(lines):
        text = lines[end][1]
        if _blank(text) or SETEXT_UNDERLINE.match(text) or _opens_block(text):
            break
        end += 1
    return end


def _html_end(lines: Sequence[Line], start: int) -> tuple[int, list[Line]]:
    """The end of an HTML block, and any comment text inside it that is prose."""
    text = lines[start][1].strip()
    if text == "<!--":
        for end in range(start + 1, len(lines)):
            if lines[end][1].strip() == "-->":
                return end + 1, list(lines[start + 1 : end])
    if text.startswith("<!--"):
        for end in range(start, len(lines)):
            if "-->" in lines[end][1]:
                return end + 1, []
        return len(lines), []
    return _until_blank(lines, start), []


def _verbatim_end(lines: Sequence[Line], index: int) -> int | None:
    """The end of a block left exactly as written, if one starts at index."""
    text = lines[index][1]
    if FENCE.match(text):
        return _fence_end(lines, index)
    if _indent(text) >= 4:
        return _indented_code_end(lines, index)
    if BLOCK_QUOTE.match(text) or TABLE_ROW.match(text) or DEFINITION_LINE.match(text):
        return _until_blank(lines, index)
    if _opens_block(text) and not (
        CONTAINER.match(text) or LIST_ITEM.match(text) or DEFINITION.match(text) or HTML.match(text)
    ):
        return index + 1
    return None


def _nested(lines: Sequence[Line], index: int) -> tuple[int, list[Line]] | None:
    """A block holding Markdown that starts at index: its end, and its content."""
    text = lines[index][1]
    if CONTAINER.match(text):
        end = _container_end(lines, index)
        return end, [(line, inner[CONTAINER_INDENT:]) for line, inner in lines[index + 1 : end]]
    if match := LIST_ITEM.match(text) or DEFINITION.match(text):
        width = _item_width(text, match)
        end = _item_end(lines, index, width)
        return end, _item_body(lines, index, end, width)
    if HTML.match(text):
        return _html_end(lines, index)
    return None


def _paragraph(lines: Sequence[Line], index: int) -> tuple[int, list[list[int]]]:
    """The end of the paragraph starting at index, and its runs to join."""
    end = _paragraph_end(lines, index)
    following = lines[end][1] if end < len(lines) else ""
    if SETEXT_UNDERLINE.match(following):
        # A setext heading: its text and its underline stay as written.
        return end + 1, []
    if DEFINITION.match(following):
        # Definition-list terms, one per line.
        return end, []
    if any(_is_table_delimiter(line) for _, line in lines[index:end]):
        return end, []
    return end, list(_hard_break_groups(lines[index:end]))


def _paragraphs(lines: Sequence[Line]) -> Iterator[list[int]]:
    """Yield the line indices of each run of paragraph lines to join into one."""
    index = 0
    while index < len(lines):
        if _blank(lines[index][1]):
            index += 1
        elif (end := _verbatim_end(lines, index)) is not None:
            index = end
        elif nested := _nested(lines, index):
            index, body = nested
            yield from _paragraphs(body)
        elif SETEXT_UNDERLINE.match(lines[index][1]):
            index += 1
        else:
            index, groups = _paragraph(lines, index)
            yield from groups


def _front_matter_end(lines: Sequence[Line]) -> int:
    if not lines or lines[0][1].rstrip() != "---":
        return 0
    for index in range(1, len(lines)):
        if lines[index][1].rstrip() in ("---", "..."):
            return index + 1
    return 0


def _groups(text: str) -> tuple[list[str], list[list[int]]]:
    lines = text.splitlines(keepends=True)
    view = [(index, line.rstrip("\r\n").expandtabs(4)) for index, line in enumerate(lines)]
    start = _front_matter_end(view)
    return lines, list(_paragraphs(view[start:]))


def wrapped(text: str) -> list[tuple[int, int]]:
    """Each wrapped paragraph's first line (1-based) and its number of lines."""
    return [(group[0] + 1, len(group)) for group in _groups(text)[1]]


def unwrap(text: str) -> str:
    """Return text with each wrapped paragraph joined onto one line."""
    lines, groups = _groups(text)
    replacement: dict[int, str] = {}
    for group in groups:
        bodies = [lines[index].rstrip("\r\n") for index in group]
        parts = [bodies[0].rstrip(" \t")]
        parts += [body.strip(" \t") for body in bodies[1:-1]]
        # The last line keeps its trailing whitespace, which may be a hard break.
        parts.append(bodies[-1].lstrip(" \t"))
        ending = lines[group[-1]][len(bodies[-1]) :]
        replacement[group[0]] = " ".join(parts) + ending
        for index in group[1:]:
            replacement[index] = ""
    return "".join(replacement.get(index, line) for index, line in enumerate(lines))


def _display(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find or join Markdown paragraphs that are split across source lines."
    )
    parser.add_argument("mode", choices=("check", "unwrap", "files"))
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Markdown files to read (default: the public first-party set)",
    )
    args = parser.parse_args(argv)
    paths = args.paths or public_markdown()

    if args.mode == "files":
        for path in paths:
            print(_display(path))
        return 0

    found = 0
    for path in paths:
        # newline="" keeps each line's own ending, so unwrap rewrites nothing else.
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
        if args.mode == "check":
            for line, count in wrapped(text):
                print(f"{_display(path)}:{line}: paragraph spans {count} source lines")
                found += 1
            continue
        joined = unwrap(text)
        if joined != text:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(joined)
            print(f"unwrapped {_display(path)}")

    if found:
        print(
            f"{found} wrapped paragraph(s); run `python tools/markdown_prose.py unwrap`",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
