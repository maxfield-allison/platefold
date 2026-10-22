"""HTTP ownership/admission/recovery checks and real Chromium export journeys."""

from __future__ import annotations

import asyncio
import errno
import hashlib
import io
import json
import os
import secrets
import sys
import time
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

pytest.importorskip("starlette")
pytest.importorskip("httpx")

from pypdf import PdfReader
from starlette.requests import ClientDisconnect
from starlette.testclient import TestClient

from platefold.server import create_app, token_hashes
from platefold.server_jobs import JobStore
from platefold.server_models import MIB, TERMINAL, ExportOptions, JobLimits, JobRequest, Mount

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux server contract")
ORIGIN = "https://testserver"


@pytest.fixture
def api(tmp_path, request):
    class TestCredentials(dict):
        def __repr__(self):
            return "<disposable test credentials>"

    credentials = TestCredentials(alice=secrets.token_urlsafe(32), bob=secrets.token_urlsafe(32))
    # Admission/ownership tests do not render. CI's unit image has no Chromium;
    # actual discovery and exports belong to the explicitly marked browser tests.
    browser = None if request.node.get_closest_marker("integration") else sys.executable
    app = create_app(tmp_path / "jobs", credentials, (ORIGIN,), browser=browser)
    with TestClient(app, base_url="https://testserver") as client:
        client.headers["Authorization"] = "Bearer " + credentials["alice"]
        yield client, app.state.jobs, credentials


