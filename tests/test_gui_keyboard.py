"""Keyboard and label checks for the legacy Tk window.

Tk 9.0 exposes nothing to screen readers, so these checks cover what a
keyboard user sees and can reach. They need a display; on Linux without one
they start a private Xvfb server when it is installed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest

tk = pytest.importorskip("tkinter")
from tkinter import ttk  # noqa: E402

from platefold.contrast import contrast_ratio  # noqa: E402


@pytest.fixture(scope="module")
def display(missing_prerequisite):
    if sys.platform != "linux" or os.environ.get("DISPLAY"):
        yield
        return
    if not shutil.which("Xvfb"):
        missing_prerequisite("No display and no Xvfb")
    read, write = os.pipe()
    server = subprocess.Popen(
        ["Xvfb", "-displayfd", str(write), "-screen", "0", "1280x1024x24", "-nolisten", "tcp"],
        pass_fds=(write,),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.close(write)
    with os.fdopen(read) as stream:
        number = stream.readline().strip()
    os.environ["DISPLAY"] = f":{number}"
    try:
        yield
    finally:
        del os.environ["DISPLAY"]
        server.terminate()
        server.wait(timeout=10)


@pytest.fixture
def app(display, monkeypatch, tmp_path):
    from platefold import gui

    monkeypatch.setattr(gui, "downloads_dir", lambda: str(tmp_path))
    window = gui.App()
    window.update()
    # Give the window focus the way a window manager would; Tk then restores
    # the widget the app chose.
    window.focus_lastfor().focus_force()
    window.update()
    yield window
    window._closing = True
    for pending in window.tk.splitlist(window.tk.call("after", "info")):
        window.after_cancel(pending)
    window.destroy()


def focused_widget(app):
    """The focused widget, or skip when this session has no interactive desktop."""
    widget = app.focus_get()
    if widget is None or widget is app:
        pytest.skip("Tk focus is unavailable in this non-interactive session")
    return widget


def press(app, key: str) -> None:
    focused_widget(app).event_generate(key, when="tail")
    app.update()


def tab_order(app, limit: int = 60) -> list:
    app.src_entry.focus_set()
    app.update()
    order = [focused_widget(app)]
    for _ in range(limit):
        press(app, "<Tab>")
        widget = focused_widget(app)
        if widget is order[0]:
            return order
        order.append(widget)
    pytest.fail(f"Tab did not cycle: {[str(w) for w in order]}")


def interactive(app) -> list:
    found, pending = [], [app]
    while pending:
        widget = pending.pop()
        pending.extend(widget.winfo_children())
        if isinstance(widget, ttk.Button | ttk.Entry | ttk.Checkbutton | tk.Text):
            found.append(widget)
    return found


def test_tab_reaches_every_enabled_control_and_cycles(app):
    order = tab_order(app)
    enabled = [
        widget
        for widget in interactive(app)
        if not (isinstance(widget, ttk.Widget) and widget.instate(["disabled"]))
    ]
    assert set(enabled) <= set(order)
    assert app.log in order
    # Shift+Tab walks the same ring backwards.
    app.src_entry.focus_set()
    app.update()
    press(app, "<Shift-Tab>")
    assert focused_widget(app) is order[-1]


def test_initial_focus_is_the_source_field(app):
    assert focused_widget(app) is app.src_entry


def test_log_is_read_only_and_tab_leaves_it(app):
    app._append_log("Exported notes.md\n")
    app.log.focus_set()
    app.update()
    press(app, "<Key-x>")
    press(app, "<Tab>")
    assert app.log.get("1.0", "end-1c") == "Exported notes.md\n"
    assert focused_widget(app) is not app.log
    app.log.focus_set()
    app.update()
    press(app, "<Shift-Tab>")
    assert focused_widget(app) is not app.log


def test_enter_and_space_activate_the_focused_button(app, monkeypatch):
    from platefold import gui

    warnings = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *args: warnings.append(args))
    app.run_btn.focus_set()
    app.update()
    press(app, "<Return>")
    assert len(warnings) == 1
    press(app, "<KP_Enter>")
    assert len(warnings) == 2
    # A disabled button stays inert.
    app.open_btn.focus_set()
    app.update()
    press(app, "<Return>")
    assert len(warnings) == 2


def test_every_field_has_a_visible_label_and_every_button_has_text(app):
    for widget in interactive(app):
        if isinstance(widget, ttk.Button | ttk.Checkbutton):
            assert str(widget.cget("text")).strip(), widget
    fields = [
        widget
        for widget in interactive(app)
        if isinstance(widget, ttk.Entry) and widget.winfo_manager() == "grid"
    ]
    assert len(fields) >= 8
    for field in fields:
        row = field.grid_info()["row"]
        labels = [
            sibling
            for sibling in field.master.grid_slaves(row=row, column=0)
            if isinstance(sibling, ttk.Label) and str(sibling.cget("text")).strip()
        ]
        assert labels, f"{field} has no label in its row"


@pytest.mark.parametrize("state", ["idle", "ok", "warn", "error"])
def test_status_and_banner_colours_meet_text_contrast(app, state):
    from platefold import gui

    colour = {
        "idle": gui.IDLE_COLOUR,
        "ok": gui.OK_COLOUR,
        "warn": gui.WARN_COLOUR,
        "error": gui.ERROR_COLOUR,
    }[state]

    def hex_of(name: str) -> str:
        red, green, blue = (value // 257 for value in app.winfo_rgb(name))
        return f"#{red:02x}{green:02x}{blue:02x}"

    status_bg = ttk.Style(app).lookup("TLabel", "background") or app.cget("background")
    assert contrast_ratio(colour, hex_of(status_bg)) >= 4.5
    assert contrast_ratio(colour, hex_of(app.banner.cget("background"))) >= 4.5
    # Windows' default window colour, where ttk follows the system theme.
    assert contrast_ratio(colour, "#f0f0f0") >= 4.5
