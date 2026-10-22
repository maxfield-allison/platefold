"""The served browser journey uses the real authenticated API and PDF renderer."""

from __future__ import annotations

import hashlib
import io
import json
import os
import secrets
import shutil
import socket
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest
from pypdf import PdfReader
from starlette.testclient import TestClient

from platefold.server import create_app
from platefold.server_models import Mount
from platefold.server_web import web_assets

pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux server contract")
DIST = Path(__file__).resolve().parents[1] / "desktop" / "dist"


def test_public_assets_do_not_open_api_or_job_files(tmp_path):
    site = tmp_path / "site"
    (site / "assets").mkdir(parents=True)
    (site / "index.html").write_text("<!doctype html><title>PlateFold</title>")
    (site / "assets" / "app.js").write_text("document.title='PlateFold'")
    (site / "secret.txt").write_text("must not be served")
    app = create_app(
        tmp_path / "jobs",
        {"alice": secrets.token_urlsafe(32)},
        ("https://testserver",),
        browser=sys.executable,
        web_root=site,
    )
    with TestClient(app, base_url="https://testserver") as client:
        for path in ("/", "/assets/app.js"):
            response = client.get(path)
            assert response.status_code == 200
            assert "script-src 'self'" in response.headers["content-security-policy"]
            assert response.headers["cache-control"] == "no-store"
        for path in (
            "/api/v1/capabilities",
            "/api/v1/jobs/anything",
            "/secret.txt",
            "/assets/../secret.txt",
        ):
            response = client.get(path)
            assert response.status_code == 401
            assert response.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert client.get("/", headers={"Host": "evil.example"}).status_code == 400
        assert client.get("/", headers={"Origin": "https://evil.example"}).status_code == 403
        assert client.get("/?token=not-accepted").status_code == 400
        assert client.post("/").status_code == 401
        assert client.head("/").status_code == 200


def test_static_assets_reject_links_and_unexpected_types(tmp_path):
    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("hello")
    private = tmp_path / "private.txt"
    private.write_text("private")
    link = tmp_path / "assets" / "bad.js"
    link.symlink_to(private)
    with pytest.raises(ValueError, match="regular files"):
        web_assets(tmp_path)
    link.unlink()
    (tmp_path / "assets" / "bad.map").write_text("private source map")
    with pytest.raises(ValueError, match="Unexpected"):
        web_assets(tmp_path)


class BrowserSession:
    def __repr__(self):
        return "<browser test session; disposable credentials redacted>"

    def login(self, principal="alice"):
        from playwright.sync_api import expect

        self.page.locator("#api-credential").fill(self.credentials[principal])
        self.page.locator("#connect-server").click()
        expect(self.page.locator("#connection-note")).to_contain_text("Connected.")
        expect(self.page.locator("#api-credential")).to_have_value("")


@pytest.fixture
def web(tmp_path):
    import uvicorn
    from playwright.sync_api import sync_playwright

    if not (DIST / "index.html").is_file():
        pytest.fail("Build desktop/dist before running browser integration tests")
    session = BrowserSession()
    session.credentials = {name: secrets.token_urlsafe(32) for name in ("alice", "bob")}
    mounted = tmp_path / "mount"
    mounted.mkdir()
    (mounted / "guide.md").write_text(
        "# Mounted guide\n\nA mounted source exported through the browser.\n"
    )
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    session.origin = f"http://127.0.0.1:{port}"
    app = create_app(
        tmp_path / "jobs",
        session.credentials,
        (session.origin,),
        mounts={"manuals": Mount(mounted, frozenset({"alice"}))},
        web_root=DIST,
    )
    session.store = app.state.jobs
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started
    try:
        with sync_playwright() as playwright:
            from platefold.browser import find_bundled

            browser = playwright.chromium.launch(
                executable_path=find_bundled(), chromium_sandbox=True
            )
            session.page = browser.new_page(viewport={"width": 1440, "height": 1100})
            errors = []
            session.console_errors = []
            session.font_responses = []
            session.page.on("pageerror", lambda error: errors.append(str(error)))

            def record_console(message):
                if message.type == "error":
                    session.console_errors.append(message.text)

            def record_response(response):
                if response.request.resource_type == "font":
                    session.font_responses.append(
                        {
                            "url": response.url,
                            "status": response.status,
                            "csp": response.headers.get("content-security-policy", ""),
                        }
                    )

            session.page.on("console", record_console)
            session.page.on("response", record_response)
            session.page.goto(session.origin)
            yield session
            assert not errors
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        sock.close()
        assert not thread.is_alive()


