"""Hostile Markdown is refused with a named paragraph before it becomes slow."""

from __future__ import annotations

from dataclasses import replace

import pytest
from markdown.extensions import md_in_html

from platefold.config import Settings, make_settings
from platefold.ingest import markdown_limits
from platefold.ingest.markdown import ingest
from platefold.limits import ResourceLimitError, ResourceLimits


def _ingest(tmp_path, text: str, **limits):
    path = tmp_path / "doc.md"
    path.write_text(text, encoding="utf-8")
    return ingest(str(path), Settings(limits=replace(ResourceLimits(), **limits)))


def test_unclosed_tags_are_refused_before_the_html_reader_runs(tmp_path, monkeypatch):
    def reader_must_not_run(self, lines):
        raise AssertionError("the HTML reader ran before the limit check")

    monkeypatch.setattr(md_in_html.HtmlBlockPreprocessor, "run", reader_must_not_run)
    text = "An intro paragraph.\n\nSee <a " + "<a " * 20_000 + "\n"
    with pytest.raises(ResourceLimitError) as refused:
        _ingest(tmp_path, text)
    message = str(refused.value)
    assert "limits.max_html_tag_search_bytes" in message
    assert "paragraph beginning “See <a <a" in message


def test_closed_tags_and_literal_angles_are_not_counted(tmp_path):
    text = "<b>x</b> " * 5000 + "\n\n" + "a < b and c > d. " * 2000 + "\n"
    _ingest(tmp_path, text, max_html_tag_search_bytes=64 * 1024)


def test_the_tag_search_estimate_follows_the_next_closing_angle():
    searches = markdown_limits.html_tag_searches("<a <b x> <c")
    assert searches == [(0, 7), (3, 4), (9, 2)]


def test_a_paragraph_with_too_many_inline_elements_is_refused_early(tmp_path):
    elements = 0
    original = markdown_limits._InlineBudget.add

    def counting(self, text):
        nonlocal elements
        elements += 1
        original(self, text)

    text = "First paragraph.\n\nBusy: " + "<b>x</b> " * 2000 + "\n"
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(markdown_limits._InlineBudget, "add", counting)
        with pytest.raises(ResourceLimitError) as refused:
            _ingest(tmp_path, text, max_paragraph_rebuild_bytes=500_000)
    message = str(refused.value)
    assert "limits.max_paragraph_rebuild_bytes" in message
    assert "paragraph beginning “Busy:" in message
    # Refused after about 500,000 bytes of copying, long before all 4,000 tags.
    assert elements < 100


def test_the_budget_is_per_paragraph(tmp_path):
    paragraph = "<b>x</b> " * 200
    text = "\n\n".join([paragraph] * 30) + "\n"
    # Each paragraph copies under 2 MB; all thirty together copy about 50 MB.
    _ingest(tmp_path, text, max_paragraph_rebuild_bytes=5_000_000)


def test_limits_are_settings(tmp_path):
    settings = make_settings(
        limits={"max_html_tag_search_bytes": 10, "max_paragraph_rebuild_bytes": 20}
    )
    assert settings.limits.max_html_tag_search_bytes == 10
    assert settings.limits.max_paragraph_rebuild_bytes == 20
    with pytest.raises(ValueError, match=r"limits\.max_paragraph_rebuild_bytes"):
        make_settings(limits={"max_paragraph_rebuild_bytes": 0})
