"""Application-service and machine-readable CLI boundaries."""

import errno
import json
import os
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError

from platefold.cli import main
from platefold.config import make_settings
from platefold.limits import ResourceLimitError, ResourceLimits
from platefold.service import ExportRequest, export


def _write_minimal_pdf(path: Path, *, title: str = "known good") -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.add_metadata({"/Title": title})
    with path.open("wb") as fh:
        writer.write(fh)
    return path.read_bytes()


@pytest.fixture
def stubbed_export_engine(monkeypatch):
    """Keep failure-safety tests below the browser boundary."""
    from platefold import service

    class Page:
        def emulate_media(self, **_kwargs):
            pass

    class Browser:
        def new_page(self, **_kwargs):
            return Page()

        def close(self):
            pass

    class Renderer:
        def __init__(self, *_args, **_kwargs):
            self.index = {}

        def save(self):
            pass

    class SyntheticDocument:
        def __init__(self, *_args, **_kwargs):
            pass

        def render(self):
            return "<main>synthetic</main>", {
                "title": "Synthetic",
                "version": "",
                "author": "",
                "subject": "",
                "has_toc": False,
                "order": [],
                "names": {},
                "levels": {},
                "markers": {},
                "figures": 0,
                "images": [],
                "warnings": [],
            }

    monkeypatch.setattr(service, "browser_launch", lambda _choice: (Browser(), "test engine"))
    monkeypatch.setattr(service, "MermaidRenderer", Renderer)
    monkeypatch.setattr(service, "Document", SyntheticDocument)
    return service


def test_service_input_error_does_not_exit_or_print(tmp_path, capsys):
    with pytest.raises(ValueError, match="Input not found"):
        export(ExportRequest(str(tmp_path / "absent.md"), str(tmp_path / "out")))
    assert capsys.readouterr().out == ""
    assert not (tmp_path / "out").exists()


def test_service_validates_settings_before_creating_output(tmp_path):
    settings = make_settings()
    settings.image_dpi = 0
    with pytest.raises(ValueError, match="image_dpi"):
        export(ExportRequest(str(tmp_path), str(tmp_path / "out"), settings))
    assert not (tmp_path / "out").exists()


def test_export_deadline_stops_before_rendering(tmp_path, stubbed_export_engine):
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")
    settings = make_settings(
        toc=False,
        bookmarks=False,
        limits=ResourceLimits(max_export_seconds=0.000_001),
    )

    with pytest.raises(ResourceLimitError, match="Export exceeded"):
        export(ExportRequest(str(source), str(tmp_path / "out"), settings))

    assert not list((tmp_path / "out").glob(".platefold-*.tmp"))


def test_service_rejects_sources_that_map_to_the_same_pdf(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "notes.md").write_text("# Lowercase extension", encoding="utf-8")
    (source / "notes.MD").write_text("# Uppercase extension", encoding="utf-8")
    if len(list(source.iterdir())) != 2:
        pytest.skip("this filesystem ignores case, so the two sources are one file")
    output = tmp_path / "out"

    with pytest.raises(ValueError, match="map to the same output PDF"):
        export(ExportRequest(str(source), str(output)))

    assert not output.exists()


def test_names_differing_only_in_case_collide_where_the_destination_ignores_case(
    tmp_path, monkeypatch
):
    import platefold.service as service

    sources = [str(tmp_path / "Notes.md"), str(tmp_path / "notes.csv")]
    output = str(tmp_path / "out")
    monkeypatch.setattr(service, "ignores_case", lambda _directory: True)
    with pytest.raises(ValueError, match="map to the same output PDF"):
        service.plan_outputs(sources, str(tmp_path), output)
    if os.path.normcase("A") != os.path.normcase("a"):
        # Windows paths always compare without case, whatever the volume says.
        monkeypatch.setattr(service, "ignores_case", lambda _directory: False)
        assert len(service.plan_outputs(sources, str(tmp_path), output)) == 2


def test_case_probe_matches_the_filesystem_under_test(tmp_path):
    from platefold.service import ignores_case

    (tmp_path / "probe").write_text("x", encoding="utf-8")
    merged = (tmp_path / "PROBE").exists()
    assert ignores_case(str(tmp_path / "not-created-yet")) is merged


