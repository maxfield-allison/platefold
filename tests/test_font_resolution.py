"""Warnings require renderer evidence, not mere source font requests."""

from dataclasses import replace

import pytest

from platefold.config import make_settings
from platefold.font_resolution import report_font_resolution
from platefold.theme import get_font_set


class ProbePage:
    def __init__(self, available, used, generic=False):
        self.available, self.used, self.generic = available, used, generic
        self.calls = []

    def evaluate(self, expression, arg=None):
        self.calls.append(expression)
        if "async" in expression:
            return [
                {
                    "role": "body",
                    "requested": "Calibri",
                    "available": self.available,
                    "generic": self.generic,
                }
            ]

    def _send(self, method, params=None):
        if method == "DOM.getDocument":
            return {"root": {"nodeId": 1}}
        if method == "DOM.querySelector":
            return {"nodeId": 2}
        if method == "CSS.getPlatformFontsForNode":
            return {"fonts": [{"familyName": family, "glyphCount": 8} for family in self.used]}
        return {}


def test_available_font_with_different_internal_name_does_not_warn():
    info = {}
    report_font_resolution(ProbePage(True, ["Internal Family"]), make_settings(), info)
    assert not info.get("warnings")
    assert info["font_resolution"][0]["status"] == "available"
    assert info["font_resolution"][0]["used"] == ["Internal Family"]


def test_missing_font_warns_with_observed_substitute_and_recovery():
    page, info = ProbePage(False, ["Sora"]), {}
    report_font_resolution(page, make_settings(), info)
    assert "Calibri is unavailable" in info["warnings"][0]
    assert "using Sora" in info["warnings"][0]
    assert "choose a font set whose fonts are installed" in info["warnings"][0]
    assert "Export settings" not in info["warnings"][0]
    report_font_resolution(page, make_settings(), info)
    assert len(info["warnings"]) == 1
    assert sum("async" in call for call in page.calls) == 1


def test_unknown_renderer_evidence_never_claims_a_substitution():
    info = {}
    report_font_resolution(ProbePage(False, []), make_settings(), info)
    assert info["font_resolution"][0]["status"] == "unverified"
    assert "FONT_SUBSTITUTED" not in info["warnings"][0]


@pytest.mark.integration
def test_real_renderer_proves_installed_missing_and_unused_fonts(tmp_path):
    from platefold.browser import find_bundled
    from platefold.cdp import Browser
    from platefold.fonts import font_css
    from platefold.installed_fonts import discover_installed_fonts

    path = find_bundled()
    if not path:
        pytest.skip("bundled Chromium unavailable")
    browser = Browser(path)
    try:
        page = browser.new_page()
        families = discover_installed_fonts().families
        if not families:
            pytest.skip("No installed font catalog")
        family = next(
            (
                name
                for name in ("Arial", "Helvetica", "FreeSans", "Liberation Sans", "DejaVu Sans")
                if name in families
            ),
            None,
        )
        if family is None:
            pytest.skip("No known TrueType/OpenType test font")
        settings = make_settings(font_set="system:" + family)

        def prepare(active):
            page.set_content(
                "<style>" + font_css(active.body_font) + "</style><p>Example body text 123.</p>"
            )
            info = {}
            report_font_resolution(page, active, info)
            assert [item["role"] for item in info["font_resolution"]] == ["body"]
            assert page.evaluate("document.querySelector('[id^=pf-font-]') === null")
            return info

        available = prepare(settings)
        assert available["font_resolution"][0]["status"] == "available"
        assert available["font_resolution"][0]["used"]
        missing = prepare(
            replace(settings, fonts=get_font_set("system:PlateFold Missing Test Face 123"))
        )
        assert missing["font_resolution"][0]["status"] == "substituted"
        assert "Sora" in missing["font_resolution"][0]["used"]
        assert len(missing["warnings"]) == 1
    finally:
        browser.close()