def upload(page, data=b"# Browser document\n\nReadable text.\n", name="notes.md"):
    page.locator("#upload-file").set_input_files(
        {"name": name, "mimeType": "application/octet-stream", "buffer": data}
    )


def wait_result(page, *, warnings=True):
    from playwright.sync_api import expect

    expect(page.locator("#status")).to_contain_text(
        "Finished with warnings" if warnings else "Export complete", timeout=60000
    )
    page.locator("#view-results").click()


@pytest.mark.integration
def test_browser_fonts_load_from_the_same_origin_under_the_strict_csp(web):
    css = "\n".join(path.read_text() for path in (DIST / "assets").glob("*.css"))
    assert "data:font" not in css
    before = len(web.font_responses)
    loaded = web.page.evaluate(
        """async () => {
          const samples = ["\u0460", "\u0102"];
          const results = [];
          for (const sample of samples) {
            const faces = await document.fonts.load('400 16px "JetBrains Mono"', sample);
            results.push(faces.length > 0);
          }
          return results;
        }"""
    )
    assert loaded == [True, True]
    responses = web.font_responses[before:]
    assert len(responses) >= 2
    assert all(response["status"] == 200 for response in responses)
    assert all(response["url"].startswith(f"{web.origin}/assets/") for response in responses)
    assert all("font-src 'self'" in response["csp"] for response in responses)
    assert not any(response["url"].startswith("data:") for response in responses)
    assert not any(
        "content security policy" in message.lower() and "font" in message.lower()
        for message in web.console_errors
    )


@pytest.mark.integration
def test_browser_advanced_settings_reach_the_downloaded_pdf(web, tmp_path):
    from playwright.sync_api import expect

    page = web.page
    web.login()
    upload(page)
    page.locator("#document-language").fill("fr-CA")
    page.locator("#output-mode").select_option("print")
    page.locator("#advanced-layout summary").click()
    expect(page.locator("#advanced-image_dpi")).to_have_attribute("placeholder", "150")
    page.locator("#advanced-image_dpi").fill("12")
    expect(page.locator("#export")).to_be_disabled()
    page.locator("#advanced-image_dpi").fill("240")
    page.locator("#advanced-page_width").fill("230")
    page.locator("#advanced-page_height").fill("340")
    page.locator("#orientation").select_option("landscape")
    page.locator("#exact-margins").check()
    for edge, value in zip(["top", "right", "bottom", "left"], [3, 17, 21, 7], strict=True):
        page.locator(f"#margin-{edge}").fill(str(value))
    page.locator("#export").click()
    wait_result(page, warnings=False)
    with page.expect_download() as download:
        page.get_by_role("button", name="Download PDF for").click()
    path = tmp_path / "advanced.pdf"
    download.value.save_as(path)
    pdf = PdfReader(path)
    assert pdf.root_object["/Lang"] == "fr-CA"
    assert "/StructTreeRoot" in pdf.root_object
    for sheet in pdf.pages:
        assert float(sheet.mediabox.width) * 25.4 / 72 == pytest.approx(340, abs=0.5)
        assert float(sheet.mediabox.height) * 25.4 / 72 == pytest.approx(230, abs=0.5)
    with page.expect_download() as download:
        page.get_by_role("button", name="Download report", exact=True).click()
    settings = json.loads(Path(download.value.path()).read_text())["settings"]
    assert settings["page_margins_mm"] == [3, 17, 21, 7]
    assert settings["document_language"] == "fr-CA"
    assert settings["image_dpi"] == 240
    assert settings["exact_margins"]


