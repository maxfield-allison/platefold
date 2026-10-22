"""Characters the requested fonts lack are reported with the face that printed them."""

import pytest

from platefold.config import make_settings
from platefold.font_fallback import (
    MAX_CHARACTERS,
    SAMPLE_SIZE,
    check_font_fallback,
    fallback_warnings,
    report_font_fallback,
)
from platefold.fonts import font_css


def entry(fallback, characters, scripts, sample, font="Sora"):
    return {
        "font": font,
        "fallback": fallback,
        "characters": characters,
        "occurrences": characters,
        "scripts": scripts,
        "sample": sample,
    }


def test_each_fallback_face_is_one_summarised_warning():
    han = [chr(0x4E00 + n) for n in range(SAMPLE_SIZE)]
    warnings = fallback_warnings(
        {
            "status": "complete",
            "characters_checked": 3100,
            "fallbacks": [
                entry("Noto Sans CJK SC", 3000, ["Han", "Hiragana"], han),
                entry("Segoe UI Emoji", 1, ["Emoji"], ["😀"]),
                entry(None, 2, ["Other scripts"], ["\U00050001", "\U00050002"]),
            ],
        }
    )
    assert len(warnings) == 3
    assert warnings[0].startswith("FONT_FALLBACK: Sora has no glyph for 3,000 characters")
    assert "(Han, Hiragana); they print in Noto Sans CJK SC" in warnings[0]
    assert " ".join(han[:8]) + " and 2,992 more." in warnings[0]
    assert "1 character (Emoji); it prints in Segoe UI Emoji: 😀." in warnings[1]
    assert warnings[2].startswith("FONT_GLYPHS_MISSING: 2 characters (Other scripts) have no")
    assert "print as placeholder boxes" in warnings[2]


def test_partial_check_says_what_it_did_not_reach():
    warnings = fallback_warnings({"status": "partial", "characters_checked": 1, "fallbacks": []})
    assert warnings == [warnings[0]]
    assert warnings[0].startswith("FONT_FALLBACK_INCOMPLETE:")
    assert f"{MAX_CHARACTERS:,} distinct characters" in warnings[0]


def test_renderer_failure_is_a_visible_warning_not_silence():
    class Broken:
        def evaluate(self, *_args):
            return None

        def _send(self, *_args):
            return {}

    info = {}
    report_font_fallback(Broken(), info, [])
    assert info["font_fallback"]["status"] == "unverified"
    assert info["warnings"][0].startswith("FONT_FALLBACK_UNVERIFIED:")
    report_font_fallback(Broken(), info, [])  # measuring and final passes
    assert len(info["warnings"]) == 1


@pytest.fixture(scope="module")
def page():
    from platefold.browser import find_bundled
    from platefold.cdp import Browser

    path = find_bundled()
    if not path:
        pytest.skip("bundled Chromium unavailable")
    browser = Browser(path)
    try:
        yield browser.new_page()
    finally:
        browser.close()


def check(page, body, running=(), body_style=""):
    settings = make_settings()  # the bundled sora set
    css = font_css(settings.body_font + settings.mono_font)
    body_stack = settings.body_font.replace("'", '"')
    mono_stack = settings.mono_font.replace("'", '"')
    page.set_content(
        f"<style>{css} body{{font-family:{body_stack}}} code{{font-family:{mono_stack}}}</style>"
        f'<body style="{body_style}">{body}</body>'
    )
    result = check_font_fallback(page, list(running))
    assert page.evaluate("document.querySelector('[id^=pf-fallback-]') === null")
    assert result["status"] == "complete"
    return result


def reported(result):
    return {text for item in result["fallbacks"] for text in item["sample"]}


def fallback_for(result, text):
    return next(item for item in result["fallbacks"] if text in item["sample"])


@pytest.mark.integration
def test_pure_latin_document_has_no_fallback(page):
    result = check(
        page,
        "<h1>Quarterly report</h1><p>Plain text, “quotes”, café, naïve — 1\u20132 € 3.</p>"
        "<ul><li><strong>Bold</strong> and <em>italic</em></li></ul><code>x = [1, 2]</code>",
        [(make_settings().head_font, "Quarterly report v1.0 report.md Page 0123456789 of")],
    )
    assert result["fallbacks"] == []
    assert result["characters_checked"] > 20
    assert fallback_warnings(result) == []


@pytest.mark.integration
def test_cjk_is_reported_under_the_requested_font(page):
    result = check(page, "<p>Latin 漢字 and かな, カナ and 한글.</p>")
    item = fallback_for(result, "漢")
    assert item["font"] == "Sora"
    assert {"字", "か", "な"} <= set(item["sample"]) | reported(result)
    assert "Han" in {script for entry in result["fallbacks"] for script in entry["scripts"]}
    assert "L" not in reported(result)


