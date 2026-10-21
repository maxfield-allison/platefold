from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from platefold.cancellation import CancelledError
from platefold.config import Settings
from platefold.document import Document
from platefold.ingest import SUPPORTED_SUFFIXES, Ingested, converter
from platefold.limits import ResourceLimitError
from platefold.server_models import JobRequest
from platefold.service import collect


class NoDiagrams:
    def render(self, _source):
        raise AssertionError("legacy fixture contains no Mermaid diagrams")


def executable(path: Path, body: str) -> Path:
    """A fake command named ``path.name`` that runs ``body`` with this Python.

    Windows finds commands by extension and ignores a #! line, so there the
    script sits beside a .cmd launcher of the same name.
    """
    if os.name == "nt":
        script = path.with_name(path.name + "-fake.py")
        script.write_text(body + "\n", encoding="utf-8")
        launcher = path.with_name(path.name + ".cmd")
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        return launcher
    path.write_text(f"#!{sys.executable}\n{body}\n", encoding="utf-8")
    path.chmod(0o700)
    return path


def fake_soffice(path: Path, conversion: str) -> Path:
    return executable(
        path,
        """import pathlib, sys
args = sys.argv[1:]
if "--version" in args:
    print("LibreOffice 24.2.7.2")
    raise SystemExit(0)
out = pathlib.Path(args[args.index("--outdir") + 1])
source = pathlib.Path(args[-1])
%s
"""
        % conversion,
    )


def fake_pandoc(path: Path) -> Path:
    return executable(
        path,
        """import pathlib, sys
args = sys.argv[1:]
if "--version" in args:
    print("pandoc 3.8.2")
elif "--list-input-formats" in args:
    print("docx\\nodt\\nrtf")
elif "--list-output-formats" in args:
    print("docx\\nodt\\nrtf")
elif "--help" in args:
    print("usage: pandoc [--sandbox]")
else:
    assert "--sandbox" in args
    assert any(arg == "--from=rtf" for arg in args)
    output = pathlib.Path(next(arg.split("=", 1)[1] for arg in args if arg.startswith("--output=")))
    output.write_bytes(b"PK\\x03\\x04pandoc-docx")
""",
    )


def test_legacy_suffixes_are_registered_and_collected(tmp_path):
    for name in ("old.DOC", "open.odt", "rich.rtf", "ignored.txt"):
        (tmp_path / name).write_bytes(b"synthetic")

    files, _root = collect(str(tmp_path))

    assert {".doc", ".odt", ".rtf"} <= SUPPORTED_SUFFIXES
    assert [Path(path).name for path in files] == ["old.DOC", "open.odt", "rich.rtf"]


def test_conversion_provenance_reaches_document_report(tmp_path):
    conversion = {
        "name": "libreoffice",
        "version": "LibreOffice 24.2.7.2",
        "input_format": "doc",
        "output_format": "docx",
        "sandbox": "safe-mode/private-profile",
    }
    source = Ingested(body_html="<p>Proof</p>", conversion=conversion, language="fr-CA")
    _html, info = Document(
        str(tmp_path / "legacy.doc"),
        str(tmp_path),
        Settings(),
        NoDiagrams(),
        source=source,
    ).render()

    assert info["conversion"] == conversion
    assert info["language"] == "fr-CA"


