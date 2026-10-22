"""Prove Windows font discovery and real renderer resolution on the build guest."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from platefold.config import make_settings
from platefold.service import ExportRequest, export
from platefold.worker import catalog


def check(directory: Path) -> dict:
    fonts = catalog()["font_sets"]
    assert any(font["name"] == "system:Arial" for font in fonts), "Windows Arial was not discovered"
    source = directory / "font-check.md"
    source.write_text(
        "# Font selection\n\nInstalled fonts keep this text readable.\n", encoding="utf-8"
    )
    cases = {}
    for label, family, expected in (
        ("installed", "Arial", "available"),
        ("missing", "PlateFold Missing Test Face 123", "substituted"),
    ):
        result = export(
            ExportRequest(
                str(source),
                str(directory / label),
                make_settings(font_set=f"system:{family}", document_language="en", toc=False),
                browser="bundled",
            )
        )
        document = result.report["documents"][0]
        body = next(row for row in document["font_resolution"] if row["role"] == "body")
        assert body["status"] == expected, body
        assert body["requested"] == family and body["used"], body
        if label == "installed":
            assert not any("FONT_SUBSTITUTED" in warning for warning in document["warnings"])
        else:
            assert "Sora" in body["used"], body
            assert any("FONT_SUBSTITUTED" in warning for warning in document["warnings"])
        # Latin text in a font that has it falls back nowhere.
        assert document["font_fallback"]["status"] == "complete", document["font_fallback"]
        assert document["font_fallback"]["fallbacks"] == [], document["font_fallback"]
        cases[label] = {"resolution": document["font_resolution"], "warnings": document["warnings"]}
    cases["fallback"] = check_fallback(directory)
    return {
        "status": "passed",
        "platform": sys.platform,
        "installed_families": sum(font["name"].startswith("system:") for font in fonts),
        "cases": cases,
        "scope": (
            "Windows discovery, real PDF font resolution and fallback reporting; "
            "not native font-picker acceptance"
        ),
    }


def check_fallback(directory: Path) -> dict:
    """Characters Sora lacks are reported with the Windows face that printed them."""
    source = directory / "fallback-check.md"
    source.write_text(
        "# Fallback\n\nLatin text, 漢字かな, مرحبا, 😀 and a box \U00050001.\n", encoding="utf-8"
    )
    result = export(
        ExportRequest(
            str(source),
            str(directory / "fallback"),
            make_settings(font_set="sora", document_language="en", toc=False),
            browser="bundled",
        )
    )
    document = result.report["documents"][0]
    report = document["font_fallback"]
    assert report["status"] == "complete", report

    def face(text: str) -> str | None:
        entry = next(item for item in report["fallbacks"] if text in item["sample"])
        assert entry["font"] == "Sora", entry
        return entry["fallback"]

    for text in ("漢", "か", "م", "😀"):
        assert face(text), (text, report)
    assert face("\U00050001") is None, report
    assert any(warning.startswith("FONT_FALLBACK:") for warning in document["warnings"])
    assert any(warning.startswith("FONT_GLYPHS_MISSING:") for warning in document["warnings"])
    assert result.status == "completed_with_warnings"
    return {"font_fallback": report, "warnings": document["warnings"]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This check requires Windows")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with TemporaryDirectory(prefix="pf-font-", dir=args.output.parent) as directory:
            result = check(Path(directory))
    except Exception as exc:
        result = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
