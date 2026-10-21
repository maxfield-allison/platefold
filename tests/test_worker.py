"""Exercise the desktop protocol over real pipes and render real documents."""

import json
import os
import queue
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
from pypdf import PdfReader

from platefold.theme import COLOR_TOKENS, THEMES
from platefold.worker import (
    MAX_RESPONSE_BYTES,
    catalog,
    delete_theme,
    overwrite_theme,
    request_from,
    response_line,
    save_copy,
)


def test_response_byte_boundary_includes_newline_and_preserves_small_events(monkeypatch):
    import platefold.worker as module

    payload = {"catalog": {"themes": ["Field notes ü" * 30]}}
    expected = json.dumps({"schema_version": 1, "event": "catalog", "id": "catalog", **payload})
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", len(expected.encode("utf-8")) + 1)
    assert response_line("catalog", "catalog", **payload) == expected + "\n"
    # One byte less excludes the required newline, so return a framed error.
    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", len(expected.encode("utf-8")))
    line = response_line("catalog", "catalog", **payload)
    assert len(line.encode("utf-8")) <= module.MAX_RESPONSE_BYTES and line.endswith("\n")
    assert json.loads(line)["code"] == "response_too_large"


def test_oversized_preview_preserves_preview_error_channel(monkeypatch):
    import platefold.worker as module

    monkeypatch.setattr(module, "MAX_RESPONSE_BYTES", 1024)
    line = response_line("preview", "preview-123", preview={"warnings": ["x" * 2048]})
    event = json.loads(line)
    assert event["event"] == "preview_error" and event["id"] == "preview-123"
    assert len(line.encode("utf-8")) <= 1024 and event["code"] == "response_too_large"


def test_oversized_progress_keeps_counts_and_path_without_losing_pipe_framing():
    record = {"missing_images": ["https://example.invalid/" + "x" * MAX_RESPONSE_BYTES]}
    event = json.loads(
        response_line("progress", "export", done=1, total=2, file="Field notes ü.md", record=record)
    )
    assert event["event"] == "progress" and event["id"] == "export"
    assert event["done"] == 1 and event["total"] == 2 and event["file"] == "Field notes ü.md"
    assert "record" not in event and "export report" in event["diagnostics_omitted"]
    assert len(record["missing_images"][0]) > MAX_RESPONSE_BYTES


def test_oversized_result_preserves_full_report_and_document_paths(tmp_path):
    report_path = tmp_path / "_export-report.json"
    documents = [{"file": "nested/Field notes ü.md", "warnings": ["x" * MAX_RESPONSE_BYTES]}]
    report = {"documents": documents}
    report_path.write_text(json.dumps(report), encoding="utf-8")
    result = {
        "status": "completed_with_warnings",
        "report_path": str(report_path),
        "output_dir": str(tmp_path),
        "report": report,
    }
    line = response_line("result", "export", result=result)
    event = json.loads(line)
    assert len(line.encode("utf-8")) <= MAX_RESPONSE_BYTES and line.endswith("\n")
    assert event["event"] == "error" and event["id"] == "export"
    assert event["code"] == "response_too_large"
    assert event["help_id"] == "help.resource_limit"
    assert str(report_path) in event["message"] and str(tmp_path) in event["message"]
    assert "result" not in event  # No partial document list can shift native open indexes.
    assert json.loads(report_path.read_text(encoding="utf-8")) == report
    assert documents[0]["file"] == "nested/Field notes ü.md"
    assert len(documents[0]["warnings"][0]) == MAX_RESPONSE_BYTES