def new_job(client, *, format="markdown", settings=None):
    response = client.post(
        "/api/v1/jobs",
        json={
            "source": {"kind": "upload", "format": format},
            "settings": settings or {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["links"]["status"]


def wait_job(client, url, *, timeout=35):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(url)
        assert response.status_code == 200
        if response.json()["state"] in TERMINAL:
            return response.json()
        time.sleep(0.05)
    pytest.fail("Job did not reach a terminal state")


def test_auth_origin_host_and_cors_are_checked_before_admission(api):
    client, store, credentials = api
    body = {"source": {"kind": "upload", "format": "markdown"}}
    client.headers.pop("Authorization")
    assert client.post("/api/v1/jobs", json=body).status_code == 401
    assert client.get("/api/v1/capabilities?token=" + credentials["alice"]).status_code == 401
    client.headers["Authorization"] = "Bearer " + credentials["alice"]
    for origin in ("null", "https://evil.example", ORIGIN + ".evil.example"):
        assert client.post("/api/v1/jobs", json=body, headers={"Origin": origin}).status_code == 403
    assert (
        client.post("/api/v1/jobs", json=body, headers={"Host": "evil.example"}).status_code == 400
    )
    assert (
        client.post("/api/v1/jobs", json=body, headers={"Cookie": "session=abc"}).status_code == 400
    )
    duplicate = [
        ("Authorization", "Bearer " + credentials["alice"]),
        ("Authorization", "Bearer " + credentials["bob"]),
    ]
    assert client.post("/api/v1/jobs", json=body, headers=duplicate).status_code == 401
    assert not store.jobs
    response = client.options(
        "/api/v1/jobs",
        headers={
            "Origin": ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization, Content-Type",
        },
    )
    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == ORIGIN
    assert "access-control-allow-credentials" not in response.headers


def test_every_job_operation_checks_owner(api):
    client, _store, credentials = api
    url = new_job(client)
    client.headers["Authorization"] = "Bearer " + credentials["bob"]
    for method, suffix in [
        ("GET", ""),
        ("GET", "/events"),
        ("GET", "/results"),
        ("GET", "/results/0"),
        ("POST", "/cancel"),
        ("PUT", "/source"),
    ]:
        response = client.request(method, url + suffix)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "job_not_found"


@pytest.mark.parametrize(
    "options",
    [
        {"theme": "/etc/passwd"},
        {"theme": "../secret"},
        {"image_dpi": True},
        {"image_dpi": "150"},
        {"image_dpi": 9999},
        {"toc": 1},
        {"document_language": "en_US"},
        {"document_language": True},
        {"browser": "/bin/sh"},
        {"limits": {"max_export_seconds": 999999}},
        {"font_set": "/etc/passwd"},
        {"colors": {"page": "url(file:///secret)"}},
        {"colors": {"unknown": "#000000"}},
    ],
)
def test_client_settings_cannot_select_paths_commands_or_limits(api, options):
    client, store, _credentials = api
    response = client.post(
        "/api/v1/jobs",
        json={
            "source": {"kind": "upload", "format": "markdown"},
            "settings": options,
        },
    )
    assert response.status_code == 422
    assert not store.jobs
    assert "/etc/" not in response.text


def test_body_media_query_limits_and_consistent_errors(api):
    client, _store, _credentials = api
    for payload in (b"{", b"null", b"[]", b"[" * 3000):
        response = client.post(
            "/api/v1/jobs", content=payload, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422
        assert set(response.json()) == {"schema_version", "error"}
    assert client.post("/api/v1/jobs", json={"extra": "x" * 17000}).status_code == 413
    assert client.post("/api/v1/jobs", content="{}").status_code == 415
    assert client.get("/api/v1/capabilities?token=sensitive").status_code == 400
    assert client.get("/not-a-route").status_code == 404


def test_upload_overflow_cancellation_and_failed_cleanup(api):
    client, store, _credentials = api
    store.limits = replace(store.limits, upload_bytes=8)
    url = new_job(client)
    response = client.put(
        url + "/source",
        content=iter([b"12345", b"6789"]),
        headers={"Content-Type": "text/markdown"},
    )
    assert response.status_code == 413
    assert client.get(url).json()["state"] == "failed"
    assert not (store.root / url.rsplit("/", 1)[-1] / "work").exists()
    assert (
        client.put(
            url + "/source", content=b"x", headers={"Content-Type": "text/markdown"}
        ).status_code
        == 409
    )
    url = new_job(client)
    assert client.post(url + "/cancel").json()["state"] == "cancelled"
    assert client.post(url + "/cancel").json()["state"] == "cancelled"
    assert client.get(url + "/results").status_code == 409


@pytest.mark.parametrize(
    "format,media_type",
    [
        ("csv", "text/csv"),
        ("tsv", "text/tab-separated-values"),
        (
            "docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ),
    ],
)
def test_document_upload_formats_and_media_types(api, format, media_type):
    client, store, _credentials = api
    # Keep the real enqueue transition; pause both immediate and periodic dispatch.
    store.schedule = lambda: None
    options = {"csv_header": False} if format in {"csv", "tsv"} else {"theme": "source"}
    if format == "xlsx":
        options["xlsx_sheets"] = ["Summary", "#2"]
    url = new_job(client, format=format, settings=options)
    job = next(iter(store.jobs.values()))
    if format in {"csv", "tsv"}:
        assert job.request["settings"]["csv_header"] is False
    else:
        assert job.request["settings"]["theme"] == "source"
    if format == "xlsx":
        assert job.request["settings"]["xlsx_sheets"] == ["Summary", "#2"]
    response = client.put(
        url + "/source", content=b"one,two\n1,2\n", headers={"Content-Type": media_type}
    )
    assert response.status_code == 202
    assert job.state == "queued"
    assert client.post(url + "/cancel").status_code == 200


def test_source_theme_server_settings_import_fonts_unless_explicit():
    source = JobRequest.model_validate(
        {"source": {"kind": "upload", "format": "docx"}, "settings": {"theme": "source"}}
    ).settings.settings(JobLimits())
    assert source.source_theme is True and source.source_fonts is True
    assert source.theme.name == "bristol"
    explicit = JobRequest.model_validate(
        {
            "source": {"kind": "upload", "format": "docx"},
            "settings": {"theme": "source", "font_set": "arial"},
        }
    ).settings.settings(JobLimits())
    assert explicit.source_theme is True and explicit.source_fonts is False
    assert explicit.fonts.name == "arial"


def test_persisted_server_settings_preserve_defaults_and_source_font_intent(api):
    client, store, _credentials = api
    # Three unstarted jobs from one principal need more than the default store.
    store.limits = replace(store.limits, store_bytes=4 * (store.limits.scratch_bytes + MIB))
    # Keep the real enqueue transition; pause both immediate and periodic dispatch.
    store.schedule = lambda: None
    new_job(client, settings={})
    ordinary = list(store.jobs.values())[-1]
    assert ordinary.request["settings"] == {}
    restored = ExportOptions.model_validate(ordinary.request["settings"]).settings(JobLimits())
    assert restored.fonts.name == "sora" and restored.source_fonts is False

    new_job(client, format="docx", settings={"theme": "source"})
    imported = list(store.jobs.values())[-1]
    assert "font_set" not in imported.request["settings"]
    restored = ExportOptions.model_validate(imported.request["settings"]).settings(JobLimits())
    assert restored.source_fonts is True

    new_job(client, format="docx", settings={"theme": "source", "font_set": "arial"})
    explicit = list(store.jobs.values())[-1]
    assert explicit.request["settings"]["font_set"] == "arial"
    restored = ExportOptions.model_validate(explicit.request["settings"]).settings(JobLimits())
    assert restored.fonts.name == "arial" and restored.source_fonts is False


def test_csv_upload_rejects_markdown_media_type(api):
    client, _store, _credentials = api
    url = new_job(client, format="csv")
    response = client.put(
        url + "/source", content=b"A,B\n1,2\n", headers={"Content-Type": "text/markdown"}
    )
    assert response.status_code == 415
    assert "text/csv" in response.json()["error"]["message"]


def test_admission_retention_events_and_storage_capacity(api):
    client, store, _credentials = api
    store.limits = replace(store.limits, owner_jobs=1)
    url = new_job(client)
    assert (
        client.post(
            "/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}}
        ).status_code
        == 429
    )
    client.post(url + "/cancel")
    response = client.get(url + "/events").json()
    assert [e["state"] for e in response["events"]] == ["awaiting_upload", "cancelled"]
    assert client.get(url + "/events?after=2").json()["events"] == []
    assert client.get(url + "/events?after=9999999999999999999").status_code == 422
    job = next(iter(store.jobs.values()))
    job.finished_at = time.time() - store.limits.retention_seconds - 1
    store.expire()
    assert client.get(url).status_code == 404
    assert not store.path(job).exists()
    store.limits = replace(store.limits, store_bytes=1)
    assert (
        client.post(
            "/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}}
        ).status_code
        == 429
    )


def test_store_lock_restart_and_no_automatic_reexecution(tmp_path):
    root = tmp_path / "jobs"
    store = JobStore(root, JobLimits(), "", {})
    store.open()
    request = JobRequest.model_validate({"source": {"kind": "upload", "format": "markdown"}})
    job = store.admit("alice", request)
    with pytest.raises(ValueError, match="already in use"):
        JobStore(root, JobLimits(), "", {}).open()
    os.close(store.lock_fd)
    resumed = JobStore(root, JobLimits(), "", {})
    resumed.open()
    try:
        assert resumed.jobs[job.id].state == "interrupted"
        assert resumed.jobs[job.id].error == "server_restarted"
        assert not (root / job.id / "work").exists()
        assert not resumed.tasks
    finally:
        os.close(resumed.lock_fd)


def test_mount_ownership_and_paths(tmp_path):
    tokens = {"alice": secrets.token_urlsafe(32), "bob": secrets.token_urlsafe(32)}
    app = create_app(
        tmp_path / "jobs",
        tokens,
        (ORIGIN,),
        browser=sys.executable,
        mounts={
            "manuals": Mount(tmp_path, frozenset({"alice"})),
        },
    )
    with TestClient(app, base_url="https://testserver") as client:
        client.headers["Authorization"] = "Bearer " + tokens["bob"]
        body = {"source": {"kind": "mount", "mount": "manuals", "path": ""}}
        assert client.post("/api/v1/jobs", json=body).status_code == 404
        assert client.get("/api/v1/capabilities").json()["mounts"] == []
        client.headers["Authorization"] = "Bearer " + tokens["alice"]
        for path in ("..", "/etc/passwd", "child/../../secret", "C:\\secret"):
            body["source"]["path"] = path
            assert client.post("/api/v1/jobs", json=body).status_code == 422


def test_credential_configuration_fails_closed_without_echoing():
    for tokens in ({}, {"owner": "short"}, {"owner": "x" * 32, "second": "x" * 32}):
        with pytest.raises(ValueError):
            token_hashes(tokens)


def test_missing_renderer_is_not_ready_and_cannot_accept_jobs(tmp_path):
    credential = secrets.token_urlsafe(32)
    app = create_app(tmp_path / "jobs", {"alice": credential}, (ORIGIN,), browser="")
    with TestClient(app, base_url="https://testserver") as client:
        client.headers["Authorization"] = "Bearer " + credential
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        assert not client.get("/api/v1/capabilities").json()["pdf_engine"]["discovered"]
        response = client.post(
            "/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}}
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "renderer_unavailable"
        assert not app.state.jobs.jobs


def test_admission_disk_failure_removes_partial_job_and_marks_not_ready(api, monkeypatch):
    from platefold import server_jobs

    client, store, _credentials = api

    def full(*_args):
        raise OSError(errno.ENOSPC, "synthetic disk full")

    monkeypatch.setattr(server_jobs, "atomic_json", full)
    response = client.post(
        "/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}}
    )
    assert response.status_code == 507
    assert not store.jobs
    assert list(store.root.iterdir()) == [store.root / ".lock"]
    assert client.get("/health/ready").status_code == 503


def test_failed_cleanup_stops_admission_instead_of_freeing_quota(api, monkeypatch):
    from platefold import server_jobs

    client, store, _credentials = api
    url = new_job(client)

    def denied(*_args, **_kwargs):
        raise OSError(errno.EACCES, "synthetic cleanup failure")

    monkeypatch.setattr(server_jobs.shutil, "rmtree", denied)
    assert client.post(url + "/cancel").json()["state"] == "cancelled"
    assert not store.storage_healthy
    assert client.get("/health/ready").status_code == 503
    assert (
        client.post(
            "/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}}
        ).status_code
        == 503
    )


@pytest.mark.parametrize("failure", ["disconnect", "timeout"])
def test_download_releases_descriptor_and_retention_pin(tmp_path, failure):
    from platefold.server import FileDownload

    store = JobStore(tmp_path / "jobs", replace(JobLimits(), download_seconds=1), "", {})
    store.open()
    try:
        job = store.admit(
            "alice", JobRequest.model_validate({"source": {"kind": "upload", "format": "markdown"}})
        )
        store.transition(job, "completed")
        path = store.path(job) / "result.pdf"
        path.write_bytes(b"%PDF synthetic response fixture")
        fd = os.open(path, os.O_RDONLY)
        response = FileDownload(fd, store, job.id)
        job.finished_at = time.time() - store.limits.retention_seconds - 1
        store.expire()
        assert job.id in store.jobs

        async def scenario():
            async def receive():
                return {"type": "http.disconnect"}

            async def send(message):
                if failure == "disconnect":
                    raise OSError("synthetic disconnected reader")
                await asyncio.sleep(3)

            expected = ClientDisconnect if failure == "disconnect" else TimeoutError
            with pytest.raises(expected):
                await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)

        asyncio.run(scenario())
        with pytest.raises(OSError):
            os.fstat(fd)
        assert not store.downloads
        store.expire()
        assert job.id not in store.jobs
        assert not store.path(job).exists()
    finally:
        os.close(store.lock_fd)


@pytest.mark.integration
def test_real_zip_export_events_pdf_download_and_restart(api):
    client, store, credentials = api
    fixture = Path(__file__).parent / "fixtures/wide_diagram.md"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("nested/wide.md", fixture.read_bytes())
        archive.writestr("another.md", "# API proof\n\nA second independent document.\n")
    url = new_job(client, format="zip")
    assert (
        client.put(
            url + "/source", content=buffer.getvalue(), headers={"Content-Type": "application/zip"}
        ).status_code
        == 202
    )
    state = wait_job(client, url)
    assert state["state"] == "completed_with_warnings", state
    manifest = client.get(url + "/results").json()["results"]
    assert len(manifest) == 3
    progress = client.get(url + "/events").json()["events"]
    assert any(e["event"] == "progress" and e["done"] == 2 for e in progress)
    for record in manifest:
        response = client.get(record["url"])
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["content-disposition"].startswith("attachment;")
        assert len(response.content) == record["bytes"]
        assert hashlib.sha256(response.content).hexdigest() == record["sha256"]
        if evidence_dir := os.environ.get("PLATEFOLD_API_EVIDENCE_DIR"):
            destination = Path(evidence_dir)
            destination.mkdir(parents=True, exist_ok=True)
            (destination / Path(record["name"]).name).write_bytes(response.content)
            (destination / "receipt.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "source_commit": os.environ.get("CI_COMMIT_SHA"),
                        "job": state,
                        "manifest": manifest,
                        "events": progress,
                    },
                    indent=2,
                )
            )
        if record["media_type"] == "application/pdf":
            pdf = PdfReader(io.BytesIO(response.content))
            assert len(pdf.pages) >= 1
            assert any(p.extract_text().strip() for p in pdf.pages)
        else:
            assert response.json()["status"] == "completed_with_warnings"
            assert all(d["warnings"] >= 1 for d in response.json()["documents"])
            assert str(store.root) not in response.text
    job = store.get("alice", state["id"])
    assert not (store.path(job) / "work").exists()
    # CI renders as an unprivileged user with Chromium's sandbox on; the report
    # comes from the renderer processes the guardian observed, not a constant.
    engine = client.get("/api/v1/capabilities").json()["pdf_engine"]
    assert engine["sandbox"] == "enabled" and engine["sandbox_disabled"] is False
    client.headers["Authorization"] = "Bearer " + credentials["bob"]
    assert client.get(manifest[0]["url"]).status_code == 404


@pytest.mark.integration
def test_bad_archive_is_failed_without_result_or_scratch(api):
    client, store, _credentials = api
    url = new_job(client, format="zip")
    client.put(url + "/source", content=b"not a zip", headers={"Content-Type": "application/zip"})
    state = wait_job(client, url)
    assert state["state"] == "failed"
    assert state["error"]["code"] == "invalid_source"
    assert not (store.root / state["id"] / "work").exists()
    assert client.get(url + "/results").status_code == 409


@pytest.mark.integration
def test_api_blocks_external_and_outside_source_resources(api, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    requests = []

    class Sink(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"PRIVATE NETWORK CONTENT")

        def log_message(self, *_):
            pass

    sink = ThreadingHTTPServer(("127.0.0.1", 0), Sink)
    thread = threading.Thread(target=sink.serve_forever)
    thread.start()
    outside = tmp_path / "private.svg"
    outside.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg"><text>PRIVATE LOCAL CONTENT</text></svg>'
    )
    target = f"http://127.0.0.1:{sink.server_port}/sentinel"
    document = (
        f"# Confined export\n\n![Outside]({outside.as_uri()})\n\n![Remote]({target})\n\n"
        f'<img src="{target}"><style>p {{ background-image: url({target}); }}</style>'
        f'<script>fetch("{target}")</script>\n\nAllowed document text.\n'
    )
    try:
        client, _store, _credentials = api
        url = new_job(client)
        client.put(
            url + "/source", content=document.encode(), headers={"Content-Type": "text/markdown"}
        )
        state = wait_job(client, url)
        assert state["state"] == "completed_with_warnings", state
        manifest = client.get(url + "/results").json()["results"]
        for record in manifest:
            payload = client.get(record["url"]).content
            if record["media_type"] == "application/pdf":
                text = " ".join(p.extract_text() for p in PdfReader(io.BytesIO(payload)).pages)
                assert "Allowed document text" in text
                assert "PRIVATE" not in text
        assert requests == []
    finally:
        sink.shutdown()
        sink.server_close()
        thread.join(timeout=3)


@pytest.mark.integration
def test_completed_result_survives_restart_and_owner_token_rotation(tmp_path):
    first = secrets.token_urlsafe(32)
    second = secrets.token_urlsafe(32)
    root = tmp_path / "jobs"
    app = create_app(root, {"alice": first}, (ORIGIN,))
    with TestClient(app, base_url="https://testserver") as client:
        client.headers["Authorization"] = "Bearer " + first
        url = new_job(client)
        client.put(
            url + "/source",
            content=b"# Persistent result\n\nLanguage: en\n",
            headers={"Content-Type": "text/markdown"},
        )
        assert wait_job(client, url)["state"] == "completed"
        record = client.get(url + "/results").json()["results"][0]
        payload = client.get(record["url"]).content
    resumed = create_app(root, {"alice": second}, (ORIGIN,))
    with TestClient(resumed, base_url="https://testserver") as client:
        client.headers["Authorization"] = "Bearer " + first
        assert client.get(record["url"]).status_code == 401
        client.headers["Authorization"] = "Bearer " + second
        assert client.get(url).json()["state"] == "completed"
        assert client.get(record["url"]).content == payload


@pytest.mark.parametrize(
    "format,media_type",
    [
        ("doc", "application/msword"),
        ("odt", "application/vnd.oasis.opendocument.text"),
        ("rtf", "application/rtf"),
    ],
)
def test_legacy_uploads_are_refused_unless_the_operator_enables_conversion(api, format, media_type):
    client, store, _credentials = api
    store.schedule = lambda: None
    refused = client.post("/api/v1/jobs", json={"source": {"kind": "upload", "format": format}})
    assert refused.status_code == 422
    assert refused.json()["error"]["code"] == "legacy_conversion_disabled"
    assert not store.jobs

    store.limits = replace(store.limits, legacy_conversion=True)
    url = new_job(client, format=format)
    job = next(iter(store.jobs.values()))
    response = client.put(url + "/source", content=b"legacy", headers={"Content-Type": media_type})
    assert response.status_code == 202
    assert job.state == "queued"
    stored = JobRequest.model_validate(job.request)
    assert stored.settings.settings(store.limits).external_converters is True
    assert client.post(url + "/cancel").status_code == 200


def test_legacy_conversion_limit_is_a_boolean_and_off_by_default():
    assert JobLimits().legacy_conversion is False
    assert JobLimits(legacy_conversion=True).legacy_conversion is True
    with pytest.raises(ValueError):
        JobLimits(legacy_conversion=1)
    server_settings = JobRequest.model_validate(
        {"source": {"kind": "upload", "format": "docx"}}
    ).settings.settings(JobLimits())
    assert server_settings.external_converters is False


def test_capability_catalog_is_built_once_off_the_event_loop(api, monkeypatch):
    import threading

    from platefold import server

    client, _store, _credentials = api
    builds = []
    real = server.static_capabilities

    def counted():
        builds.append(threading.current_thread() is threading.main_thread())
        return real()

    monkeypatch.setattr(server, "static_capabilities", counted)
    first = client.get("/api/v1/capabilities")
    second = client.get("/api/v1/capabilities")

    assert first.status_code == second.status_code == 200
    assert first.json()["themes"] == second.json()["themes"]
    assert builds == [False]


def test_one_principal_cannot_hold_every_scratch_reservation(api):
    client, store, credentials = api
    store.schedule = lambda: None
    # Default limits leave room for three active jobs.
    assert store.owner_active_limit() == 2
    new_job(client)
    new_job(client)
    third = client.post("/api/v1/jobs", json={"source": {"kind": "upload", "format": "markdown"}})
    assert third.status_code == 429
    other = client.post(
        "/api/v1/jobs",
        json={"source": {"kind": "upload", "format": "markdown"}},
        headers={"Authorization": f"Bearer {credentials['bob']}"},
    )
    assert other.status_code == 201, other.text


def test_cancelled_jobs_do_not_count_toward_the_owner_job_limit(api):
    client, store, _credentials = api
    store.schedule = lambda: None
    for _ in range(store.limits.owner_jobs + 1):
        url = new_job(client)
        assert client.post(url + "/cancel").status_code == 200
    assert len(store.jobs) == store.limits.owner_jobs + 1
