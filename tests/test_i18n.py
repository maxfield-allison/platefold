import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from platefold.i18n import Translator, resolve_locale, translator
from platefold.ingest.converter import ExternalConverterError
from platefold.worker import DesktopWorker, recovery_id

ROOT = Path(__file__).resolve().parents[1]


def test_shipped_catalogs_and_source_references_are_complete():
    result = subprocess.run(
        [sys.executable, "tools/check_i18n.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "1 shipped catalog(s)" in result.stdout


@pytest.mark.parametrize(
    ("requested", "expected"),
    [("en", "en"), ("en-US", "en"), ("EN_gb", "en"), ("fr-CA", "en"), ("../en", "en")],
)
def test_locale_resolution_uses_exact_language_then_english_fallback(requested, expected):
    assert resolve_locale(requested, {"en"}) == expected


def test_interpolation_plural_and_locale_aware_numbers():
    messages = translator("en-US")
    assert messages.text("result.rendered_with", engine="Chromium") == "Rendered with Chromium"
    assert messages.plural("result.pages", 1) == "1 page"
    assert messages.plural("result.pages", 1200) == "1,200 pages"


def test_plural_catalog_requires_placeholders():
    messages = Translator.from_catalog(
        {
            "locale": "en",
            "direction": "ltr",
            "messages": {
                "test": {"one": "{count} item", "other": "{count} items"},
                "plain": "Value: {value}",
            },
        }
    )
    assert messages.plural("test", 2) == "2 items"
    with pytest.raises(ValueError, match="Missing interface placeholders"):
        messages.text("plain")


def test_catalog_is_utf8_and_contains_unicode_source_text():
    raw = (ROOT / "platefold" / "locales" / "en.json").read_bytes()
    catalog = json.loads(raw.decode("utf-8"))
    assert "→" in catalog["messages"]["result.summary"]["other"]
    assert "…" in catalog["messages"]["status.connecting"]


def test_worker_errors_carry_a_stable_message_id_without_changing_event_keys():
    payload = DesktopWorker.error_payload(
        ValueError("An export is already running"), {"ui_locale": "en"}
    )
    assert payload == {
        "message_id": "worker.export_running",
        "message": "An export is already running",
        "help_id": "help.invalid_input",
        "help": "Correct the source or setting named above, then retry.",
    }
    assert DesktopWorker.error_payload(ValueError("Library detail"), {}) == {
        "message": "Library detail",
        "help_id": "help.invalid_input",
        "help": "Correct the source or setting named above, then retry.",
    }


def test_converter_errors_localize_by_stable_code_and_preserve_safe_fields():
    error = ExternalConverterError(
        "converter_unavailable",
        "source-specific fallback",
        input_format="doc",
        converter_options="LibreOffice",
    )

    assert DesktopWorker.error_payload(error, {"ui_locale": "en"}) == {
        "message_id": "error.converter_unavailable",
        "message": (
            "No supported external converter route is available. Install LibreOffice, or "
            "re-save the file as DOCX. Pandoc can handle ODT/RTF only when its sandboxed "
            "DOCX capability check passes."
        ),
        "help_id": "help.converter",
        "help": (
            "Install LibreOffice, or open the source in its original application and save it "
            "as DOCX before retrying."
        ),
        "code": "converter_unavailable",
        "fields": {
            "input_format": "doc",
            "converter_options": "LibreOffice",
        },
    }


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (FileNotFoundError("missing"), "help.source_missing"),
        (PermissionError("denied"), "help.permission"),
        (ValueError("bad option"), "help.invalid_input"),
        (RuntimeError("unexpected"), "help.retry"),
    ],
)
def test_worker_recovery_guidance_matches_failure_class(error, expected):
    assert recovery_id(error) == expected


def test_desktop_styles_use_logical_directions_for_rtl_readiness():
    css = (ROOT / "desktop" / "src" / "style.css").read_text(encoding="utf-8")
    physical = re.compile(
        r"(?:padding|margin|border)-(?:left|right)|text-align:\s*(?:left|right)|"
        r"(?:^|[;{])\s*(?:left|right):",
        re.MULTILINE,
    )
    assert not physical.search(css)
