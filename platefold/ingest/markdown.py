"""Read a Markdown file into the shape the exporter renders.

This is the original conversion path, unchanged, moved behind the `Ingested`
contract so other formats can join it. Mermaid fences leave placeholders
rather than diagrams: rendering one needs the renderer and the document's
resource budget, which belong to `Document`.

Python-Markdown converts a whole document in one call, and a large source can
keep it busy for minutes. `_Cancellation` hooks that call through the
library's extension points so Cancel is honoured per block, per run of inline
text and between processing stages.
"""

from __future__ import annotations

import re
from typing import Any, cast

import markdown
from markdown.blockprocessors import BlockProcessor
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
from markdown.postprocessors import Postprocessor
from markdown.treeprocessors import Treeprocessor

from ..cancellation import CancelCheck, Throttle, never_cancelled
from ..config import Settings
from ..limits import read_limited_text
from ..placeholders import Placeholders
from . import Ingested, heading_ids, inline_lookups, inline_scan, markdown_limits

# A fenced block of any length, optionally indented, with an optional info
# string. Tracking the opening run length matters: a ```` ```` fence can
# legitimately contain ``` lines.
FENCE_OPEN = re.compile(r"^([ \t]*)(`{3,})[ \t]*([^\s`]*)[ \t]*$")
META_LINE = re.compile(r"^([A-Z][A-Za-z /]{2,28}):\s+(.+)$")


def heading_text(raw: str) -> str:
    return re.sub(r"\s*\{.*?\}\s*$", "", re.sub(r"^#+\s*", "", raw).strip())


def split_metadata(lines: list[str]) -> tuple[str | None, list[tuple[str, str]] | None, list[str]]:
    """Pull an H1 title plus the `Key: value` block that follows it."""
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    title = None
    if i < len(lines) and lines[i].startswith("# "):
        title = lines[i][2:].strip()
        i += 1
    while i < len(lines) and not lines[i].strip():
        i += 1
    meta, k = [], i
    while k < len(lines):
        m = META_LINE.match(lines[k])
        if not m:
            break
        meta.append((m.group(1), m.group(2)))
        k += 1
    if title is None or (len(meta) < 2 and not any(k.lower() == "language" for k, _ in meta)):
        return None, None, lines
    rest = lines[k:]
    while rest and (not rest[0].strip() or rest[0].strip() == "---"):
        rest.pop(0)
    return title, meta, rest


class _BlockCheckpoint(BlockProcessor):
    """Consulted before every block; checks for cancellation and never claims one."""

    def __init__(self, parser, tick: CancelCheck):
        super().__init__(parser)
        self.tick = tick

    def test(self, parent, block) -> bool:
        self.tick()
        return False

    def run(self, parent, blocks) -> bool:  # pragma: no cover - test() never accepts
        return False


class _InlineCheckpoint(InlineProcessor):
    """Tried at the start of every text run; checks for cancellation and never matches."""

    def __init__(self, tick: CancelCheck):
        # \A matches only when a run is first scanned, not at later offsets.
        super().__init__(r"\A")
        self.tick = tick

    def handleMatch(self, m, data):
        self.tick()
        return None, None, None


class _StageCheckpoint(Treeprocessor):
    def __init__(self, md, cancel_check: CancelCheck):
        super().__init__(md)
        self.cancel_check = cancel_check

    def run(self, root) -> None:
        self.cancel_check()


class _TextCheckpoint(Postprocessor):
    def __init__(self, md, cancel_check: CancelCheck):
        super().__init__(md)
        self.cancel_check = cancel_check

    def run(self, text: str) -> str:
        self.cancel_check()
        return text


class _Cancellation(Extension):
    """Check for cancellation while Python-Markdown converts a document."""

    # Between the built-in and enabled extension tree stages: code highlighting
    # (30), inline (20), prettify (10), attribute lists (8), contents (5).
    STAGES = (100, 25, 15, 9, 6, 1)

    def __init__(self, cancel_check: CancelCheck):
        super().__init__()
        self.cancel_check = cancel_check

    def extendMarkdown(self, md) -> None:
        tick = Throttle(self.cancel_check, 64)
        md.parser.blockprocessors.register(
            _BlockCheckpoint(md.parser, tick), "platefold_cancel", 1000
        )
        md.inlinePatterns.register(_InlineCheckpoint(tick), "platefold_cancel", 1000)
        for priority in self.STAGES:
            md.treeprocessors.register(
                _StageCheckpoint(md, self.cancel_check), f"platefold_cancel_{priority}", priority
            )
        md.postprocessors.register(_TextCheckpoint(md, self.cancel_check), "platefold_cancel", 100)


