"""Real DOCX-to-PDF semantics behind a deterministic converter subprocess.

The stub controls the external converter output. These checks establish neither
LibreOffice/Pandoc fidelity nor native assistive-technology acceptance.
"""

import hashlib
from pathlib import Path

import pytest
from test_accessibility import _docx
from test_converter_ingest import fake_soffice

from platefold.config import make_settings
from platefold.service import ExportRequest, export
from tools.a11y.compare import raster_hashes
from tools.a11y.inspect_pdf import inspect


@pytest.mark.integration
@pytest.mark.parametrize(
    "suffix,mode,override", [("doc", "online", ""), ("odt", "print", "de-DE"), ("rtf", "tiled", "")]
)
def test_converted_document_keeps_language_structure_and_provenance(
    tmp_path, monkeypatch, suffix, mode, override
):
    from platefold import service

    converted = _docx(tmp_path / "converter-output.docx")
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_soffice(
        tools / "soffice",
        f'(out / f"{{source.stem}}.docx").write_bytes(pathlib.Path({str(converted)!r}).read_bytes())',
    )
    monkeypatch.setenv("PATH", str(tools))
    source = tmp_path / f"legacy.{suffix}"
    source.write_bytes(b"deterministic converter input")
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    original = service.print_pdf
    captures = []

    def capture(page, html, destination, *args, **options):
        original(page, html, destination, *args, **options)
        raw = tmp_path / f"raw-{len(captures)}.pdf"
        raw.write_bytes(Path(destination).read_bytes())
        captures.append(raw)

    monkeypatch.setattr(service, "print_pdf", capture)
    result = export(
        ExportRequest(
            str(source),
            str(tmp_path / "final"),
            settings=make_settings(
                output_mode=mode, document_language=override, locale_hint="ja-JP"
            ),
            browser="bundled",
        )
    )
    assert result.status == "completed_with_warnings"
    document = result.report["documents"][0]
    assert document["conversion"]["input_format"] == suffix
    assert document["conversion"]["name"] == "libreoffice"
    final = tmp_path / "final" / "legacy.pdf"
    before, after = inspect(captures[-1]), inspect(final)
    assert after["language"] == (override or "en-GB")
    assert after["structure_tree"] and after["parent_tree"]
    assert not after["parent_tree_errors"]
    assert after["nodes"] == before["nodes"]
    assert raster_hashes(final) == raster_hashes(captures[-1])
    assert "Embedded proof" in after["alternatives"]
    assert any(node.get("/Lang") == "fr-CA" for node in after["nodes"])
    assert any("/ColSpan" in str(node.get("/A")) for node in after["nodes"])
    assert after["roles"]["/TH"] >= 1
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
