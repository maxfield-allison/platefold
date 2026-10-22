"""Inline Markdown: identical HTML, work that grows linearly, and Cancel."""

from __future__ import annotations

import random
import sys

import pytest
from markdown.inlinepatterns import BacktickInlineProcessor, LinkInlineProcessor

from platefold.config import Settings
from platefold.ingest import inline_scan
from platefold.ingest.markdown import ingest

# Code spans and their neighbours, written to catch differences in matching.
CORPUS = [
    "`code`",
    "``code with ` tick``",
    "```triple``",
    "`` ` ``",
    "`unclosed",
    "``a` b``",
    "`a``b`",
    "``` a `` b ```",
    "\\`not code\\`",
    "\\\\`code`",
    "`a\\`b`",
    "`line\nbreak`",
    "a `b\nc` d",
    "[`link`](http://example.com)",
    "[a](`b`)",
    "[a `]` b](c)",
    "![`image`](i.png)",
    "*`em`*",
    "`*not em*`",
    "**`a`**",
    "_`a`_",
    "`<b>`",
    "`&amp;`",
    "&amp; `&lt;` &copy;",
    "`a` `b` `c`",
    "``a``b``",
    "` a `",
    "``",
    "` `` `",
    "<http://example.com/a`b>",
    "`<http://example.com>`",
    "`a` \\` `b`",
    "`x`y`z`",
    "[a [b] c](d)",
    "[a [b c](d)",
    "[a]] [b]",
    "[[[a]]](b)",
    "[a](b) [c] [d][] [e][f]",
    "``` `` ` ```` ``",
    "text ````",
]

ATOMS = [
    "`",
    "``",
    "```",
    "\\`",
    "\\\\",
    "a",
    " ",
    "\n",
    "*",
    "_",
    "[",
    "]",
    "(",
    ")",
    "!",
    "<",
    ">",
    "&amp;",
    "&",
    "<b>",
    "</b>",
    "<http://x.y>",
    '"',
    "'",
]


def _documents() -> list[str]:
    rng = random.Random(75)
    generated = ["".join(rng.choice(ATOMS) for _ in range(rng.randint(1, 30))) for _ in range(250)]
    return CORPUS + generated


def _render(tmp_path, text: str) -> str:
    path = tmp_path / "inline.md"
    path.write_text(text, encoding="utf-8")
    return ingest(str(path), Settings()).body_html


def test_inline_html_is_unchanged(tmp_path, monkeypatch):
    documents = _documents()
    fast = [_render(tmp_path, text) for text in documents]
    monkeypatch.setattr(inline_scan, "install", lambda *_args: inline_scan.InlineScan())
    stock = [_render(tmp_path, text) for text in documents]
    assert fast == stock


def test_bracket_lookup_matches_python_markdown():
    rng = random.Random(75)
    processor = LinkInlineProcessor(r"\[")
    table = inline_scan.SuffixCache(inline_scan._match_brackets)
    get_text = inline_scan._get_text(processor.getText, table)
    for _ in range(3000):
        data = "".join(rng.choice("[]a ") for _ in range(rng.randint(1, 30)))
        for index in range(len(data) + 1):
            text, end, handled = processor.getText(data, index)
            if not handled and index and data[index - 1] == "[":
                # Every caller discards the text of an unclosed bracket.
                text = ""
            assert get_text(data, index) == (text, end, handled)


@pytest.mark.skipif(
    not hasattr(BacktickInlineProcessor, "find_code_spans"),
    reason="this Python-Markdown matches code spans with a regular expression",
)
def test_code_span_lookup_matches_python_markdown():
    rng = random.Random(75)
    processor = BacktickInlineProcessor(r"`")
    find = inline_scan._find_code_spans(processor.find_code_spans, inline_scan._FinalTicks())
    for _ in range(3000):
        text = "".join(rng.choice("``a ") for _ in range(rng.randint(1, 30)))
        for start in range(len(text)):
            if text[start] == "`":
                assert find(start, text) == processor.find_code_spans(start, text)