@pytest.mark.integration
def test_arabic_is_reported(page):
    result = check(page, '<p>English and <span lang="ar" dir="rtl">مرحبا بالعالم</span></p>')
    item = fallback_for(result, "م")
    assert item["font"] == "Sora"
    assert item["scripts"] == ["Arabic"]
    assert item["occurrences"] >= item["characters"]


@pytest.mark.integration
def test_emoji_sequences_are_reported_whole(page):
    result = check(page, "<p>Thumbs 👍🏽 flag 🇺🇸 family 👨‍👩‍👧 smile 😀</p>")
    samples = reported(result)
    assert {"👍🏽", "🇺🇸", "👨‍👩‍👧", "😀"} <= samples
    assert "👍" not in samples  # the modifier sequence is one character
    assert "Emoji" in fallback_for(result, "😀")["scripts"]


@pytest.mark.integration
def test_combining_marks_follow_the_shaped_cluster(page):
    # e + combining acute composes to é, which Sora has; x + dot below has
    # no composed form and needs a face with the mark.
    result = check(page, "<p>Café and x̣ and ą</p>")
    samples = reported(result)
    assert "é" not in samples
    assert {"x̣", "ą"} <= samples


@pytest.mark.integration
def test_code_is_checked_against_its_own_font(page):
    result = check(page, "<p>plain</p><code>字</code>")
    assert fallback_for(result, "字")["font"] == "JetBrains Mono"


@pytest.mark.integration
def test_unassigned_characters_are_missing_glyphs_not_a_face(page):
    result = check(page, "<p>box \U00050001 here</p>")
    item = fallback_for(result, "\U00050001")
    assert item["fallback"] is None
    assert fallback_warnings(result)[0].startswith("FONT_GLYPHS_MISSING:")


@pytest.mark.integration
def test_missing_glyph_check_follows_an_offset_body(page):
    result = check(
        page,
        "<p>box \U00050001 and Latin text</p>",
        body_style="position:relative;margin:37px 0 0 53px;padding:11px",
    )
    assert [item["fallback"] for item in result["fallbacks"]] == [None]
    assert result["fallbacks"][0]["sample"] == ["\U00050001"]


@pytest.mark.integration
def test_hidden_text_and_running_text(page):
    head = make_settings().head_font
    result = check(
        page,
        '<p>shown</p><p style="display:none">漢</p><svg><title>字</title>'
        '<text x="1" y="9">かな</text></svg>',
        [(head, "標題")],
    )
    samples = reported(result)
    assert "漢" not in samples and "字" not in samples
    assert {"か", "標"} <= samples


def installed_chromium():
    """A full Chromium build other than the bundled engine, as an installed browser."""
    from platefold.browser import find_bundled, playwright_cache

    bundled = find_bundled()
    names = ("chrome", "chrome.exe", "Chromium")
    for path in sorted(playwright_cache().glob("chromium-*/**/*"), reverse=True):
        if path.name in names and path.is_file() and str(path) != bundled:
            return path
    return None


@pytest.mark.integration
def test_an_installed_browser_reports_the_faces_it_used(tmp_path):
    """The sandbox fallback uses an installed browser with its own build and fonts."""
    from platefold.cdp import Browser, CDPError
    from platefold.service import ExportRequest, export

    path = installed_chromium()
    if path is None:
        pytest.skip("no Chromium build besides the bundled engine")
    try:
        Browser(str(path)).close()
    except CDPError as exc:
        # Some CI images carry a full build that aborts on start in a container;
        # starting it is not what this test is about.
        pytest.skip(f"the installed Chromium cannot start here: {str(exc).splitlines()[0]}")
    source = tmp_path / "mixed.md"
    source.write_text("# Mixed\n\nLatin 漢字 مرحبا 😀 x̣ and \U00050001.\n", encoding="utf-8")
    result = export(
        ExportRequest(
            str(source),
            str(tmp_path / "out"),
            make_settings(document_language="en", toc=False),
            browser=str(path),
        )
    )
    assert "bundled" not in result.report["engine"]
    report = result.report["documents"][0]["font_fallback"]
    assert report["status"] == "complete", report
    samples = {text: item for item in report["fallbacks"] for text in item["sample"]}
    assert {"漢", "م", "😀", "x̣", "\U00050001"} <= set(samples), report
    assert samples["\U00050001"]["fallback"] is None
    assert all(samples[text]["fallback"] for text in ("漢", "م", "😀"))
    assert not {"L", "a", "t", "i", "n"} & set(samples)
