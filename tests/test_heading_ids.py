"""Heading ids: identical to Python-Markdown's, in linear time, and cancellable."""

from __future__ import annotations

import copy
import random
import re

import pytest
from markdown.extensions import toc

from platefold.config import Settings
from platefold.ingest import heading_ids
from platefold.ingest.docx import _sanitize
from platefold.ingest.heading_ids import STOCK_NEST_TOC_TOKENS, STOCK_UNIQUE, HeadingIds
from platefold.ingest.markdown import ingest

# Slugs that collide with each other and with the suffixes `unique` generates.
TRICKY = [
    "",
    "_",
    "_1",
    "_2",
    "_0",
    "a",
    "a_",
    "a_0",
    "a_1",
    "a_2",
    "a_01",
    "a_1_1",
    "a_1_2",
    "a__1",
    "a-1",
    "a-1_1",
    "b_9",
    "b_10",
    "1",
    "é",
    "日本語",
    "a\nb",
    "a_1\n",
]

SEQUENCES = [
    ["a"] * 5,
    [""] * 4,
    ["a", "a_1", "a", "a_1", "a"],
    ["a_1", "a", "a", "a"],
    ["a_2", "a", "a", "a", "a_2"],
    ["a_01", "a_01", "a_1", "a"],
    ["b_9", "b_9", "b_10", "b_9"],
    ["_1", "", "", "_1"],
    ["a_1_1", "a_1", "a_1", "a_1_1"],
    ["a-1", "a", "a-1", "a"],
    ["a\nb", "a\nb", "a\nb_1", "a\nb"],
    ["a_1\n", "a_1\n", "a", "a"],
]


def _stock(sequence, taken=()):
    ids = set(taken)
    return [STOCK_UNIQUE(item, ids) for item in sequence], ids


def _linear(sequence, taken=()):
    allocator = HeadingIds(set(taken))
    return [allocator(item) for item in sequence], allocator.ids


@pytest.mark.parametrize("sequence", SEQUENCES)
@pytest.mark.parametrize("taken", [(), ("a_1",), ("a", "a_2", "a_3"), ("_1", "b_10")])
def test_ids_match_python_markdown_for_colliding_names(sequence, taken):
    assert _linear(sequence, taken) == _stock(sequence, taken)


def test_ids_match_python_markdown_for_random_sequences():
    rng = random.Random(72)
    for _ in range(2000):
        sequence = [rng.choice(TRICKY) for _ in range(rng.randint(1, 40))]
        taken = rng.sample(TRICKY, rng.randint(0, 4))
        assert _linear(sequence, taken) == _stock(sequence, taken), (sequence, taken)


def test_an_oversized_counter_gets_a_suffix_instead_of_failing():
    slug = "x_" + "1" * 5000
    with pytest.raises(ValueError):
        _stock([slug, slug])
    assert _linear([slug, slug])[0] == [slug, slug + "_1"]


def test_nesting_matches_python_markdown():
    rng = random.Random(72)
    for _ in range(1000):
        tokens = [{"level": rng.randint(1, 6), "n": n} for n in range(rng.randint(0, 30))]
        stock, linear = copy.deepcopy(tokens), copy.deepcopy(tokens)
        assert heading_ids.nest_toc_tokens(linear) == STOCK_NEST_TOC_TOKENS(stock)
        assert linear == stock == []


@pytest.mark.parametrize("count", [1000, 4000])
def test_repeated_names_cost_a_bounded_number_of_lookups(count):
    allocator = HeadingIds()
    for _ in range(count):
        allocator("section")
    # Python-Markdown's own function makes count**2 / 2 lookups here.
    assert allocator.probes <= 2 * count
    assert allocator("section") == "section_%d" % count


def _heading_line(name: str) -> str:
    return f"## {name}" if name else "##"