@pytest.mark.skipif(sys.platform != "linux", reason="the HTTP service runs only on Linux")
def test_http_contract_accepts_legacy_uploads_and_reports_conversion():
    # Imported here: the service applies POSIX resource limits, so its render
    # module does not import on other platforms.
    from platefold.server_render import public_report

    for format_name in ("doc", "odt", "rtf"):
        assert (
            JobRequest.model_validate(
                {"source": {"kind": "upload", "format": format_name}}
            ).source.format
            == format_name
        )
    conversion = {
        "name": "pandoc",
        "version": "pandoc 3.8.2",
        "input_format": "rtf",
        "output_format": "docx",
        "sandbox": "pandoc --sandbox",
    }
    document = {
        "file": "proof.rtf",
        "pages": 1,
        "figures": 0,
        "missing_images": [],
        "diagram_errors": [],
        "warnings": ["EXTERNAL_CONVERSION: proof"],
        "conversion": conversion,
        "theme": "bristol",
        "font_set": "sora",
        "tiled_figures": [{"figure": 1, "columns": 2, "rows": 1, "sheets": 2, "scale": 1.0}],
    }
    result = SimpleNamespace(
        status="completed_with_warnings",
        elapsed_seconds=0.2,
        report={
            "engine": "test",
            "settings": dict.fromkeys(
                (
                    "theme",
                    "theme_mode",
                    "font_set",
                    "page_width_mm",
                    "page_height_mm",
                    "output_mode",
                    "margin_mode",
                    "page_margins_mm",
                    "image_dpi",
                    "csv_header",
                    "xlsx_sheets",
                    "document_language",
                    "exact_margins",
                    "inline_fit",
                    "plate_fit",
                    "max_custom_width_mm",
                    "caption_px",
                    "tile_overlap_mm",
                    "body_size_pt",
                    "mermaid_font_px",
                    "mermaid_wrap_px",
                ),
                1,
            ),
            "documents": [document],
        },
    )

    public = public_report(result)
    assert public["documents"][0]["conversion"] == conversion
    assert public["documents"][0]["tiled_figures"] == document["tiled_figures"]
    assert public["settings"]["tile_overlap_mm"] == 1


def test_no_converter_is_actionable_and_does_not_claim_pandoc_reads_doc(tmp_path, monkeypatch):
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"synthetic")
    monkeypatch.setenv("PATH", "")

    with pytest.raises(converter.ExternalConverterError) as caught:
        converter.ingest(str(source), Settings())

    assert caught.value.code == "converter_unavailable"
    assert "Install LibreOffice" in str(caught.value)
    assert "Pandoc is eligible for ODT/RTF" in str(caught.value)
    assert "re-save the file as DOCX" in str(caught.value)


def test_disabled_conversion_refuses_before_looking_for_a_converter(tmp_path, monkeypatch):
    source = tmp_path / "legacy.rtf"
    source.write_bytes(b"{\\rtf1 synthetic}")
    monkeypatch.setattr(
        converter, "_detect_routes", lambda *a, **k: pytest.fail("converter was probed")
    )

    with pytest.raises(converter.ExternalConverterError) as caught:
        converter.ingest(str(source), Settings(external_converters=False))

    assert caught.value.code == "converter_disabled"


