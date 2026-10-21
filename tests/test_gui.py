"""Exercise cooperative Tk shutdown without requiring a display for unit checks."""

import inspect
import threading
from types import SimpleNamespace

import pytest

pytest.importorskip("tkinter")
from platefold.gui import App
from platefold.i18n import translator


def test_legacy_gui_describes_all_supported_documents():
    source = inspect.getsource(App)
    assert translator("en").text("gui.supported_documents") == "Supported documents"
    assert ".md files" not in source and "Markdown folder" not in source


def closing_app(worker, cancelled):
    callbacks, destroyed, states, statuses = [], [], [], []
    button = SimpleNamespace(config=lambda **values: states.append(values["state"]))
    app = SimpleNamespace(
        worker=worker,
        i18n=translator("en"),
        _cancel=cancelled,
        _closing=False,
        run_btn=button,
        cancel_btn=button,
        open_btn=button,
        _set_status=lambda *args: statuses.append(args[0]),
        after=lambda delay, callback: callbacks.append(callback),
        destroy=lambda: destroyed.append(True),
    )
    app._finish_close = lambda: App._finish_close(app)
    return app, callbacks, destroyed, states, statuses


def test_close_keeps_event_loop_alive_until_worker_cleanup_finishes(monkeypatch):
    cancelled, cleanup_started, finish_cleanup, cleaned = (threading.Event() for _ in range(4))

    def export_worker():
        assert cancelled.wait(5)
        cleanup_started.set()
        assert finish_cleanup.wait(5)
        cleaned.set()

    worker = threading.Thread(target=export_worker)
    worker.start()
    app, callbacks, destroyed, states, statuses = closing_app(worker, cancelled)
    monkeypatch.setattr("platefold.gui.messagebox.askokcancel", lambda *args: True)
    try:
        App._on_close(app)
        assert cleanup_started.wait(5)
        assert app._closing and cancelled.is_set() and not destroyed
        assert states == ["disabled", "disabled", "disabled"]
        assert "closing" in statuses[-1]
        # A scheduled poll returns while cleanup is still running. It neither
        # blocks Tk nor destroys the window or permits a replacement export.
        callbacks.pop(0)()
        assert callbacks and not destroyed and worker.is_alive()
        App.start(app)
        App._handle_done(app, 2)
        App._handle_progress(app, 0, 1, "notes.md", None)
        App._on_close(app)
        assert app.worker is worker and len(callbacks) == 1
        assert states == ["disabled", "disabled", "disabled"]
        finish_cleanup.set()
        worker.join(timeout=5)
        assert not worker.is_alive() and cleaned.is_set()
        callbacks.pop(0)()
        assert destroyed == [True]
    finally:
        cancelled.set()
        finish_cleanup.set()
        worker.join(timeout=5)


def test_declined_close_leaves_running_export_unchanged(monkeypatch):
    app, callbacks, destroyed, states, _ = closing_app(
        SimpleNamespace(is_alive=lambda: True), threading.Event()
    )
    monkeypatch.setattr("platefold.gui.messagebox.askokcancel", lambda *args: False)
    App._on_close(app)
    assert not app._closing and not app._cancel.is_set()
    assert not callbacks and not destroyed and not states


def test_idle_close_destroys_without_waiting():
    app, callbacks, destroyed, _, _ = closing_app(None, threading.Event())
    App._on_close(app)
    assert app._closing and destroyed == [True] and not callbacks


def test_input_changes_preserve_explicit_output_and_default_to_downloads(monkeypatch):
    from platefold import gui

    class Value:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    app = SimpleNamespace(src=Value(), dst=Value(), i18n=translator("en"))
    monkeypatch.setattr(gui, "downloads_dir", lambda: "/os-user/Redirected Downloads")
    monkeypatch.setattr(gui.filedialog, "askdirectory", lambda **_: "/documents/one")
    monkeypatch.setattr(gui.filedialog, "askopenfilename", lambda **_: "/documents/two.docx")
    App.pick_src(app)
    assert app.dst.get() == "/os-user/Redirected Downloads"
    app.dst.set("/chosen/export")
    App.pick_file(app)
    assert app.src.get() == "/documents/two.docx"
    assert app.dst.get() == "/chosen/export"
    assert App._default_output("/unrelated/input") == "/os-user/Redirected Downloads"