CORPUS = [
    "Section",
    "Section",
    "Section 1",
    "Section_1",
    "section-1",
    "Section",
    "",
    "",
    "Café",
    "Cafe",
    "日本語",
    "日本語",
    "🎉",
    "A_1",
    "A",
    "A",
    "A_1",
    "Overview {#section_3}",
    "Section",
    "Section",
    "Notes &amp; queries",
    "Notes & queries",
    "`code` heading",
    "code heading",
]


def _markdown_source(tmp_path, names):
    path = tmp_path / "headings.md"
    path.write_text(
        "\n\n".join(_heading_line(name) + "\n\nText." for name in names) + "\n",
        encoding="utf-8",
    )
    return path


def test_markdown_import_keeps_the_ids_python_markdown_gave(tmp_path, monkeypatch):
    path = _markdown_source(tmp_path, CORPUS)
    fast = ingest(str(path), Settings())
    with monkeypatch.context() as stock:
        stock.setattr(heading_ids, "install", lambda: None)
        stock.setattr(toc, "unique", STOCK_UNIQUE)
        stock.setattr(toc, "nest_toc_tokens", STOCK_NEST_TOC_TOKENS)
        expected = ingest(str(path), Settings())
    assert fast.body_html == expected.body_html
    assert fast.outline == expected.outline
    ids = re.findall(r'<h2 id="([^"]*)"', fast.body_html)
    # An explicit `{#section_3}` is reserved before any id is assigned, and
    # headings that slug to nothing share the `_n` series.
    assert ids == [
        "section",
        "section_1",
        "section-1",
        "section_2",
        "section-1_1",
        "section_4",
        "_1",
        "_2",
        "cafe",
        "cafe_1",
        "_3",
        "_4",
        "_5",
        "a_1",
        "a",
        "a_2",
        "a_3",
        "section_3",
        "section_5",
        "section_6",
        "notes-queries",
        "notes-queries_1",
        "code-heading",
        "code-heading_1",
    ]


def test_docx_headings_keep_the_ids_python_markdown_gave():
    names = [name for name in CORPUS if "{" not in name and "`" not in name]
    fragment = "".join(f"<h2>{name}</h2><p>Text.</p>" for name in names)
    _body, outline, _warnings = _sanitize(fragment)
    ids: set[str] = set()
    expected = [STOCK_UNIQUE(toc.slugify(name.replace("&amp;", "&"), "-"), ids) for name in names]
    assert [anchor for _level, _name, anchor in outline] == expected


def test_markdown_import_assigns_repeated_ids_in_linear_time(tmp_path, monkeypatch):
    allocators: list[HeadingIds] = []

    class Recording(HeadingIds):
        def __init__(self, ids=None):
            super().__init__(ids)
            allocators.append(self)

    monkeypatch.setattr(heading_ids, "HeadingIds", Recording)
    count = 3000
    result = ingest(str(_markdown_source(tmp_path, ["Section"] * count)), Settings())
    assert toc.unique is heading_ids.unique
    assert toc.nest_toc_tokens is heading_ids.nest_toc_tokens
    assert [anchor for _level, _name, anchor in result.outline][-1] == "section_%d" % (count - 1)
    assert len(allocators) == 1
    assert allocators[0].probes <= 2 * count


class Stop(Exception):
    pass


def test_cancel_is_checked_while_heading_ids_are_assigned(tmp_path):
    calls = 0

    def count():
        nonlocal calls
        calls += 1

    ingest(str(_markdown_source(tmp_path, ["Section"])), Settings(), count)
    baseline, calls = calls, 0
    ingest(str(_markdown_source(tmp_path, ["Section"] * 1000)), Settings(), count)
    assert calls >= baseline + 1000 // heading_ids.CANCEL_STRIDE

    def stop():
        nonlocal calls
        calls += 1
        if calls > baseline:
            raise Stop

    calls = 0
    with pytest.raises(Stop):
        ingest(str(_markdown_source(tmp_path, ["Section"] * 1000)), Settings(), stop)


def test_outside_an_import_python_markdown_behaves_as_shipped():
    heading_ids.install()
    ids = {"a"}
    assert toc.unique("a", ids) == "a_1"
    assert heading_ids._ACTIVE.get() is None
