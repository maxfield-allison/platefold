"""Link destinations and emphasis: identical HTML, predictions that match, linear work."""

from __future__ import annotations

import random
import re
import sys

import markdown
import pytest
from markdown.inlinepatterns import LinkInlineProcessor

from platefold.config import Settings
from platefold.ingest import inline_lookups
from platefold.ingest.inline_scan import SuffixCache
from platefold.ingest.markdown import ingest

CORPUS = [
    "[a](b)",
    "[a](b c)",
    "[a](<b c>)",
    "[a](b 'title')",
    '[a](b "title")',
    "[a](b 'ti)tle')",
    "[a](b (c) d)",
    "[a](b (c d)",
    '[a](b "c) d',
    "[a](b 'c\" d) e')",
    '[a](b"(',
    "[a](b'x'(y)",
    "[a](",
    "[a]( b )",
    "![i](j 'k')",
    "![i](j (k)",
    "*a*",
    "**a**",
    "***a***",
    "***a* b**",
    "***a** b*",
    "**a *b* c**",
    "*a **b** c*",
    "*a",
    "**a",
    "_a_",
    "__a__",
    "___a___",
    "___a_ b__",
    "___a__ b_",
    "__a _b_ c__",
    "_a __b__ c_",
    "snake_case_name",
    "a_b _c_ d_e",
    "_a",
    "__a",
    "x_ _y",
    "_é_",
    "_ a _",
    "*a\nb*",
    "_a\n\nb_",
    "**a * b**",
    "\\*a*",
    "[*a*](b)",
    "*[a](b)*",
]

ATOMS = ["*", "**", "_", "__", "a", " ", "é", "\n", "[", "]", "(", ")", "'", '"', "!", "\\", "x_y"]


def _documents() -> list[str]:
    rng = random.Random(81)
    generated = ["".join(rng.choice(ATOMS) for _ in range(rng.randint(1, 30))) for _ in range(250)]
    return CORPUS + generated


def _render(tmp_path, text: str) -> str:
    path = tmp_path / "inline.md"
    path.write_text(text, encoding="utf-8")
    return ingest(str(path), Settings()).body_html


def test_inline_html_is_unchanged(tmp_path, monkeypatch):
    documents = _documents()
    fast = [_render(tmp_path, text) for text in documents]
    monkeypatch.setattr(inline_lookups, "install", lambda _parser: inline_lookups.InlineLookups())
    stock = [_render(tmp_path, text) for text in documents]
    assert fast == stock


def test_link_predictions_match_python_markdown():
    rng = random.Random(81)
    parser = markdown.Markdown()
    # getLink unescapes through the inline processor's stash, set up per run.
    parser.treeprocessors["inline"].stashed_nodes = {}
    processor = LinkInlineProcessor(r"\[", parser)
    get_link = inline_lookups._get_link(
        processor.getLink, processor.RE_LINK, SuffixCache(inline_lookups._LinkTables)
    )
    for _ in range(4000):
        data = "".join(rng.choice("()'\" a") for _ in range(rng.randint(1, 24)))
        for index in range(len(data)):
            expected = processor.getLink(data, index)
            got = get_link(data, index)
            assert got[3] == expected[3], (data, index)
            if expected[3]:
                assert got == expected


@pytest.mark.parametrize("pattern", sorted(inline_lookups.PREDICTIONS))
def test_emphasis_predictions_are_exact(pattern):
    char, predict = inline_lookups.PREDICTIONS[pattern]
    compiled = re.compile(pattern, re.DOTALL | re.UNICODE)
    cache = SuffixCache(lambda text: inline_lookups._EmphasisTables(text, char))
    rng = random.Random(81)
    alphabet = [char, char * 2, char * 3, "a", " ", "é", "\n", "."]
    for _ in range(3000):
        text = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 16)))
        for pos in range(len(text)):
            tables, shift = cache.get(text, pos)
            assert predict(text, pos, tables, shift) == (compiled.match(text, pos) is not None), (
                text,
                pos,
            )


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


class _Searched:
    """Counts the characters each emphasis expression passes over."""

    total = 0

    def __init__(self, compiled):
        self.compiled = compiled

    def match(self, string, pos=0, *rest):
        found = self.compiled.match(string, pos, *rest)
        # A failed match has examined up to the end of the text.
        _Searched.total += (found.end() if found else len(string)) - pos
        return found

    def __getattr__(self, name):
        return getattr(self.compiled, name)


def _characters_matched(tmp_path, monkeypatch, text: str) -> int:
    _Searched.total = 0
    install = inline_lookups.install

    def counting(parser):
        lookups = install(parser)
        for processor in parser.inlinePatterns:
            for item in getattr(processor, "PATTERNS", []):
                predicted = item.pattern
                predicted._compiled = _Searched(predicted._compiled)
        return lookups

    with monkeypatch.context() as patch:
        patch.setattr(inline_lookups, "install", counting)
        _render(tmp_path, text)
    return _Searched.total


# Doubling the paragraph doubles linear work and quadruples work that scans
# the rest of the paragraph for every rejected candidate.
@pytest.mark.parametrize("unit", ["[a](", "[a](b ", '[a](b" ', "[a](b) [c](", "[a](b 'c' ("])
def test_rejected_destinations_do_not_rescan_the_paragraph(tmp_path, unit):
    assert _lines_executed(tmp_path, unit * 600) / _lines_executed(tmp_path, unit * 300) < 2.3


@pytest.mark.parametrize(
    "unit", ["_a ", "__a ", "___a ", "*a ", "**a ", "***a ", "a_b ", "_a_ *b "]
)
def test_rejected_emphasis_does_not_rescan_the_paragraph(tmp_path, monkeypatch, unit):
    small = _characters_matched(tmp_path, monkeypatch, unit * 400)
    large = _characters_matched(tmp_path, monkeypatch, unit * 800)
    assert large <= 2.3 * max(small, 1)


def test_tables_are_not_rebuilt_after_every_link(tmp_path, monkeypatch):
    recorded: list[inline_lookups.InlineLookups] = []
    install = inline_lookups.install

    def recording(parser):
        recorded.append(install(parser))
        return recorded[-1]

    monkeypatch.setattr(inline_lookups, "install", recording)
    text = "[a](b) [c](d " * 1500 + "_e_ *f* _g " * 1500
    _render(tmp_path, text)
    lookups = recorded[0]
    built = lookups.links.built + sum(cache.built for cache in lookups.emphasis.values())
    assert 0 < built <= 32 * len(text)