def test_no_converter_has_nonzero_cli_result_and_stable_error_code(tmp_path, monkeypatch, capsys):
    from platefold import cli

    source = tmp_path / "legacy.doc"
    source.write_bytes(b"synthetic")
    monkeypatch.setenv("PATH", "")

    def fail(request, **_kwargs):
        return converter.ingest(request.input_path, request.settings)

    monkeypatch.setattr(cli, "export", fail)
    assert cli.main([str(source), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "converter_unavailable"
    assert result["error"]["fields"] == {
        "input_format": "doc",
        "converter_options": "LibreOffice; sandbox-capable Pandoc (ODT/RTF only)",
    }
    assert "Install LibreOffice" in result["error"]["message"]


def test_libreoffice_converts_to_docx_then_preserves_ingested_fields(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_soffice(
        tools / "soffice",
        '(out / f"{source.stem}.docx").write_bytes(b"converted-docx")',
    )
    monkeypatch.setenv("PATH", str(tools))
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"synthetic-doc")
    converted_paths = []

    def ingest_docx(path, settings, cancel_check=None):
        converted = Path(path)
        assert converted.read_bytes() == b"converted-docx"
        converted_paths.append(converted)
        return Ingested(body_html="<h1 id='proof'>Proof</h1>", title="Proof", language="fr-CA")

    monkeypatch.setattr(converter, "ingest_docx", ingest_docx)
    result = converter.ingest(str(source), Settings())

    assert result.title == "Proof"
    assert result.language == "fr-CA"
    assert result.conversion == {
        "name": "libreoffice",
        "version": "LibreOffice 24.2.7.2",
        "input_format": "doc",
        "output_format": "docx",
        "sandbox": "safe-mode/private-profile",
    }
    assert any(code == "EXTERNAL_CONVERSION" for code, _ in result.warnings)
    assert converted_paths and not converted_paths[0].exists()


@pytest.mark.parametrize("error", [CancelledError, ResourceLimitError])
def test_converted_docx_import_honours_the_callers_cancel_check(tmp_path, monkeypatch, error):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_soffice(
        tools / "soffice",
        '(out / f"{source.stem}.docx").write_bytes(b"converted-docx")',
    )
    monkeypatch.setenv("PATH", str(tools))
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"synthetic-doc")

    def cancel_check():
        pass

    def ingest_docx(path, settings, cancel_check=None):
        assert cancel_check is caller_check
        raise error("stopped while reading the converted DOCX")

    caller_check = cancel_check
    monkeypatch.setattr(converter, "ingest_docx", ingest_docx)
    # Neither is reported as a converter failure, and no other route is tried.
    with pytest.raises(error, match="stopped while reading"):
        converter.ingest(str(source), Settings(), cancel_check)


def test_pandoc_route_requires_advertised_formats_and_uses_sandbox(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_pandoc(tools / "pandoc")
    monkeypatch.setenv("PATH", str(tools))
    source = tmp_path / "legacy.rtf"
    source.write_text(r"{\rtf1 proof}", encoding="ascii")
    monkeypatch.setattr(
        converter,
        "ingest_docx",
        lambda path, settings, cancel_check=None: Ingested(body_html="<p>Proof</p>"),
    )

    result = converter.ingest(str(source), Settings())

    assert result.conversion["name"] == "pandoc"
    assert result.conversion["input_format"] == "rtf"
    assert result.conversion["sandbox"] == "pandoc --sandbox"


def test_pandoc_success_reports_failed_libreoffice_fallback(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_soffice(tools / "soffice", 'print("LibreOffice conversion failed"); raise SystemExit(7)')
    fake_pandoc(tools / "pandoc")
    monkeypatch.setenv("PATH", str(tools))
    source = tmp_path / "legacy.rtf"
    source.write_text(r"{\rtf1 proof}", encoding="ascii")
    monkeypatch.setattr(
        converter,
        "ingest_docx",
        lambda path, settings, cancel_check=None: Ingested(body_html="<p>Proof</p>"),
    )

    result = converter.ingest(str(source), Settings())

    assert result.conversion["name"] == "pandoc"
    assert any(code == "EXTERNAL_CONVERTER_FALLBACK" for code, _ in result.warnings)


def test_pandoc_is_never_offered_for_binary_doc(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_pandoc(tools / "pandoc")
    monkeypatch.setenv("PATH", str(tools))
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"not really a doc")

    with pytest.raises(converter.ExternalConverterError) as caught:
        converter.ingest(str(source), Settings())

    assert caught.value.code == "converter_unavailable"


def test_pandoc_route_must_prove_sandboxed_docx_writing(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    executable(
        tools / "pandoc",
        """import sys
args = sys.argv[1:]
if "--version" in args:
    print("pandoc 2.17.1.1")
elif "--list-input-formats" in args:
    print("odt\\nrtf")
elif "--list-output-formats" in args:
    print("docx")
elif "--help" in args:
    print("usage: pandoc [--sandbox]")
else:
    print("Could not find data file data/data/docx/[Content_Types].xml")
    raise SystemExit(6)
""",
    )
    monkeypatch.setenv("PATH", str(tools))
    source = tmp_path / "legacy.rtf"
    source.write_text(r"{\rtf1 proof}", encoding="ascii")

    with pytest.raises(converter.ExternalConverterError) as caught:
        converter.ingest(str(source), Settings())

    assert caught.value.code == "converter_unavailable"
    assert "sandboxed DOCX self-test failed" in str(caught.value)


def test_converter_nonzero_and_missing_output_are_clear(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_soffice(
        tools / "soffice", 'print("conversion failed", file=sys.stderr); raise SystemExit(7)'
    )
    monkeypatch.setenv("PATH", str(tools))
    source = tmp_path / "legacy.odt"
    source.write_bytes(b"synthetic")

    with pytest.raises(converter.ExternalConverterError) as caught:
        converter.ingest(str(source), Settings())

    assert caught.value.code == "converter_failed"
    assert "conversion failed" in str(caught.value)
    assert "libreoffice" in str(caught.value)


def test_malformed_docx_output_is_rejected_and_cleaned(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_soffice(
        tools / "soffice",
        '(out / f"{source.stem}.docx").write_bytes(b"malformed")',
    )
    monkeypatch.setenv("PATH", str(tools))
    monkeypatch.setattr(converter.tempfile, "tempdir", str(tmp_path))
    source = tmp_path / "legacy.rtf"
    source.write_bytes(b"synthetic")
    monkeypatch.setattr(
        converter,
        "ingest_docx",
        lambda path, settings, cancel_check=None: (_ for _ in ()).throw(ValueError("invalid DOCX")),
    )

    with pytest.raises(converter.ExternalConverterError) as caught:
        converter.ingest(str(source), Settings())

    assert caught.value.code == "converter_output_invalid"
    assert "could not validate" in str(caught.value)
    assert not list(tmp_path.glob("platefold-converter-*"))


def test_linked_converter_output_is_rejected_without_touching_target(
    tmp_path, monkeypatch, symlinks
):
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"outside")
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_soffice(
        tools / "soffice",
        f'(out / f"{{source.stem}}.docx").symlink_to({str(outside)!r})',
    )
    monkeypatch.setenv("PATH", str(tools))
    source = tmp_path / "legacy.doc"
    source.write_bytes(b"synthetic")

    with pytest.raises(converter.ExternalConverterError) as caught:
        converter.ingest(str(source), Settings())

    assert caught.value.code == "converter_output_escaped"
    assert "linked or escaping DOCX" in str(caught.value)
    assert outside.read_bytes() == b"outside"


def test_subprocess_output_and_time_are_bounded(tmp_path):
    env = converter._private_environment(tmp_path)
    with pytest.raises(converter.ExternalConverterError) as output_error:
        converter._run_bounded(
            [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'x' * 100_000)"],
            cwd=tmp_path,
            env=env,
            timeout=5,
            cancel_check=lambda: None,
            output_limit=4096,
        )
    assert output_error.value.code == "converter_output_too_large"

    with pytest.raises(converter.ExternalConverterError) as timeout_error:
        converter._run_bounded(
            [sys.executable, "-c", "import time; time.sleep(5)"],
            cwd=tmp_path,
            env=env,
            timeout=0.1,
            cancel_check=lambda: None,
        )
    assert timeout_error.value.code == "converter_timeout"


def test_converted_docx_size_is_bounded_with_stable_code(tmp_path):
    output = tmp_path / "output"
    output.mkdir()
    expected = output / "source.docx"
    expected.write_bytes(b"too large")

    with pytest.raises(converter.ExternalConverterError) as caught:
        converter._validate_output(output, expected, 4)

    assert caught.value.code == "converter_output_too_large"
    assert caught.value.fields == {"limit": 4}


def test_subprocess_final_temporary_burst_is_bounded(tmp_path):
    watched = tmp_path / "watched"
    watched.mkdir()
    program = (
        "import pathlib; root=pathlib.Path('watched'); "
        "[(root / str(index)).write_bytes(b'x') for index in range(12)]"
    )

    with pytest.raises(ResourceLimitError):
        converter._run_bounded(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            env=converter._private_environment(tmp_path),
            timeout=5,
            cancel_check=lambda: None,
            watched_root=watched,
            temporary_entry_limit=4,
        )


def test_cancellation_kills_converter_descendants(tmp_path):
    """The whole converter tree dies: a process group on POSIX, a job on Windows."""
    psutil = pytest.importorskip("psutil")

    def alive(pid):
        try:
            return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False

    pid_file = tmp_path / "child.pid"
    program = (
        "import pathlib,subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid)); "
        "time.sleep(60)"
    )

    class StopNow(RuntimeError):
        pass

    def cancel_check():
        if pid_file.exists():
            raise StopNow("cancelled")

    with pytest.raises(StopNow):
        converter._run_bounded(
            [sys.executable, "-c", program],
            cwd=tmp_path,
            env=converter._private_environment(tmp_path),
            timeout=5,
            cancel_check=cancel_check,
        )

    child_pid = int(pid_file.read_text())
    for _ in range(50):
        if not alive(child_pid):
            break
        time.sleep(0.02)
    assert not alive(child_pid)