@pytest.mark.integration
def test_browser_csv_upload_header_option_and_pdf_mapping(web, tmp_path):
    from playwright.sync_api import expect

    page = web.page
    web.login()
    upload(page, b"one,two,three\n1,2,3\n", "raw.csv")
    page.get_by_label("First CSV/TSV row is a header", exact=True).uncheck()
    page.locator("#export").click()
    wait_result(page)
    job = next(iter(web.store.jobs.values()))
    assert job.request["source"]["format"] == "csv"
    assert job.request["settings"]["csv_header"] is False
    assert any(item["name"] == "document.pdf" for item in job.results)
    with page.expect_download() as download:
        page.get_by_role("button", name="Download PDF for").click()
    path = tmp_path / "browser-csv.pdf"
    download.value.save_as(path)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    assert all(value in text for value in ("one", "two", "three", "1", "2", "3"))
    expect(page.get_by_role("button", name="Download PDF for")).to_have_count(1)


@pytest.mark.integration
def test_browser_docx_upload_uses_source_theme_and_reports_effective_style(web, tmp_path):
    from playwright.sync_api import expect

    from tests.test_docx_ingest import make_docx

    page = web.page
    web.login()
    source = make_docx(tmp_path / "browser.docx")
    upload(page, source.read_bytes(), source.name)
    page.locator("#choose-theme").click()
    page.get_by_role("button", name="source", exact=True).click()
    expect(page.locator("#theme-kind")).to_contain_text("SOURCE DOCUMENT")
    page.locator("#export").click()
    expect(page.locator("#status")).to_contain_text("Finished with warnings", timeout=60000)
    page.locator("#view-results").click()
    job = next(iter(web.store.jobs.values()))
    assert job.request["source"]["format"] == "docx"
    assert job.request["settings"]["theme"] == "source"
    with page.expect_download() as download:
        page.get_by_role("button", name="Download report", exact=True).click()
    report = json.loads(Path(download.value.path()).read_text())
    assert report["documents"][0]["theme"] == "source"
    assert report["documents"][0]["font_set"] == "source"
    with page.expect_download() as download:
        page.get_by_role("button", name="Download PDF for").click()
    path = tmp_path / "browser-docx.pdf"
    download.value.save_as(path)
    text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    assert "DOCX Proof" in text and "Semantic body text" in text


@pytest.mark.integration
def test_browser_upload_palette_download_and_reload_recovery(web, tmp_path):
    from playwright.sync_api import expect

    page = web.page
    expect(page.locator("#export")).to_be_disabled()
    expect(page.locator("#choose-folder")).not_to_be_visible()
    expect(page.get_by_role("button", name="Updates", exact=True)).to_have_count(0)
    web.login()
    expect(page.locator("#save-copy")).not_to_be_visible()
    expect(page.locator("#preview-status")).to_contain_text("Style sample only")
    page.locator("#preset").select_option("a4")
    page.locator("#palette-editor summary").click()
    page.get_by_label("ink hex", exact=True).fill("#123456")
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w") as bundle:
        bundle.writestr("first.md", "# First document\n\n```mermaid\nflowchart LR\n A --> B\n```\n")
        bundle.writestr("nested/second.md", "# Second document\n\nReadable text.\n")
    upload(page, data.getvalue(), "bundle.zip")
    page.locator("#export").click()
    wait_result(page)
    job = next(iter(web.store.jobs.values()))
    assert job.request["settings"]["preset"] == "a4"
    assert job.request["settings"]["colors"]["ink"] == "#123456"
    assert len(job.results) == 3
    retained = os.environ.get("PLATEFOLD_API_EVIDENCE_DIR")
    evidence = Path(retained) / "web" if retained else tmp_path / "evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    for index, button in enumerate(page.get_by_role("button", name="Download PDF for").all()):
        with page.expect_download() as download:
            button.click()
        path = tmp_path / f"result-{index}.pdf"
        download.value.save_as(path)
        pdf = PdfReader(path)
        assert len(pdf.pages) >= 1
        assert pdf.pages[0].extract_text().strip()
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert digest in {r["sha256"] for r in job.results}
        shutil.copyfile(path, evidence / path.name)
    with page.expect_download() as download:
        page.get_by_role("button", name="Download report", exact=True).click()
    report = json.loads(Path(download.value.path()).read_text())
    (evidence / "report.json").write_text(json.dumps(report, indent=2))
    (evidence / "manifest.json").write_text(json.dumps(job.results, indent=2))
    page.screenshot(path=str(evidence / "results.png"), full_page=True)
    assert len(report["documents"]) == 2
    assert "owner" not in report
    downloads = []
    page.on("download", lambda download: downloads.append(download))

    def corrupt_download(route):
        response = route.fetch()
        content = response.body()
        route.fulfill(response=response, body=b"!" + content[1:])

    artifact = next(item for item in job.results if item["media_type"] == "application/pdf")
    pattern = f"**/api/v1/jobs/{job.id}/results/{artifact['id']}"
    page.route(pattern, corrupt_download)
    page.get_by_role("button", name="Download PDF for").first.click()
    expect(page.locator("#error")).to_contain_text("checksum mismatch")
    assert not downloads
    page.unroute(pattern)
    assert page.evaluate("Object.keys(localStorage)") == []
    assert page.evaluate("Object.keys(sessionStorage)") == ["platefold.last-job"]
    page.reload()
    expect(page.locator("#export")).to_be_disabled()
    web.login()
    wait_result(page)
    expect(page.get_by_role("button", name="Download PDF for")).to_have_count(2)
    page.locator("#dismiss-results").click()
    page.locator("#disconnect-server").click()
    page.locator("#dismiss-results").click()
    web.login("bob")
    expect(page.locator("#error")).to_contain_text("unavailable to this credential")
    expect(page.locator("#mount-controls")).not_to_be_visible()
    expect(page.get_by_role("button", name="Download PDF for")).to_have_count(0)