class _Scanned:
    """Count the characters each inline pattern's search passes over."""

    def __init__(self, compiled):
        self.compiled = compiled
        self.total = 0

    def finditer(self, data, pos=0):
        previous = pos
        for match in self.compiled.finditer(data, pos):
            self.total += match.start() - previous
            previous = match.end()
            yield match
        self.total += len(data) - previous

    def __getattr__(self, name):
        return getattr(self.compiled, name)


def _characters_searched(tmp_path, monkeypatch, text: str) -> int:
    counters: list[_Scanned] = []
    install = inline_scan.install

    def counting_install(parser, cancel_check=None):
        for processor in parser.inlinePatterns:
            counter = _Scanned(processor.compiled_re)
            processor.compiled_re = counter
            counters.append(counter)
        return install(parser, cancel_check)

    with monkeypatch.context() as patch:
        patch.setattr(inline_scan, "install", counting_install)
        _render(tmp_path, text)
    return sum(counter.total for counter in counters)


def _lines_executed(tmp_path, text: str) -> int:
    count = 0

    def trace(frame, event, _arg):
        nonlocal count
        if event == "line":
            count += 1
        return trace

    path = tmp_path / "inline.md"
    path.write_text(text, encoding="utf-8")
    previous = sys.gettrace()
    sys.settrace(trace)
    try:
        ingest(str(path), Settings())
    finally:
        sys.settrace(previous)
    return count


def _growth(measure, unit: str, count: int) -> float:
    return measure(unit * (2 * count)) / measure(unit * count)


# Each case doubles the paragraph. Linear work doubles too; work that scans the
# rest of the paragraph for every candidate quadruples.
@pytest.mark.parametrize("unit", ["`x` ", "<b>x</b> ", "*x* ", "[a](b) ", "&amp; "])
def test_pattern_searches_do_not_rescan_the_paragraph(tmp_path, monkeypatch, unit):
    growth = _growth(lambda text: _characters_searched(tmp_path, monkeypatch, text), unit, 400)
    assert growth < 2.3


@pytest.mark.parametrize("unit", ["[a ", "[", "`", "`x` ", "[a] "])
def test_rejected_candidates_do_not_rescan_the_paragraph(tmp_path, unit):
    growth = _growth(lambda text: _lines_executed(tmp_path, text), unit, 300)
    assert growth < 2.3


@pytest.mark.parametrize("unit", ["[a ", "[a](b) [c ", "[a] [b][c] ["])
def test_the_bracket_table_is_not_rebuilt_after_every_link(tmp_path, monkeypatch, unit):
    scans: list[inline_scan.InlineScan] = []
    install = inline_scan.install

    def recording(parser, cancel_check=None):
        scans.append(install(parser, cancel_check))
        return scans[-1]

    monkeypatch.setattr(inline_scan, "install", recording)
    text = unit * 2000
    _render(tmp_path, text)
    # One build per inline pattern that reads links, not one per link.
    assert 0 < scans[0].brackets.built <= 16 * len(text)


def test_a_table_is_reused_only_for_a_matching_ending():
    cache = inline_scan.SuffixCache(inline_scan._match_brackets)
    first = "x [a] [b] tail"
    closers, shift = cache.get(first, 2)
    assert (closers[2], shift) == (4, 0)
    # A replacement before the queried position keeps the table.
    second = "x \x02p\x03 [b] tail"
    closers, shift = cache.get(second, second.index("[b]"))
    assert closers[second.index("[b]") + shift] - shift == second.index("]", second.index("[b]"))
    assert cache.built == len(first)
    # A different ending forces a rebuild.
    third = "x [a] [b] tale"
    cache.get(third, 2)
    assert cache.built == len(first) + len(third)


class Stop(Exception):
    pass


def test_cancel_is_checked_within_one_paragraph(tmp_path):
    path = tmp_path / "long.md"
    path.write_text("[a " * 2000, encoding="utf-8")
    calls = 0

    def count():
        nonlocal calls
        calls += 1

    ingest(str(path), Settings(), count)
    assert calls >= 2000 // inline_scan.CANCEL_STRIDE

    def stop():
        raise Stop

    with pytest.raises(Stop):
        ingest(str(path), Settings(), stop)
