import json
from types import SimpleNamespace

import pytest

from platefold import browser, browser_qualification
from platefold.cdp import CancelledError
from platefold.limits import ResourceLimitError


class FakeBrowser:
    version = "Chrome/151.0.0.0"

    def __init__(self, path="test"):
        self.path = path
        self.closed = False
        self.callback = None

    def close(self):
        self.closed = True

    def set_cancel_callback(self, callback):
        self.callback = callback


def test_default_still_prefers_bundled_without_reuse_probe(monkeypatch):
    monkeypatch.setattr(browser, "Browser", FakeBrowser)
    monkeypatch.setattr(browser, "find_bundled", lambda: "/bundled")
    monkeypatch.setattr(browser, "find_system", lambda _: pytest.fail("system discovery"))
    instance, label = browser.launch()
    assert instance.path == "/bundled"
    assert "bundled Chromium" in label


def test_system_tries_next_engine_after_failed_capability_check(monkeypatch):
    calls = []
    accepted = FakeBrowser()
    monkeypatch.setattr(browser.sys, "platform", "win32")
    monkeypatch.setattr(browser, "find_system", lambda name: f"/{name}")

    def checked(path, label, cancel_check=None):
        calls.append(path)
        if path == "/edge":
            raise RuntimeError("No tagged PDF")
        return accepted, label

    monkeypatch.setattr(browser, "_qualified", checked)
    instance, label = browser.launch("system")
    assert calls == ["/edge", "/chrome"]
    assert instance is accepted
    assert label == "qualified chrome"


def test_system_fallback_also_checks_bundled_and_retains_failure(monkeypatch):
    calls = []
    fallback = FakeBrowser()
    monkeypatch.setattr(browser, "find_system", lambda name: "/edge" if name == "edge" else None)
    monkeypatch.setattr(browser, "find_bundled", lambda: "/bundled")

    def checked(path, label, cancel_check=None):
        calls.append(path)
        if path == "/edge":
            raise RuntimeError("Unsupported CSS page size")
        return fallback, label

    monkeypatch.setattr(browser, "_qualified", checked)
    instance, label = browser.launch("system")
    assert calls == ["/edge", "/bundled"]
    assert instance is fallback
    assert "installed engine unavailable" in label
    assert instance.system_browser_failures == ["edge: Unsupported CSS page size"]


def test_explicit_failed_browser_does_not_silently_use_different_engine(monkeypatch):
    monkeypatch.setattr(browser, "find_system", lambda _: "/edge")
    monkeypatch.setattr(
        browser, "_qualified", lambda *_: (_ for _ in ()).throw(RuntimeError("old"))
    )
    with pytest.raises(RuntimeError, match="Use the bundled browser"):
        browser.launch("edge")


def test_failed_probe_stops_browser_without_waiting_for_more_cdp_responses(monkeypatch):
    instance = FakeBrowser()
    monkeypatch.setattr(browser, "Browser", lambda _: instance)

    def fail(_browser, **kwargs):
        raise RuntimeError("failed qualification")

    monkeypatch.setattr(browser_qualification, "qualify", fail)
    with pytest.raises(RuntimeError, match="failed qualification"):
        browser._qualified("/engine", "engine")
    assert instance.closed
    assert instance.callback()


class FakePage:
    def __init__(self, instance):
        self.instance = instance
        self.closed = False
        self.fonts = [{"familyName": "Arial", "glyphCount": 20, "isCustomFont": False}]
        self.cancels = True

    def emulate_media(self, _media):
        pass

    def set_content(self, _html):
        pass

    def _send(self, method, _params=None):
        return {
            "DOM.getDocument": {"root": {"nodeId": 1}},
            "DOM.querySelector": {"nodeId": 2},
            "CSS.getPlatformFontsForNode": {"fonts": self.fonts},
        }.get(method, {})

    def pdf(self, **kwargs):
        assert kwargs["prefer_css_page_size"] is True
        assert kwargs["max_bytes"] == 2_000_000
        return b"fake"

    def evaluate(self, _expression):
        assert self.instance.callback is not None
        if self.cancels:
            raise browser_qualification.ProbeCancelled("cancelled")
        return True

    def close(self):
        self.closed = True


@pytest.fixture
def smoke(monkeypatch):
    instance = FakeBrowser()
    page = FakePage(instance)
    instance.new_page = lambda: page
    reader = SimpleNamespace(
        pages=[
            SimpleNamespace(
                mediabox=SimpleNamespace(width=283, height=397),
                extract_text=lambda: "PlateFold browser\ncheck",
            ),
            SimpleNamespace(mediabox=SimpleNamespace(width=510, height=283)),
        ],
        trailer={
            "/Root": {
                "/StructTreeRoot": {"/Type": "/StructTreeRoot"},
                "/MarkInfo": {"/Marked": True},
            }
        },
    )
    monkeypatch.setattr(browser_qualification, "PdfReader", lambda _: reader)
    return instance, page, reader