@pytest.mark.integration
def test_browser_mount_invalid_bundle_retry_and_cancel(web):
    from playwright.sync_api import expect

    page = web.page
    web.login()
    page.locator("#mount-path").fill("../escape")
    page.locator("#use-mount").click()
    expect(page.locator("#error")).to_contain_text("relative")
    page.locator("#dismiss-results").click()
    page.locator("#mount-path").fill("guide.md")
    page.locator("#use-mount").click()
    page.locator("#export").click()
    wait_result(page)
    page.locator("#dismiss-results").click()
    upload(page, b"this is not a zip", "invalid.zip")
    page.locator("#export").click()
    expect(page.locator("#error")).to_contain_text("invalid_source", timeout=20000)
    page.locator("#dismiss-results").click()
    upload(page, b"# Retry\n\n" + b"```mermaid\nflowchart LR\n A --> B\n```\n" * 30)
    page.locator("#export").click()
    expect(page.locator("#status")).to_contain_text("Rendering", timeout=20000)
    page.locator("#cancel").click()
    expect(page.locator("#error")).to_contain_text("cancelled", timeout=20000)
    assert not list(web.store.jobs.values())[-1].results
    page.locator("#dismiss-results").click()
    upload(page)
    page.locator("#export").click()
    wait_result(page)


@pytest.mark.integration
def test_browser_connection_loss_and_small_screen_recovery(web):
    from playwright.sync_api import expect

    page = web.page
    page.set_viewport_size({"width": 600, "height": 850})
    page.locator("#api-credential").fill("invalid")
    page.locator("#connect-server").click()
    expect(page.locator("#connection-note")).to_contain_text("administrator")
    web.login()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    upload(page)
    page.route(
        "**/api/v1/jobs/*",
        lambda route: route.abort() if route.request.method == "GET" else route.continue_(),
    )
    page.locator("#export").click()
    expect(page.locator("#error")).to_contain_text("Connection interrupted", timeout=20000)
    expect(page.locator("#export")).to_be_disabled()
    page.unroute("**/api/v1/jobs/*")
    page.locator("#dismiss-results").click()
    web.login()
    wait_result(page)
    expect(page.get_by_role("button", name="Download PDF for")).to_have_count(1)
    # Credential-free URLs cannot download even when the same browser has signed in.
    job_id = page.evaluate("sessionStorage.getItem('platefold.last-job')")
    assert page.request.get(f"{web.origin}/api/v1/jobs/{job_id}/results/0").status == 401
    page.locator("#dismiss-results").click()
    page.locator("#preview-tab").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#preview-status")).to_contain_text("Style sample only")


def test_the_interface_declares_its_icon_so_browsers_request_none():
    # Without a declared icon every browser asks for /favicon.ico, which the
    # server refuses with 401 like any path outside the public assets, and
    # each page load then logs that refusal as a console error.
    page = (DIST.parent / "index.html").read_text(encoding="utf-8")
    assert '<link rel="icon" href="data:,">' in page