def extract_mermaid(
    text: str, cancel_check: CancelCheck = never_cancelled, *, placeholders: Placeholders
) -> tuple[str, list[dict]]:
    """Swap mermaid fences for placeholders, noting the nearest heading.

    Non-mermaid fenced blocks are copied through untouched, including any
    ```mermaid lines nested inside them - documentation about mermaid should
    not be rendered as a diagram.
    """
    # Inputs commonly arrive from Windows even when the export runs on Linux.
    # Preserve the old trailing-line behaviour while removing CR characters
    # that would otherwise stop the anchored fence expressions from matching.
    lines = re.split(r"\r\n?|\n", text)
    out: list[str] = []
    figs: list[dict] = []
    tick = Throttle(cancel_check, 4096)
    i, head = 0, ""
    while i < len(lines):
        tick()
        line = lines[i]
        opener = FENCE_OPEN.match(line)
        if opener:
            ticks, info = opener.group(2), opener.group(3).lower()
            closer = re.compile(r"^[ \t]*`{%d,}[ \t]*$" % len(ticks))
            j, body = i + 1, []
            while j < len(lines) and not closer.match(lines[j]):
                body.append(lines[j])
                j += 1
            if info == "mermaid":
                figs.append({"code": "\n".join(body) + "\n", "head": head})
                out.append("\n%s\n" % placeholders.figure(len(figs) - 1))
            else:
                out.append(line)
                out.extend(body)
                if j < len(lines):
                    out.append(lines[j])
            i = j + 1
            continue
        if line.startswith("#"):
            head = heading_text(line)
        out.append(line)
        i += 1
    return "\n".join(out), figs


def _outline(parser: markdown.Markdown) -> list[tuple[int, str, str]]:
    """Flatten the parser's heading tree to the two levels the exporter maps."""
    flat: list[tuple[int, str, str]] = []

    def walk(items: list[dict[str, Any]], level: int) -> None:
        for item in items:
            if level <= 2:
                flat.append((level, item["name"], item["id"]))
            walk(item.get("children", []), level + 1)

    tokens = cast(list[dict[str, Any]], getattr(parser, "toc_tokens", []))
    walk(tokens, 1)
    return flat


def ingest(path: str, settings: Settings, cancel_check: CancelCheck | None = None) -> Ingested:
    cancel_check = cancel_check or never_cancelled
    cancel_check()
    raw = read_limited_text(
        path,
        settings.limits.max_markdown_bytes,
        label="Markdown file",
    )
    placeholders = Placeholders.unused_in([raw])
    body_md, figures = extract_mermaid(raw, cancel_check, placeholders=placeholders)
    title, metadata, rest = split_metadata(body_md.split("\n"))
    if title:
        body_md = "\n".join(rest)

    extensions: list[str | Extension] = [
        _Cancellation(cancel_check),
        "tables",
        "fenced_code",
        "codehilite",
        "sane_lists",
        "attr_list",
        "toc",
        "md_in_html",
    ]
    if settings.soft_breaks:
        extensions.append("nl2br")
    parser = markdown.Markdown(
        extensions=extensions,
        extension_configs={"codehilite": {"guess_lang": False}, "toc": {"toc_depth": "1-3"}},
    )
    inline_scan.install(parser, cancel_check)
    inline_lookups.install(parser)
    markdown_limits.install(parser, settings.limits)
    # The contents extension assigns every heading id in one call; the scope
    # makes that linear in the number of headings and lets Cancel stop it.
    with heading_ids.scope(cancel_check):
        body_html = parser.convert(body_md)
    return Ingested(
        body_html=body_html,
        title=title,
        metadata=metadata or [],
        language=next((v for k, v in metadata or [] if k.lower() == "language"), None),
        outline=_outline(parser),
        figures=figures,
        placeholders=placeholders,
    )