def test_smoke_returns_actual_font_and_page_evidence(smoke):
    instance, page, _ = smoke
    receipt = browser_qualification.qualify(instance)
    assert receipt.platform_fonts == ["Arial"]
    assert receipt.tagged_pdf
    assert receipt.browser_version == instance.version
    assert page.closed
    assert instance.callback is None


@pytest.mark.parametrize("missing", ["font", "tags", "size", "cancellation"])
def test_smoke_rejects_missing_capability_and_cleans_up(smoke, missing):
    instance, page, reader = smoke
    if missing == "font":
        page.fonts = []
    elif missing == "tags":
        reader.trailer["/Root"].pop("/StructTreeRoot")
    elif missing == "size":
        reader.pages[1].mediabox.width = 283
    else:
        page.cancels = False
    with pytest.raises(RuntimeError):
        browser_qualification.qualify(instance)
    assert page.closed
    assert instance.callback is None


@pytest.mark.parametrize("error_type", [CancelledError, ResourceLimitError])
def test_caller_cancellation_does_not_try_next_browser(monkeypatch, error_type):
    attempted = []
    monkeypatch.setattr(browser, "find_system", lambda name: f"/{name}")

    def abort(path, label, cancel_check=None):
        attempted.append(path)
        raise error_type("caller stopped")

    monkeypatch.setattr(browser, "_qualified", abort)
    with pytest.raises(error_type, match="caller stopped"):
        browser.launch("system")
    assert len(attempted) == 1


def test_caller_cancel_during_synthetic_probe_is_not_qualification_success(smoke):
    instance, page, _ = smoke

    def real_cancel(_expression):
        raise CancelledError("user cancelled")

    page.evaluate = real_cancel
    with pytest.raises(CancelledError, match="user cancelled"):
        browser_qualification.qualify(instance)
    assert page.closed
    assert instance.callback is None


def test_caller_cancel_before_probe_starts(smoke):
    instance, page, _ = smoke

    def cancel_check():
        raise CancelledError("cancel before launch")

    with pytest.raises(CancelledError, match="cancel before launch"):
        browser_qualification.qualify(instance, cancel_check=cancel_check)
    assert instance.callback is None
    assert not page.closed  # No page was created.


def test_qualification_cleanup_cannot_mask_caller_error(smoke):
    instance, page, _ = smoke

    def real_cancel(_expression):
        raise CancelledError("user cancelled")

    def broken_cleanup():
        assert instance.callback()
        raise RuntimeError("cleanup error")

    page.evaluate = real_cancel
    page.close = broken_cleanup
    with pytest.raises(CancelledError, match="user cancelled"):
        browser_qualification.qualify(instance)
    assert instance.callback is None


def test_system_cli_records_qualified_fallback_and_rejected_installed_engine(monkeypatch, tmp_path):
    selected = FakeBrowser()
    selected.qualification_receipt = {"browser_version": "HeadlessChrome/151", "tagged_pdf": True}
    selected.system_browser_failures = ["edge: The browser exited immediately (code 1002)"]
    calls = []

    def launch(choice):
        calls.append(choice)
        return selected, "bundled Chromium (installed engine unavailable) (HeadlessChrome/151)"

    monkeypatch.setattr(browser, "launch", launch)
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(
        "sys.argv", ["qualification", "--browser", "system", "--output", str(output)]
    )
    assert browser_qualification.main() == 0
    receipt = json.loads(output.read_text())
    assert calls == ["system"]
    assert receipt["status"] == "passed"
    assert receipt["requested_browser"] == "system"
    assert receipt["selected_engine"].startswith("bundled Chromium")
    assert receipt["system_browser_failures"] == selected.system_browser_failures
    assert receipt["tagged_pdf"] is True
    assert selected.closed


def test_strict_browser_cli_records_startup_failure_without_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(browser, "find_system", lambda _: "/edge")
    monkeypatch.setattr(browser, "launch", lambda *_: pytest.fail("unexpected system fallback"))

    def failed_browser(_path):
        raise RuntimeError("The browser exited immediately (code 1002)")

    monkeypatch.setattr(browser_qualification, "Browser", failed_browser)
    output = tmp_path / "receipt.json"
    monkeypatch.setattr("sys.argv", ["qualification", "--browser", "edge", "--output", str(output)])
    assert browser_qualification.main() == 1
    receipt = json.loads(output.read_text())
    assert receipt["status"] == "failed"
    assert receipt["requested_browser"] == "edge"
    assert "1002" in receipt["error"]
    assert "selected_engine" not in receipt


def test_system_cli_fails_when_no_engine_qualifies(monkeypatch, tmp_path):
    def fail(_choice):
        raise RuntimeError("No browser passed the rendering check")

    monkeypatch.setattr(browser, "launch", fail)
    output = tmp_path / "receipt.json"
    monkeypatch.setattr(
        "sys.argv", ["qualification", "--browser", "system", "--output", str(output)]
    )
    assert browser_qualification.main() == 1
    receipt = json.loads(output.read_text())
    assert receipt["status"] == "failed"
    assert "No browser passed" in receipt["error"]