@pytest.mark.parametrize("args", [[], ["absent.md"], ["--dpi", "nope"], ["--not-a-flag"]])
def test_json_input_errors_have_one_result_and_a_useful_exit_code(args, capsys):
    assert main(["--json", *args]) == 64
    result = json.loads(capsys.readouterr().out)
    assert result["schema_version"] == 1
    assert result["status"] == "failed"
    assert result["exit_code"] == 64
    assert result["error"]["code"] == "invalid_input"
    assert result["error"]["message"]


def test_json_theme_listing_needs_no_browser(capsys):
    assert main(["--list-themes", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["command"] == "list_themes"
    assert len(result["themes"]) == 10
    assert {"bristol", "bond", "newsprint"} <= {t["name"] for t in result["themes"]}
    assert all(t["source"] and t["mode"] in ("light", "dark") for t in result["themes"])


@pytest.mark.parametrize("error", [OSError, RuntimeError, PdfReadError])
def test_json_runtime_error_is_structured(error, fixture_doc, monkeypatch, capsys):
    from platefold import cli

    def fail(*args, **kwargs):
        raise error("Output volume is unavailable")

    monkeypatch.setattr(cli, "export", fail)
    assert main([str(fixture_doc("wide_diagram.md")), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"] == {"code": "export_failed", "message": "Output volume is unavailable"}


def test_service_missing_browser_does_not_exit(fixture_doc, tmp_path, capsys):
    with pytest.raises(RuntimeError, match="Browser executable not found"):
        export(
            ExportRequest(
                str(fixture_doc("wide_diagram.md")),
                str(tmp_path / "out"),
                browser=str(tmp_path / "absent-browser"),
            )
        )
    assert capsys.readouterr().out == ""


def test_missing_renderer_dependency_does_not_exit(tmp_path):
    from platefold.mermaid import find_lib

    with pytest.raises(RuntimeError, match="package is incomplete"):
        find_lib(str(tmp_path))


@pytest.mark.integration
def test_direct_service_exports_pdf_and_reports_progress_without_console_output(
    fixture_doc, tmp_path, capsys
):
    events = []
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "unrelated.txt").write_text("Keep this file", encoding="utf-8")
    result = export(
        ExportRequest(
            str(fixture_doc("wide_diagram.md")),
            str(tmp_path / "out"),
            diagram_cache=str(cache),
            clear_cache=True,
        ),
        on_progress=lambda *event: events.append(event),
    )
    assert capsys.readouterr().out == ""
    assert (cache / "unrelated.txt").read_text(encoding="utf-8") == "Keep this file"
    assert result.exit_code == 0
    assert result.status == "completed_with_warnings"
    assert any("Document language" in w for w in result.report["documents"][0]["warnings"])
    assert events[0][:3] == (0, 1, "wide_diagram.md")
    assert events[0][3] is None
    assert events[-1][:3] == (1, 1, "wide_diagram.md")
    assert result.report["documents"][0]["pages"] == len(
        PdfReader(tmp_path / "out" / "wide_diagram.pdf").pages
    )
    assert json.loads((tmp_path / "out" / "_export-report.json").read_text()) == result.report


@pytest.mark.integration
def test_json_export_contains_report_and_keeps_progress_on_stderr(fixture_doc, tmp_path, capsys):
    code = main(
        [
            str(fixture_doc("wide_diagram.md")),
            "-o",
            str(tmp_path / "out"),
            "--json",
            "--timings",
        ]
    )
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert code == result["exit_code"] == 0
    assert result["status"] == "completed_with_warnings"
    assert any("Document language" in w for w in result["report"]["documents"][0]["warnings"])
    assert result["report"]["documents"][0]["pages"] > 0
    assert "wide_diagram.md" in captured.err
    assert "Phase timings" in captured.err


@pytest.mark.integration
def test_json_export_preserves_nonfatal_missing_image_status(tmp_path, capsys):
    doc = tmp_path / "missing.md"
    doc.write_text("# Missing image\n\n![Missing](absent.png)\n", encoding="utf-8")
    assert main([str(doc), "-o", str(tmp_path / "out"), "--json", "--quiet"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "completed_with_warnings"
    assert result["report"]["documents"][0]["missing_images"] == ["absent.png"]


@pytest.mark.integration
def test_pdf_output_limit_discards_the_staged_document(tmp_path):
    source = tmp_path / "notes.md"
    source.write_text("# Notes\n\nRendered text.\n", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    report = output / "_export-report.json"
    report.write_text('{"prior": true}\n', encoding="utf-8")
    settings = make_settings(
        toc=False,
        bookmarks=False,
        limits=ResourceLimits(max_pdf_bytes=100),
    )

    with pytest.raises(ResourceLimitError, match="Rendered PDF exceeds"):
        export(
            ExportRequest(
                str(source),
                str(output),
                settings,
                diagram_cache=str(tmp_path / "cache"),
            )
        )

    assert not (output / "notes.pdf").exists()
    assert json.loads(report.read_text(encoding="utf-8")) == {"prior": True}
    assert not list(output.glob(".platefold-*.tmp"))


@pytest.mark.integration
def test_mid_batch_input_limit_keeps_completed_pdf_and_prior_report(tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.md").write_text("# A\n\nshort\n", encoding="utf-8")
    (source / "b.md").write_text("# B\n\n" + "x" * 100, encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    report = output / "_export-report.json"
    report.write_text('{"prior": true}\n', encoding="utf-8")
    settings = make_settings(
        toc=False,
        bookmarks=False,
        limits=ResourceLimits(max_markdown_bytes=32),
    )

    with pytest.raises(ResourceLimitError, match="Markdown file exceeds"):
        export(
            ExportRequest(
                str(source),
                str(output),
                settings,
                diagram_cache=str(tmp_path / "cache"),
            )
        )

    assert len(PdfReader(output / "a.pdf").pages) >= 1
    assert not (output / "b.pdf").exists()
    assert json.loads(report.read_text(encoding="utf-8")) == {"prior": True}
    assert not list(output.glob(".platefold-*.tmp"))


@pytest.mark.integration
def test_service_cancellation_returns_partial_report(tmp_path, capsys):
    src = tmp_path / "src"
    src.mkdir()
    for name in ["a.md", "b.md"]:
        (src / name).write_text("# Example\n\nText.\n", encoding="utf-8")
    finished = []

    def progress(done, total, rel, record):
        if record:
            finished.append(rel)

    result = export(
        ExportRequest(str(src), str(tmp_path / "out"), diagram_cache=str(tmp_path / "cache")),
        on_progress=progress,
        should_cancel=lambda: bool(finished),
    )
    assert capsys.readouterr().out == ""
    assert result.exit_code == 2
    assert result.status == "cancelled"
    assert result.total_documents == 2
    assert [d["file"] for d in result.report["documents"]] == ["a.md"]
    assert not (tmp_path / "out" / "b.pdf").exists()


@pytest.mark.parametrize("failure", ["raises", "corrupt", "replace"])
def test_failed_render_preserves_existing_pdf_and_report(
    tmp_path, monkeypatch, stubbed_export_engine, failure
):
    source = tmp_path / "notes.md"
    source.write_text("# Replacement", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    pdf = output / "notes.pdf"
    before_pdf = _write_minimal_pdf(pdf)
    report = output / "_export-report.json"
    before_report = b'{"prior": true}\n'
    report.write_bytes(before_report)

    def fail_print(_page, _html, path, _info, _rel, _settings, **_options):
        if failure == "replace":
            _write_minimal_pdf(Path(path), title="unpublished replacement")
            return
        Path(path).write_bytes(b"%PDF-1.7\npartial output")
        if failure == "raises":
            raise OSError("simulated full output volume")

    monkeypatch.setattr(stubbed_export_engine, "print_pdf", fail_print)
    if failure == "replace":
        real_replace = os.replace

        def fail_pdf_replace(source_path, destination_path):
            if Path(destination_path).name == "notes.pdf":
                raise OSError("simulated PDF replace failure")
            real_replace(source_path, destination_path)

        monkeypatch.setattr(stubbed_export_engine.os, "replace", fail_pdf_replace)
    settings = make_settings(toc=False, bookmarks=False)
    with pytest.raises((OSError, PdfReadError)):
        export(ExportRequest(str(source), str(output), settings))

    assert pdf.read_bytes() == before_pdf
    assert PdfReader(pdf).metadata.title == "known good"
    assert report.read_bytes() == before_report
    assert not list(output.glob(".platefold-*.tmp"))


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_report_failure_preserves_existing_report(
    tmp_path, monkeypatch, stubbed_export_engine, failure
):
    source = tmp_path / "notes.md"
    source.write_text("# Replacement", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    report = output / "_export-report.json"
    before_report = b'{"prior": true}\n'
    report.write_bytes(before_report)

    def print_valid(_page, _html, path, _info, _rel, _settings, **_options):
        _write_minimal_pdf(Path(path), title="replacement")

    real_replace = os.replace

    def fail_report_replace(source_path, destination_path):
        if Path(destination_path).name == "_export-report.json":
            raise OSError("simulated report replace failure")
        real_replace(source_path, destination_path)

    def fail_report_write(_payload, stream, **_kwargs):
        stream.write('{"partial":')
        raise OSError("simulated report write failure")

    monkeypatch.setattr(stubbed_export_engine, "print_pdf", print_valid)
    if failure == "replace":
        monkeypatch.setattr(stubbed_export_engine.os, "replace", fail_report_replace)
    else:
        monkeypatch.setattr(stubbed_export_engine.json, "dump", fail_report_write)
    settings = make_settings(toc=False, bookmarks=False)
    with pytest.raises(OSError, match=f"report {failure}"):
        export(ExportRequest(str(source), str(output), settings))

    assert report.read_bytes() == before_report
    # The new PDF was published: the final rewrite gave it the document's own
    # properties in place of the printed file's.
    assert PdfReader(output / "notes.pdf").metadata.title == "Synthetic"
    assert not list(output.glob(".platefold-*.tmp"))


def test_bookmark_failure_preserves_the_rendered_pdf(tmp_path, monkeypatch):
    from platefold import pdf as pdf_module

    target = tmp_path / "notes.pdf"
    before = _write_minimal_pdf(target)

    def partial_finish(_source, staged, *_args, **_kwargs):
        Path(staged).write_bytes(b"%PDF-1.7\npartial bookmarks")
        raise OSError("simulated bookmark write failure")

    monkeypatch.setattr(pdf_module, "finish_file", partial_finish)
    warning = pdf_module.add_bookmarks(
        str(target),
        {
            "title": "Replacement",
            "version": "",
            "author": "",
            "subject": "",
            "order": ["heading"],
            "names": {"heading": "Heading"},
            "levels": {"heading": 1},
        },
        {"heading": 1},
    )

    assert warning == "Bookmarks skipped: simulated bookmark write failure"
    assert target.read_bytes() == before
    assert len(PdfReader(target).pages) == 1
    assert not list(tmp_path.glob(".platefold-*.tmp"))


def _bookmark_info() -> dict:
    return {
        "title": "Quarterly ü",
        "version": "2.1",
        "author": "Operations",
        "subject": "",
        "order": ["summary", "north", "details"],
        "names": {"summary": "Summary", "north": "North ü", "details": "Details"},
        "levels": {"summary": 1, "north": 2, "details": 1},
    }


def _three_blank_pages(path: Path) -> None:
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=72, height=72)
    with path.open("wb") as fh:
        writer.write(fh)


def _assert_finished(path: Path) -> None:
    with PdfReader(path) as reader:
        outline = reader.outline
        assert [outline[0].title, outline[2].title] == ["Summary", "Details"]
        assert [item.title for item in outline[1]] == ["North ü"]
        pages = [reader.get_destination_page_number(item) for item in (outline[0], outline[2])]
        assert pages == [0, 2]
        assert reader.get_destination_page_number(outline[1][0]) == 1
        assert reader.metadata.title == "Quarterly ü"
        assert reader.metadata["/Keywords"] == "version 2.1"
        assert reader.metadata["/Producer"] == "platefold"
        xmp = reader.root_object["/Metadata"].get_object().get_data().decode("utf-8")
        assert "<dc:title>" in xmp and "Quarterly ü" in xmp


def test_bookmarks_and_metadata_are_written_in_one_streaming_pass(tmp_path, monkeypatch):
    from platefold import pdf as pdf_module

    target = tmp_path / "report.pdf"
    _three_blank_pages(target)

    def no_fallback(*_args, **_kwargs):
        raise AssertionError("a classic cross-reference file must not need pypdf")

    monkeypatch.setattr(pdf_module, "_finish_with_pypdf", no_fallback)
    warning = pdf_module.add_bookmarks(
        str(target), _bookmark_info(), {"summary": 1, "north": 2, "details": 3}
    )

    assert warning is None
    _assert_finished(target)


def test_bookmarks_fall_back_to_pypdf_for_a_file_outside_chromiums_shape(tmp_path, monkeypatch):
    from platefold import pdf as pdf_module
    from platefold.pdfobjects import PdfShapeError

    target = tmp_path / "other.pdf"
    _three_blank_pages(target)

    def other_shape(*_args, **_kwargs):
        raise PdfShapeError("not a classic cross-reference table")

    monkeypatch.setattr(pdf_module, "finish_file", other_shape)
    warning = pdf_module.add_bookmarks(
        str(target), _bookmark_info(), {"summary": 1, "north": 2, "details": 3}
    )

    assert warning is None
    _assert_finished(target)


@pytest.mark.integration
@pytest.mark.parametrize("toc", [False, True])
def test_bookmark_staging_failure_is_a_warning_through_export(tmp_path, monkeypatch, toc):
    from platefold import pdf as pdf_module
    from platefold import service

    source = tmp_path / "notes.md"
    source.write_text("# A\n\nFirst section.\n\n# B\n\nSecond section.\n", encoding="utf-8")
    real_add_bookmarks = pdf_module.add_bookmarks

    def fail_staging(*_args, **_kwargs):
        raise PermissionError("simulated bookmark staging failure")

    def add_with_staging_failure(*args, **kwargs):
        # Inject only in post-processing; printing and publication use real files.
        with monkeypatch.context() as postprocess:
            postprocess.setattr(pdf_module.tempfile, "mkstemp", fail_staging)
            return real_add_bookmarks(*args, **kwargs)

    monkeypatch.setattr(service, "add_bookmarks", add_with_staging_failure)
    result = export(
        ExportRequest(
            str(source),
            str(tmp_path / "out"),
            make_settings(font_set="sora", toc=toc, toc_min_headings=2, document_language="en"),
            diagram_cache=str(tmp_path / "cache"),
        )
    )

    assert result.status == "completed_with_warnings"
    assert result.exit_code == 0
    expected = ["Bookmarks skipped: simulated bookmark staging failure"]
    assert result.report["documents"][0]["warnings"] == expected
    assert json.loads(Path(result.report_path).read_text()) == result.report
    with PdfReader(tmp_path / "out" / "notes.pdf") as reader:
        assert len(reader.pages) == (2 if toc else 1)
        text = "\n".join(page.extract_text() for page in reader.pages)
        assert "First section." in text and "Second section." in text
    assert not list((tmp_path / "out").glob(".platefold-*.tmp"))


def test_cancellation_during_a_document_discards_its_staged_replacement(
    tmp_path, monkeypatch, stubbed_export_engine
):
    source = tmp_path / "notes.md"
    source.write_text("# Replacement", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    pdf = output / "notes.pdf"
    before = _write_minimal_pdf(pdf)
    report = output / "_export-report.json"
    before_report = b'{"documents": [{"file": "notes.md", "pages": 1}]}\n'
    report.write_bytes(before_report)
    cancel = False

    def print_then_cancel(_page, _html, path, _info, _rel, _settings, **_options):
        nonlocal cancel
        _write_minimal_pdf(Path(path), title="cancelled replacement")
        cancel = True

    monkeypatch.setattr(stubbed_export_engine, "print_pdf", print_then_cancel)
    settings = make_settings(toc=False, bookmarks=False)
    result = export(ExportRequest(str(source), str(output), settings), should_cancel=lambda: cancel)

    assert result.status == "cancelled"
    assert result.report["documents"] == []
    assert result.report_path == ""
    assert pdf.read_bytes() == before
    assert report.read_bytes() == before_report
    assert not list(output.glob(".platefold-*.tmp"))


def test_zero_document_cancellation_does_not_publish_an_empty_report(
    tmp_path, stubbed_export_engine
):
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")
    output = tmp_path / "out"

    result = export(
        ExportRequest(str(source), str(output), make_settings(toc=False, bookmarks=False)),
        should_cancel=lambda: True,
    )

    assert result.status == "cancelled"
    assert result.report["documents"] == []
    assert result.report_path == ""
    assert not (output / "_export-report.json").exists()


def test_plain_export_skips_diagram_engine_and_reports_activity(
    tmp_path, stubbed_export_engine, monkeypatch
):
    service = stubbed_export_engine
    source = tmp_path / "table.csv"
    source.write_text("Title,Author\nA book,An author\n")

    def unexpected_renderer(*args, **kwargs):
        pytest.fail("Plain CSV should not start a diagram engine")

    monkeypatch.setattr(service, "MermaidRenderer", unexpected_renderer)
    monkeypatch.setattr(
        service,
        "print_pdf",
        lambda _page, _html, path, *_args, **_options: _write_minimal_pdf(Path(path)),
    )
    phases = []
    result = export(
        ExportRequest(
            str(source), str(tmp_path / "out"), make_settings(toc=False, bookmarks=False)
        ),
        on_activity=phases.append,
    )
    assert result.status == "completed"
    assert phases == [
        "prepare",
        "launch browser",
        "read source",
        "render html",
        "print pdf",
        "finish pdf",
        "validate pdf",
    ]


def test_system_browser_qualification_cancellation_returns_without_publishing(
    tmp_path, monkeypatch
):
    from platefold import service

    source = tmp_path / "sample.md"
    source.write_text("# Sample")
    cancelled = False

    def launch(_choice, *, cancel_check):
        nonlocal cancelled
        cancelled = True
        cancel_check()
        raise AssertionError("cancellation must interrupt qualification")

    monkeypatch.setattr(service, "browser_launch", launch)
    result = export(
        ExportRequest(str(source), str(tmp_path / "out"), browser="system"),
        should_cancel=lambda: cancelled,
    )
    assert result.status == "cancelled"
    assert result.report_path == ""
    assert not list((tmp_path / "out").glob("*.pdf"))


def _print_minimal_pdf(_page, _html, path, _info, _rel, _settings, **_options):
    _write_minimal_pdf(Path(path), title="replacement")


def _cancel_part_way_through_import(monkeypatch, service, target: Path, after_checks: int = 10):
    """Return a should_cancel that turns true part-way through importing `target`."""
    state = {"importing": False, "checks": 0}
    real_ingest = service.ingest

    def tracked_ingest(path, *args, **kwargs):
        state["importing"] = Path(path).resolve() == target.resolve()
        try:
            return real_ingest(path, *args, **kwargs)
        finally:
            state["importing"] = False

    def should_cancel():
        if state["importing"]:
            state["checks"] += 1
        return state["checks"] >= after_checks

    monkeypatch.setattr(service, "ingest", tracked_ingest)
    return should_cancel, state


@pytest.mark.parametrize("fmt", ["markdown", "csv", "docx", "xlsx"])
def test_cancelling_an_import_keeps_completed_documents_and_prior_outputs(
    tmp_path, monkeypatch, stubbed_export_engine, fmt
):
    from test_ingest_cancellation import FORMATS

    source = tmp_path / "src"
    source.mkdir()
    (source / "a.md").write_text("# First\n\nCompleted before the cancel.", encoding="utf-8")
    build = tmp_path / "build"
    build.mkdir()
    large = FORMATS[fmt][0](build)
    cancelled_source = large.rename(source / f"b{large.suffix}")
    output = tmp_path / "out"
    output.mkdir()
    prior_pdf = _write_minimal_pdf(output / "b.pdf", title="known good")
    (output / "_export-report.json").write_bytes(b'{"prior": true}\n')
    monkeypatch.setattr(stubbed_export_engine, "print_pdf", _print_minimal_pdf)
    should_cancel, state = _cancel_part_way_through_import(
        monkeypatch, stubbed_export_engine, cancelled_source
    )

    result = export(
        ExportRequest(str(source), str(output), make_settings(toc=False, bookmarks=False)),
        should_cancel=should_cancel,
    )

    # Cancelled inside the second import, which was not allowed to finish.
    assert state["checks"] == 10 and not state["importing"]
    assert result.status == "cancelled"
    assert [d["file"] for d in result.report["documents"]] == ["a.md"]
    assert PdfReader(output / "a.pdf").metadata.title == "Synthetic"
    assert (output / "b.pdf").read_bytes() == prior_pdf
    assert json.loads((output / "_export-report.json").read_text()) == result.report
    assert cancelled_source.exists()
    assert not list(output.glob(".platefold-*.tmp"))


def test_cancelling_a_source_theme_import_saves_nothing(
    tmp_path, monkeypatch, stubbed_export_engine
):
    from test_ingest_cancellation import FORMATS

    source = FORMATS["docx"][0](tmp_path)
    themes = tmp_path / "themes"
    monkeypatch.setattr("platefold.theme.user_theme_dir", lambda: themes)
    should_cancel, state = _cancel_part_way_through_import(
        monkeypatch, stubbed_export_engine, source
    )

    result = export(
        ExportRequest(
            str(source),
            str(tmp_path / "out"),
            make_settings(source_theme=True, toc=False, bookmarks=False),
            save_theme="office-proof",
        ),
        should_cancel=should_cancel,
    )

    assert state["checks"] == 10
    assert result.status == "cancelled"
    assert result.report_path == ""
    assert "saved_theme" not in result.report
    assert not themes.exists() or not list(themes.iterdir())
    assert not (tmp_path / "out").exists()


def _disk_full(*_args, **_kwargs):
    raise OSError(errno.ENOSPC, "No space left on device")


def _prior_outputs(tmp_path: Path) -> tuple[Path, Path, bytes, bytes]:
    source = tmp_path / "notes.md"
    source.write_text("# Replacement", encoding="utf-8")
    output = tmp_path / "out"
    output.mkdir()
    before_pdf = _write_minimal_pdf(output / "notes.pdf")
    before_report = b'{"prior": true}\n'
    (output / "_export-report.json").write_bytes(before_report)
    return source, output, before_pdf, before_report


def _inject_disk_full(monkeypatch, service, where: str) -> None:
    if where == "pdf":

        def print_until_full(_page, _html, path, _info, _rel, _settings, **_options):
            Path(path).write_bytes(b"%PDF-1.7\npartial")
            _disk_full()

        monkeypatch.setattr(service, "print_pdf", print_until_full)
    else:
        monkeypatch.setattr(service, "print_pdf", _print_minimal_pdf)

        def dump_until_full(_payload, stream, **_kwargs):
            stream.write('{"partial":')
            _disk_full()

        monkeypatch.setattr(service.json, "dump", dump_until_full)


def _assert_prior_outputs_kept(output: Path, before_pdf: bytes, before_report: bytes, where: str):
    if where == "pdf":
        assert (output / "notes.pdf").read_bytes() == before_pdf
    else:
        # The PDF completed before the disk filled; only the report failed.
        assert PdfReader(output / "notes.pdf").metadata.title == "Synthetic"
    assert (output / "_export-report.json").read_bytes() == before_report
    assert not list(output.glob(".platefold-*.tmp"))


@pytest.mark.parametrize("where", ["pdf", "report"])
def test_cli_disk_full_reports_failure_and_keeps_prior_outputs(
    tmp_path, monkeypatch, capsys, stubbed_export_engine, where
):
    source, output, before_pdf, before_report = _prior_outputs(tmp_path)
    _inject_disk_full(monkeypatch, stubbed_export_engine, where)

    code = main([str(source), "-o", str(output), "--no-toc", "--no-bookmarks", "--json"])

    result = json.loads(capsys.readouterr().out)
    assert code == 1
    assert result["status"] == "failed"
    assert result["error"]["code"] == "export_failed"
    assert "No space left on device" in result["error"]["message"]
    _assert_prior_outputs_kept(output, before_pdf, before_report, where)


@pytest.mark.parametrize("where", ["pdf", "report"])
def test_desktop_disk_full_reports_failure_and_keeps_prior_outputs(
    tmp_path, monkeypatch, capsys, stubbed_export_engine, where
):
    from platefold.worker import DesktopWorker

    source, output, before_pdf, before_report = _prior_outputs(tmp_path)
    _inject_disk_full(monkeypatch, stubbed_export_engine, where)
    worker = DesktopWorker()
    worker.busy.set()
    try:
        worker.render_export(
            {
                "id": "export-1",
                "input_path": str(source),
                "output_dir": str(output),
                "settings": {"toc": False, "bookmarks": False},
            }
        )
    finally:
        worker.close()

    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[-1]["event"] == "error"
    assert events[-1]["id"] == "export-1"
    assert "No space left on device" in events[-1]["message"]
    assert events[-1]["help_id"] == "help.retry"
    # The worker is free to accept the next job.
    assert not worker.busy.is_set()
    _assert_prior_outputs_kept(output, before_pdf, before_report, where)