def test_real_worker_recovers_after_oversized_catalog_response():
    # A synthetic producer makes a large catalog; the process, dispatcher,
    # serializer, pipes and subsequent ordinary catalog are all real.
    program = (
        "import platefold.worker as worker; original = worker.catalog; "
        "worker.catalog = lambda source=None: "
        "{'themes': ['x' * worker.MAX_RESPONSE_BYTES]} if source == 'large' else original(source); "
        "raise SystemExit(worker.main())"
    )
    requests = [
        {"schema_version": 1, "id": "large", "command": "catalog", "input_path": "large"},
        {"schema_version": 1, "id": "normal", "command": "catalog"},
    ]
    result = subprocess.run(
        [sys.executable, "-c", program],
        input="".join(json.dumps(message) + "\n" for message in requests),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines(keepends=True)
    assert all(
        line.endswith("\n") and len(line.encode("utf-8")) <= MAX_RESPONSE_BYTES for line in lines
    )
    ready, oversized, recovered = [json.loads(line) for line in lines]
    assert ready["event"] == "ready"
    assert oversized["event"] == "error" and oversized["id"] == "large"
    assert oversized["code"] == "response_too_large"
    assert recovered["event"] == "catalog" and recovered["id"] == "normal"
    assert len(recovered["catalog"]["themes"]) == 11
    assert recovered["catalog"]["themes"][0]["kind"] == "source"


def stop_worker(process):
    if not process.stdin.closed:
        process.stdin.close()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        pytest.fail("Worker did not shut down when stdin closed")
    assert process.returncode == 0, process.stderr.read()


def worker_config_dir(tmp_path):
    """Where a worker started by start_worker keeps its configuration."""
    if sys.platform == "darwin":
        return tmp_path / "home" / "Library" / "Application Support"
    return tmp_path / "config"


@pytest.fixture
def start_worker(tmp_path):
    """Start real worker processes whose user themes live under tmp_path.

    The in-process isolation in conftest cannot reach a subprocess, so the
    configuration directory is redirected through the environment instead:
    XDG_CONFIG_HOME on Linux, APPDATA on Windows and HOME on macOS.
    """
    config = worker_config_dir(tmp_path)
    env = {**os.environ, "XDG_CONFIG_HOME": str(config), "APPDATA": str(config)}
    if sys.platform == "darwin":
        from platefold.browser import playwright_cache

        # Keep the browser the real home folder's cache already holds.
        env["PLAYWRIGHT_BROWSERS_PATH"] = str(playwright_cache())
        env["HOME"] = str(tmp_path / "home")
    processes = []

    def start():
        process = subprocess.Popen(
            [sys.executable, "-m", "platefold", "--desktop-worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        processes.append(process)
        events = queue.Queue()

        def read():
            for line in process.stdout:
                events.put(json.loads(line))

        threading.Thread(target=read, daemon=True).start()

        def send(command, **fields):
            process.stdin.write(
                json.dumps({"schema_version": 1, "id": command, "command": command, **fields})
                + "\n"
            )
            process.stdin.flush()

        def receive():
            return events.get(timeout=60)

        assert receive()["event"] == "ready"
        return SimpleNamespace(
            send=send, receive=receive, stop=lambda: stop_worker(process), process=process
        )

    yield start
    for process in processes:
        if process.poll() is None:
            stop_worker(process)


@pytest.fixture
def worker(start_worker):
    started = start_worker()
    return started.send, started.receive


def test_catalog_and_bad_command_keep_worker_usable(worker):
    send, receive = worker
    send("not-a-command")
    assert receive()["event"] == "error"
    send("catalog")
    result = receive()
    assert result["event"] == "catalog"
    assert len(result["catalog"]["themes"]) == 11
    assert result["catalog"]["themes"][0]["kind"] == "source"
    assert any(f["name"] == "sora" for f in result["catalog"]["font_sets"])


def test_missing_source_is_reported_without_worker_exit(worker):
    send, receive = worker
    send("export", input_path="absent-source.md", output_dir="unused")
    assert receive()["event"] == "error"
    send("catalog")
    assert receive()["event"] == "catalog"


def test_desktop_options_reject_unknown_settings(tmp_path):
    source = tmp_path / "example.md"
    source.write_text("# Example")
    with pytest.raises(ValueError, match="Unknown desktop setting"):
        request_from(
            {
                "input_path": str(source),
                "output_dir": str(tmp_path / "out"),
                "settings": {"command": "anything"},
            }
        )


def test_desktop_request_accepts_csv_header_setting(tmp_path):
    source = tmp_path / "table.csv"
    source.write_text("1,2\n3,4\n")
    request = request_from(
        {
            "input_path": str(source),
            "output_dir": str(tmp_path / "out"),
            "settings": {"csv_header": False},
        }
    )
    assert request.settings.csv_header is False


def test_desktop_request_accepts_xlsx_sheet_selection(tmp_path):
    source = tmp_path / "table.xlsx"
    source.write_bytes(b"selection is validated during ingestion")
    request = request_from(
        {
            "input_path": str(source),
            "output_dir": str(tmp_path / "out"),
            "settings": {"xlsx_sheets": ["Summary", "#2"]},
        }
    )
    assert request.settings.xlsx_sheets == ("Summary", "#2")


def test_desktop_source_theme_keeps_fallback_and_optional_font_override(tmp_path):
    source = tmp_path / "notes.md"
    source.write_text("# Notes", encoding="utf-8")
    automatic = request_from(
        {
            "input_path": str(source),
            "output_dir": str(tmp_path / "out"),
            "settings": {"theme": "source", "colors": {"accent": "#123456"}},
        }
    ).settings
    assert automatic.source_theme is True
    assert automatic.source_fonts is True
    assert automatic.theme.name == "bristol"
    assert automatic.source_theme_colors == {"accent": "#123456"}

    explicit = request_from(
        {
            "input_path": str(source),
            "output_dir": str(tmp_path / "out"),
            "settings": {"theme": "source", "font_set": "arial"},
        }
    ).settings
    assert explicit.source_theme is True
    assert explicit.source_fonts is False
    assert explicit.fonts.name == "arial"


def test_catalog_preserves_theme_collision_warning(tmp_path, monkeypatch):
    monkeypatch.setattr("platefold.theme.user_theme_dir", lambda: tmp_path / "user")
    themes = tmp_path / "themes"
    themes.mkdir()
    (themes / "bristol.json").write_text(
        json.dumps({"name": "bristol", "base": "bristol", "colors": {"accent": "#123456"}})
    )
    result = catalog(str(tmp_path))
    assert any("overrides" in warning for warning in result["warnings"])
    assert (
        next(t for t in result["themes"] if t["name"] == "bristol")["colors"]["accent"] == "#123456"
    )


def user_dir(tmp_path, monkeypatch):
    directory = tmp_path / "themes"
    monkeypatch.setattr("platefold.worker.user_theme_dir", lambda: directory)
    monkeypatch.setattr("platefold.theme.user_theme_dir", lambda: directory)
    return directory


def test_save_copy_cannot_replace_builtins_or_escape_user_directory(tmp_path, monkeypatch):
    user_dir(tmp_path, monkeypatch)
    for name in ["bristol", "source", "../outside", "A name"]:
        with pytest.raises(ValueError):
            save_copy({"name": name, "theme": "bristol"})
    result = save_copy({"name": "my-proof", "theme": "bristol", "colors": {"accent": "#123456"}})
    copied = next(t for t in result["catalog"]["themes"] if t["name"] == "my-proof")
    assert copied["colors"]["accent"] == "#123456"
    with pytest.raises(ValueError, match="already exists"):
        save_copy({"name": "my-proof", "theme": "bristol"})
    assert result["path"] == str(tmp_path / "themes" / "my-proof.json")


def test_worker_catalog_and_save_accept_uppercase_docx_suffix(tmp_path, monkeypatch):
    from tests.test_docx_ingest import make_docx

    user_dir(tmp_path, monkeypatch)
    source = make_docx(tmp_path / "PROOF.DOCX")
    imported = catalog(str(tmp_path))["themes"][0]
    assert imported["name"] == "source"
    assert imported["colors"]["accent"] == "#2e6f95"
    saved = save_copy({"name": "uppercase-proof", "theme": "source", "input_path": str(source)})
    assert Path(saved["path"]).is_file()


def test_catalog_reports_where_each_theme_lives_and_its_contrast(tmp_path, monkeypatch):
    directory = user_dir(tmp_path, monkeypatch)
    save_copy({"name": "my-proof", "theme": "bristol"})
    project = tmp_path / "project" / "themes"
    project.mkdir(parents=True)
    (project / "faint.json").write_text(
        json.dumps({"name": "faint", "base": "bristol", "colors": {"ink_soft": "#e0d8c8"}})
    )
    result = catalog(str(tmp_path / "project"))
    by_name = {t["name"]: t for t in result["themes"]}
    assert by_name["bristol"]["kind"] == "builtin" and by_name["bristol"]["builtin"]
    assert by_name["my-proof"]["kind"] == "user" and not by_name["my-proof"]["builtin"]
    assert by_name["my-proof"]["path"] == str(directory / "my-proof.json")
    assert by_name["my-proof"]["file"] == "my-proof.json"
    assert by_name["faint"]["kind"] == "project" and not by_name["faint"]["builtin"]
    assert {(f["fg"], f["bg"]) for f in by_name["faint"]["contrast_failures"]} == {
        ("ink_soft", "page"),
        ("ink_soft", "surface_alt"),
    }
    assert not by_name["bristol"]["contrast_failures"]
    assert {rule["fg"] for rule in result["contrast_rules"]} >= {"ink", "diagram_text"}
    # Contrast is data for the editor, never a modal warning on every catalog load.
    assert result["warnings"] == []


def test_overwrite_and_delete_refuse_builtins_project_themes_and_unknown_names(
    tmp_path, monkeypatch
):
    user_dir(tmp_path, monkeypatch)
    project = tmp_path / "project" / "themes"
    project.mkdir(parents=True)
    (project / "team.json").write_text(json.dumps({"name": "team", "base": "bristol"}))
    cases = [
        ({"name": "bristol"}, "read-only"),
        ({"name": "team", "input_path": str(tmp_path / "project")}, "Project themes"),
        ({"name": "ghost"}, "no longer available"),
        ({"name": ""}, "name is required"),
    ]
    for message, error in cases:
        with pytest.raises(ValueError, match=error):
            overwrite_theme(message)
        with pytest.raises(ValueError, match=error):
            delete_theme(message)
    assert (project / "team.json").is_file()
    assert (Path(THEMES["bristol"].source)).is_file()


def test_overwrite_stores_the_complete_palette_and_keeps_metadata(tmp_path, monkeypatch):
    directory = user_dir(tmp_path, monkeypatch)
    save_copy({"name": "field-notes", "theme": "bristol", "colors": {"accent": "#123456"}})
    path = directory / "field-notes.json"
    before = json.loads(path.read_text(encoding="utf-8"))
    before["description"] = "Hand edited"
    before["fonts"] = {
        "body": "Georgia, serif",
        "head": "Georgia, serif",
        "mono": "Menlo, monospace",
    }
    path.write_text(json.dumps(before), encoding="utf-8")
    result = overwrite_theme(
        {"name": "field-notes", "colors": {"accent": "#654321", "ink": "#101010"}}
    )
    assert result["path"] == str(path)
    after = json.loads(path.read_text(encoding="utf-8"))
    assert after["colors"]["accent"] == "#654321" and after["colors"]["ink"] == "#101010"
    assert after["colors"]["page"] == before["colors"]["page"]
    assert after["description"] == "Hand edited" and after["label"] == "field-notes"
    assert after["fonts"]["body"] == "Georgia, serif"
    assert set(after["colors"]) == COLOR_TOKENS
    copied = next(t for t in result["catalog"]["themes"] if t["name"] == "field-notes")
    assert copied["colors"]["accent"] == "#654321" and copied["kind"] == "user"
    assert not list(directory.glob(".*.tmp"))
    with pytest.raises(ValueError, match="unknown theme colour"):
        overwrite_theme({"name": "field-notes", "colors": {"nope": "#000000"}})
    assert json.loads(path.read_text(encoding="utf-8")) == after


def test_overwriting_a_sparse_user_theme_writes_every_token_and_drops_its_base(
    tmp_path, monkeypatch
):
    directory = user_dir(tmp_path, monkeypatch)
    directory.mkdir()
    (directory / "tint.json").write_text(
        json.dumps({"name": "tint", "base": "bristol", "colors": {"accent": "#123456"}})
    )
    overwrite_theme({"name": "tint", "colors": {"link": "#654321"}})
    data = json.loads((directory / "tint.json").read_text(encoding="utf-8"))
    assert "base" not in data and set(data["colors"]) == COLOR_TOKENS
    assert data["colors"]["accent"] == "#123456" and data["colors"]["link"] == "#654321"
    assert data["colors"]["ink"] == THEMES["bristol"].ink


def test_overwrite_refuses_a_linked_theme_but_delete_removes_only_the_link(
    tmp_path, monkeypatch, symlinks
):
    directory = user_dir(tmp_path, monkeypatch)
    directory.mkdir()
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps({"name": "linked", "base": "bristol"}), encoding="utf-8")
    (directory / "linked.json").symlink_to(target)
    with pytest.raises(ValueError, match="is a link"):
        overwrite_theme({"name": "linked", "colors": {}})
    assert json.loads(target.read_text(encoding="utf-8"))["base"] == "bristol"
    delete_theme({"name": "linked"})
    assert target.is_file() and not (directory / "linked.json").exists()


def test_delete_removes_the_named_file_and_uncovers_a_shadowed_builtin(tmp_path, monkeypatch):
    directory = user_dir(tmp_path, monkeypatch)
    directory.mkdir()
    (directory / "bristol.json").write_text(
        json.dumps({"name": "bristol", "base": "bristol", "colors": {"accent": "#123456"}})
    )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # the collision is the point of this fixture
        shadowed = next(t for t in catalog()["themes"] if t["name"] == "bristol")
    assert shadowed["kind"] == "user" and shadowed["colors"]["accent"] == "#123456"
    result = delete_theme({"name": "bristol"})
    assert result["path"] == str(directory / "bristol.json")
    assert not (directory / "bristol.json").exists()
    restored = next(t for t in result["catalog"]["themes"] if t["name"] == "bristol")
    assert restored["kind"] == "builtin"
    assert restored["colors"]["accent"] == THEMES["bristol"].accent
    with pytest.raises(ValueError, match="read-only"):
        delete_theme({"name": "bristol"})


def test_theme_edits_survive_worker_restarts_until_deleted(start_worker, tmp_path):
    """The complete save, restart, overwrite, restart, delete, restart journey over real pipes."""
    first = start_worker()
    first.send("catalog")
    baseline = first.receive()["catalog"]
    assert baseline["themes"][0]["kind"] == "source"
    assert all(t["kind"] == "builtin" for t in baseline["themes"][1:])
    first.send("save_copy", name="field-notes", theme="bristol", colors={"accent": "#123456"})
    saved = first.receive()
    assert saved["event"] == "saved", saved
    path = Path(saved["path"])
    assert path == worker_config_dir(tmp_path) / "platefold" / "themes" / "field-notes.json"
    assert path.is_file()
    first.stop()

    second = start_worker()
    second.send("catalog")
    themes = {t["name"]: t for t in second.receive()["catalog"]["themes"]}
    assert themes["field-notes"]["kind"] == "user"
    assert themes["field-notes"]["colors"]["accent"] == "#123456"
    assert themes["field-notes"]["path"] == str(path)
    second.send("overwrite", name="field-notes", colors={"accent": "#654321"})
    event = second.receive()
    assert event["event"] == "overwritten" and event["path"] == str(path), event
    second.send("overwrite", name="bristol", colors={})
    refused = second.receive()
    assert refused["event"] == "error" and refused["id"] == "overwrite"
    assert "read-only" in refused["message"]
    second.stop()

    third = start_worker()
    third.send("catalog")
    themes = {t["name"]: t for t in third.receive()["catalog"]["themes"]}
    assert themes["field-notes"]["colors"]["accent"] == "#654321"
    assert json.loads(path.read_text(encoding="utf-8"))["label"] == "field-notes"
    third.send("delete", name="field-notes")
    event = third.receive()
    assert event["event"] == "deleted" and event["path"] == str(path), event
    assert not path.exists()
    assert {t["name"] for t in event["catalog"]["themes"]} == {
        t["name"] for t in baseline["themes"]
    }
    third.send("delete", name="field-notes")
    assert third.receive()["event"] == "error"
    third.send("catalog")
    assert third.receive()["event"] == "catalog"
    third.stop()

    fourth = start_worker()
    fourth.send("catalog")
    final = fourth.receive()["catalog"]
    assert len(final["themes"]) == len(baseline["themes"])
    assert final["themes"][0]["kind"] == "source"
    assert all(t["kind"] == "builtin" for t in final["themes"][1:])


@pytest.mark.integration
def test_worker_exports_with_bundled_fonts_and_progress(worker, tmp_path):
    send, receive = worker
    source = tmp_path / "Field notes ü.md"
    source.write_text(
        "# Field notes\n\nLocal fonts and Unicode paths.\n\n```mermaid\ngraph LR\n accTitle: Document export\n accDescr: Source becomes a printed page.\n A[Source] --> B[Printed page]\n```\n",
        encoding="utf-8",
    )
    out = tmp_path / "PDF output"
    send(
        "export",
        activity=True,
        input_path=str(source),
        output_dir=str(out),
        settings={"font_set": "sora", "theme": "bristol", "document_language": "en"},
    )
    progress = []
    phases = []
    while (event := receive())["event"] in {"progress", "activity"}:
        if event["event"] == "progress":
            progress.append(event)
        else:
            phases.append(event["phase"])
    assert phases[0] == "prepare"
    assert "print pdf" in phases
    assert phases[-1] == "validate pdf"
    assert event["event"] == "result", event
    assert event["result"]["status"] == "completed"
    assert progress[0]["done"] == 0 and progress[-1]["done"] == 1
    reader = PdfReader(out / "Field notes ü.pdf")
    fonts = {
        str(font.get_object().get("/BaseFont"))
        for page in reader.pages
        for font in page["/Resources"].get("/Font", {}).values()
    }
    assert any("Sora" in font for font in fonts), fonts
    assert "Local fonts" in reader.pages[0].extract_text()


@pytest.mark.integration
def test_worker_cancel_returns_completed_documents_and_allows_next_job(worker, tmp_path):
    send, receive = worker
    source = tmp_path / "notes"
    source.mkdir()
    for n in range(12):
        body = "Body text.\n\n" * 30
        if n == 0:
            body += "\n\n".join(
                f"```mermaid\ngraph LR\n A{i}[Start {i}] --> B{i}[Finish {i}]\n```"
                for i in range(60)
            )
        (source / f"{n:02}.md").write_text("# Notes\n\n" + body)
    send("export", input_path=str(source), output_dir=str(tmp_path / "out"))
    assert receive()["event"] == "progress"
    started = time.monotonic()
    send("cancel")
    while (event := receive())["event"] in {"progress", "cancelling"}:
        pass
    assert event["event"] == "result", event
    assert event["result"]["status"] == "cancelled"
    assert event["result"]["report"]["documents"] == []
    assert time.monotonic() - started < 5
    assert not list((tmp_path / "out").glob(".platefold-*.tmp"))
    send("catalog")
    assert receive()["event"] == "catalog"


@pytest.mark.integration
def test_worker_cancel_interrupts_a_large_import(worker, tmp_path):
    send, receive = worker
    source = tmp_path / "large.md"
    # Several seconds of Markdown conversion even on a fast machine.
    source.write_text(
        "# Large\n\n"
        + "".join(
            f"## Section {n}\n\nSome *emphasis*, **strong** text and a "
            "[link](https://example.com).\n\n"
            for n in range(40_000)
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"
    output.mkdir()
    (output / "large.pdf").write_bytes(b"%PDF-1.7\nprior export")
    send("export", input_path=str(source), output_dir=str(output))
    # The first progress event is sent as the document's import begins.
    first = receive()
    assert first["event"] == "progress", first
    started = time.monotonic()
    send("cancel")
    while (event := receive())["event"] in {"progress", "cancelling"}:
        pass
    assert event["event"] == "result", event
    assert event["result"]["status"] == "cancelled"
    assert time.monotonic() - started < 5
    assert (output / "large.pdf").read_bytes() == b"%PDF-1.7\nprior export"
    assert not list(output.glob(".platefold-*.tmp"))


@pytest.mark.integration
def test_worker_preview_needs_no_destination_and_keeps_folder_resource_root(worker, tmp_path):
    from PIL import Image

    send, receive = worker
    source = tmp_path / "source"
    nested = source / "a"
    nested.mkdir(parents=True)
    Image.new("RGB", (80, 80), "red").save(source / "shared.png")
    (nested / "first.md").write_text("# First source\n\n![Image](../shared.png)\n")
    (source / "z.md").write_text("# This document is not previewed\n")
    send(
        "preview", input_path=str(source), settings={"font_set": "sora", "document_language": "en"}
    )
    event = receive()
    assert event["event"] == "preview", event
    preview = event["preview"]
    assert preview["file"].replace("\\", "/") == "a/first.md"
    assert "First source" in preview["text"] and not preview["warnings"]
    assert not list(source.rglob("*.pdf"))
    assert not list(tmp_path.glob("_export-report.json"))
    send("catalog")
    assert receive()["event"] == "catalog"
    send("preview", input_path=str(source / "absent.md"))
    assert receive()["event"] == "preview_error"
    send("catalog")
    assert receive()["event"] == "catalog"
