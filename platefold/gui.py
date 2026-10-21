#!/usr/bin/env python3
"""Desktop front end for export.py.

Runs the same code path as the CLI on a worker thread so the window stays
responsive, and reports what happened rather than leaving the user to read a
log: a progress bar per document, a cancel button, a result banner that is
impossible to miss when images or diagrams failed, and a button that opens the
output folder in Explorer.

    python gui.py
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from contextlib import suppress
from tkinter import filedialog, messagebox, ttk

from platefold.i18n import translator
from platefold.paper import PAPER_SIZES, local_paper
from platefold.paths import downloads_dir
from platefold.theme import DEFAULT_FONT_SET, FONT_SETS, THEMES

# At least 4.5:1 on the default Tk, ttk and Windows window backgrounds.
OK_COLOUR = "#0f5c28"
WARN_COLOUR = "#6b4700"
ERROR_COLOUR = "#8c1d17"
IDLE_COLOUR = "#3d4c5c"


class Redirector:
    """Funnel stdout from the worker thread into the Tk log pane."""

    def __init__(self, sink: queue.Queue):
        self.sink = sink

    def write(self, text: str) -> None:
        if text:
            self.sink.put(("log", text))

    def flush(self) -> None:
        pass


def reveal(path: str) -> None:
    """Open a folder in the platform file browser."""
    if not os.path.isdir(path):
        return
    if os.name == "nt":
        os.startfile(path)
    elif sys.platform == "darwin":
        subprocess.run(["open", path], check=False)
    else:
        subprocess.run(["xdg-open", path], check=False)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.i18n = translator()
        self.preset_labels = {
            "auto": self.i18n.text("paper.auto", paper=local_paper().upper()),
            **{name: name.upper() for name in PAPER_SIZES},
            "uniform-letter": self.i18n.text("paper.uniform_letter"),
        }
        self.title("Plate|Fold")
        self.geometry("820x620")
        self.minsize(720, 520)

        self.messages: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._closing = False
        self._last_output: str | None = None
        self._totals = {"documents": 0, "pages": 0, "missing": 0, "errors": 0, "warnings": 0}

        self._build()
        self.after(100, self._drain)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- layout ------------------------------------------------------------
    def _build(self) -> None:
        paths = ttk.Frame(self)
        paths.pack(fill="x", padx=10, pady=6)

        ttk.Label(paths, text=self.i18n.text("gui.source")).grid(row=0, column=0, sticky="w")
        self.src = tk.StringVar()
        self.src_entry = ttk.Entry(paths, textvariable=self.src)
        self.src_entry.grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(paths, text=self.i18n.text("gui.file"), command=self.pick_file).grid(
            row=0, column=2
        )
        ttk.Button(paths, text=self.i18n.text("gui.folder"), command=self.pick_src).grid(
            row=0, column=3, padx=(4, 0)
        )

        ttk.Label(paths, text=self.i18n.text("gui.output")).grid(
            row=1, column=0, sticky="w", pady=(6, 0)
        )
        self.dst = tk.StringVar(value=downloads_dir() or "")
        ttk.Entry(paths, textvariable=self.dst).grid(
            row=1, column=1, sticky="ew", padx=6, pady=(6, 0)
        )
        ttk.Button(paths, text=self.i18n.text("gui.browse"), command=self.pick_dst).grid(
            row=1, column=2, pady=(6, 0)
        )
        paths.columnconfigure(1, weight=1)

        opts = ttk.LabelFrame(self, text=self.i18n.text("gui.options"))
        opts.pack(fill="x", padx=10, pady=6)

        ttk.Label(opts, text=self.i18n.text("gui.page_size")).grid(
            row=0, column=0, sticky="w", padx=8, pady=(8, 4)
        )
        self.preset = tk.StringVar(value=self.preset_labels["auto"])
        ttk.Combobox(
            opts,
            textvariable=self.preset,
            width=32,
            state="readonly",
            values=list(self.preset_labels.values()),
        ).grid(row=0, column=1, sticky="w", pady=(8, 4))

        ttk.Label(opts, text=self.i18n.text("gui.theme")).grid(
            row=1, column=0, sticky="w", padx=8, pady=4
        )
        self.theme_labels = {
            "Source document palette + fonts": "source",
            **{t.label: name for name, t in sorted(THEMES.items())},
        }
        self.theme = tk.StringVar(value=THEMES["default"].label)
        self.theme_box = ttk.Combobox(
            opts,
            textvariable=self.theme,
            width=32,
            state="readonly",
            values=list(self.theme_labels),
        )
        self.theme_box.grid(row=1, column=1, sticky="w", pady=4)

        ttk.Label(opts, text=self.i18n.text("gui.fonts")).grid(
            row=2, column=0, sticky="w", padx=8, pady=4
        )
        self.font_labels = {f.label: name for name, f in sorted(FONT_SETS.items())}
        self.font = tk.StringVar(value=FONT_SETS[DEFAULT_FONT_SET].label)
        self.font_box = ttk.Combobox(
            opts,
            textvariable=self.font,
            width=32,
            state="readonly",
            values=list(self.font_labels),
        )
        self.font_box.grid(row=2, column=1, sticky="w", pady=4)
        self.theme_box.bind("<<ComboboxSelected>>", self._theme_changed)

        checks = ttk.Frame(opts)
        checks.grid(row=0, column=2, rowspan=3, sticky="nw", padx=20, pady=(8, 4))
        self.soft = tk.BooleanVar(value=True)
        self.toc = tk.BooleanVar(value=True)
        ttk.Checkbutton(checks, text=self.i18n.text("gui.keep_breaks"), variable=self.soft).pack(
            anchor="w"
        )
        ttk.Checkbutton(checks, text=self.i18n.text("gui.contents"), variable=self.toc).pack(
            anchor="w"
        )
        opts.columnconfigure(2, weight=1)
        self.print_options = {}
        self.print_labels = {
            "output-mode": {
                self.i18n.text("option.online"): "online",
                self.i18n.text("option.print"): "print",
                self.i18n.text("option.tiled"): "tiled",
            },
            "margins": {
                self.i18n.text("option.margin_full"): "full",
                self.i18n.text("option.margin_none"): "none",
                self.i18n.text("option.margin_bottom_right"): "bottom-right",
            },
            "orientation": {
                self.i18n.text("option.portrait"): "portrait",
                self.i18n.text("option.landscape"): "landscape",
            },
        }
        for row, (flag, label_id) in enumerate(
            [
                ("output-mode", "field.output_mode"),
                ("margins", "field.page_margins"),
                ("orientation", "field.orientation"),
            ],
            start=3,
        ):
            choices = tuple(self.print_labels[flag])
            value = tk.StringVar(value=choices[0])
            self.print_options[flag] = value
            ttk.Label(opts, text=self.i18n.text(label_id)).grid(
                row=row, column=0, sticky="w", padx=8, pady=4
            )
            ttk.Combobox(opts, textvariable=value, values=choices, state="readonly", width=32).grid(
                row=row, column=1, sticky="w", pady=4
            )

        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=10, pady=6)
        self.run_btn = ttk.Button(bar, text=self.i18n.text("action.export"), command=self.start)
        self.run_btn.pack(side="left")
        self.cancel_btn = ttk.Button(
            bar, text=self.i18n.text("action.cancel"), command=self.cancel, state="disabled"
        )
        self.cancel_btn.pack(side="left", padx=6)
        self.open_btn = ttk.Button(
            bar,
            text=self.i18n.text("gui.open_output"),
            command=self.open_output,
            state="disabled",
        )
        self.open_btn.pack(side="left", padx=6)

        prog = ttk.Frame(self)
        prog.pack(fill="x", padx=10)
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.status = ttk.Label(prog, text=self.i18n.text("status.ready"), foreground=IDLE_COLOUR)
        self.status.pack(anchor="w", pady=(4, 0))

        self.banner = tk.Label(
            self,
            text="",
            anchor="w",
            justify="left",
            padx=10,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        )

        # Read-only, but still focusable so the keyboard can scroll it. Tab
        # leaves it instead of inserting a tab character.
        self.log = tk.Text(
            self, height=16, wrap="none", font=("Consolas", 9), state="disabled", takefocus=1
        )
        self.log.pack(fill="both", expand=True, padx=10, pady=(6, 10))
        self.log.bind("<Tab>", self._focus_next)
        for sequence in ("<Shift-Tab>", "<ISO_Left_Tab>"):
            with suppress(tk.TclError):  # ISO_Left_Tab is an X11 keysym
                self.log.bind(sequence, self._focus_previous)
        # ttk buttons answer only Space; Enter is the common expectation too.
        for sequence in ("<Return>", "<KP_Enter>"):
            self.bind_class("TButton", sequence, self._invoke_button)
        self.src_entry.focus_set()

    @staticmethod
    def _focus_next(event) -> str:
        event.widget.tk_focusNext().focus_set()
        return "break"

    @staticmethod
    def _focus_previous(event) -> str:
        event.widget.tk_focusPrev().focus_set()
        return "break"

    @staticmethod
    def _invoke_button(event) -> str:
        if event.widget.instate(["!disabled"]):
            event.widget.invoke()
        return "break"

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text)
        self.log.configure(state="disabled")
        self.log.see("end")

    # -- pickers -----------------------------------------------------------
    def pick_src(self) -> None:
        d = filedialog.askdirectory(title=self.i18n.text("gui.choose_source_folder"))
        if d:
            self.src.set(d)
            if not self.dst.get():
                self.dst.set(downloads_dir() or "")

    def pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title=self.i18n.text("gui.choose_source_title"),
            filetypes=[
                (
                    self.i18n.text("gui.supported_documents"),
                    "*.doc *.docx *.odt *.rtf *.xlsx *.md *.csv *.tsv",
                ),
                (self.i18n.text("gui.word_documents"), "*.doc *.docx *.odt *.rtf"),
                (self.i18n.text("gui.excel_workbooks"), "*.xlsx"),
                (self.i18n.text("gui.all_files"), "*.*"),
            ],
        )
        if path:
            self.src.set(path)
            if not self.dst.get():
                self.dst.set(downloads_dir() or "")

    def _theme_changed(self, _event=None) -> None:
        state = "disabled" if self.theme_labels[self.theme.get()] == "source" else "readonly"
        self.font_box.configure(state=state)

    def pick_dst(self) -> None:
        d = filedialog.askdirectory(title=self.i18n.text("gui.choose_output_folder"))
        if d:
            self.dst.set(d)

    def open_output(self) -> None:
        if self._last_output:
            reveal(self._last_output)

    # -- run control -------------------------------------------------------
    def start(self) -> None:
        if self._closing or (self.worker and self.worker.is_alive()):
            return
        src = self.src.get().strip()
        suffix = os.path.splitext(src)[1].lower()
        if not src or not (
            os.path.isdir(src)
            or (
                os.path.isfile(src)
                and suffix in {".doc", ".docx", ".odt", ".rtf", ".xlsx", ".md", ".csv", ".tsv"}
            )
        ):
            messagebox.showwarning(
                self.i18n.text("gui.choose_source_title"),
                self.i18n.text("gui.choose_source_warning"),
            )
            return

        output = self.dst.get().strip() or self._default_output(src)
        if not output:
            messagebox.showwarning(
                self.i18n.text("gui.choose_output_folder"),
                "Your Downloads folder is unavailable. Choose an output folder before exporting.",
            )
            return
        self.dst.set(output)
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.banner.pack_forget()
        self._cancel.clear()
        self._totals = {"documents": 0, "pages": 0, "missing": 0, "errors": 0, "warnings": 0}
        self.progress.config(value=0, maximum=100)
        self.run_btn.config(state="disabled")
        self.cancel_btn.config(state="normal")
        self.open_btn.config(state="disabled")
        self._set_status(self.i18n.text("gui.starting"), IDLE_COLOUR)

        # Read Tk variables on the UI thread, before shutdown can begin.
        argv = self._argv(src)
        self.worker = threading.Thread(target=self._run, args=(argv, output))
        self.worker.start()

    def cancel(self) -> None:
        self._cancel.set()
        self.cancel_btn.config(state="disabled")
        self._set_status(self.i18n.text("status.cancelling"), WARN_COLOUR)

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.worker and self.worker.is_alive():
            if not messagebox.askokcancel(
                self.i18n.text("gui.quit_title"), self.i18n.text("gui.quit_running")
            ):
                return
            self._closing = True
            self._cancel.set()
            self.run_btn.config(state="disabled")
            self.cancel_btn.config(state="disabled")
            self.open_btn.config(state="disabled")
            self._set_status(self.i18n.text("gui.stopping"), WARN_COLOUR)
            self.after(50, self._finish_close)
            return
        self._closing = True
        self.destroy()

    def _finish_close(self) -> None:
        """Keep Tk responsive until the worker's browser and stream cleanup finish."""
        if self.worker and self.worker.is_alive():
            self.after(50, self._finish_close)
            return
        self.destroy()

    # -- worker ------------------------------------------------------------
    def _argv(self, src: str) -> list[str]:
        argv = [src]
        if self.dst.get().strip():
            argv += ["-o", self.dst.get().strip()]
        preset = next(k for k, v in self.preset_labels.items() if v == self.preset.get())
        argv += ["--preset", preset]
        for flag, value in self.print_options.items():
            argv += [f"--{flag}", self.print_labels[flag][value.get()]]
        selected_theme = self.theme_labels[self.theme.get()]
        argv += ["--theme", selected_theme]
        if selected_theme != "source":
            argv += ["--font", self.font_labels[self.font.get()]]
        if not self.soft.get():
            argv.append("--no-soft-breaks")
        if not self.toc.get():
            argv += ["--no-toc", "--no-bookmarks"]
        return argv

    def _run(self, argv: list[str], output: str) -> None:
        self.messages.put(("output", output))

        old_stdout = sys.stdout
        sys.stdout = Redirector(self.messages)
        try:
            from platefold.cli import main

            code = main(
                argv,
                on_progress=self._on_progress,
                should_cancel=self._cancel.is_set,
                command_hints=False,
            )
            self.messages.put(("done", code))
        except SystemExit as exc:
            self.messages.put(("log", "\n%s\n" % exc))
            self.messages.put(("done", 1))
        except Exception as exc:
            import traceback

            self.messages.put(("log", "\nExport failed: %s\n%s\n" % (exc, traceback.format_exc())))
            self.messages.put(("done", 1))
        finally:
            sys.stdout = old_stdout

    @staticmethod
    def _default_output(src: str) -> str:
        return downloads_dir() or ""

    def _on_progress(self, done: int, total: int, rel: str, summary: dict | None) -> None:
        """Called on the worker thread; hand everything to the UI queue."""
        self.messages.put(("progress", (done, total, rel, summary)))

    # -- UI pump -----------------------------------------------------------
    def _set_status(self, text: str, colour: str = IDLE_COLOUR) -> None:
        self.status.config(text=text, foreground=colour)

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.messages.get_nowait()
                if kind == "log":
                    self._append_log(payload)
                elif kind == "output":
                    self._last_output = payload
                elif kind == "progress":
                    self._handle_progress(*payload)
                elif kind == "done":
                    self._handle_done(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain)

    def _handle_progress(self, done: int, total: int, rel: str, summary: dict | None) -> None:
        if self._closing:
            return
        self.progress.config(maximum=max(total, 1), value=done)
        if summary is None:
            self._set_status(
                self.i18n.text(
                    "gui.exporting",
                    current=self.i18n.number(done + 1),
                    total=self.i18n.number(total),
                    file=rel,
                )
            )
            return
        self._totals["documents"] += 1
        self._totals["pages"] += summary["pages"]
        self._totals["missing"] += len(summary["missing_images"])
        self._totals["errors"] += len(summary["diagram_errors"])
        self._totals["warnings"] += len(summary["warnings"])

    def _handle_done(self, code: int) -> None:
        if self._closing:
            return
        self.run_btn.config(state="normal")
        self.cancel_btn.config(state="disabled")
        if self._last_output and os.path.isdir(self._last_output):
            self.open_btn.config(state="normal")

        t = self._totals
        problems = t["missing"] + t["errors"]
        if code == 2:
            self._set_status(self.i18n.text("status.cancelled"), WARN_COLOUR)
            self._show_banner(
                self.i18n.plural("gui.cancelled_written", t["documents"]),
                WARN_COLOUR,
            )
        elif code != 0:
            self._set_status(self.i18n.text("status.failed"), ERROR_COLOUR)
            self._show_banner(self.i18n.text("gui.failed_log"), ERROR_COLOUR)
        elif problems:
            self._set_status(self.i18n.text("gui.finished_problems"), WARN_COLOUR)
            bits = []
            if t["missing"]:
                bits.append(self.i18n.plural("gui.missing_images", t["missing"]))
            if t["errors"]:
                bits.append(self.i18n.plural("gui.failed_diagrams", t["errors"]))
            self._show_banner(
                self.i18n.plural(
                    "gui.exported_problems",
                    t["documents"],
                    documents=self.i18n.number(t["documents"]),
                    pages=self.i18n.number(t["pages"]),
                    problems=self.i18n.text("gui.problem_join").join(bits),
                ),
                WARN_COLOUR,
            )
        elif t["warnings"]:
            self._set_status(self.i18n.text("status.finished_warnings"), WARN_COLOUR)
            self._show_banner(
                self.i18n.plural(
                    "gui.exported_warnings",
                    t["documents"],
                    documents=self.i18n.number(t["documents"]),
                    pages=self.i18n.number(t["pages"]),
                    warnings=self.i18n.plural("gui.warning_count", t["warnings"]),
                ),
                WARN_COLOUR,
            )
        else:
            self._set_status(self.i18n.text("status.done"), OK_COLOUR)
            self._show_banner(
                self.i18n.plural(
                    "gui.exported_ok",
                    t["documents"],
                    documents=self.i18n.number(t["documents"]),
                    pages=self.i18n.number(t["pages"]),
                ),
                OK_COLOUR,
            )
        self.progress.config(value=self.progress.cget("maximum"))

    def _show_banner(self, text: str, colour: str) -> None:
        self.banner.config(text=text, fg=colour)
        self.banner.pack(fill="x", padx=10, pady=(4, 0), before=self.log)


def main() -> None:
    """Entry point for the `platefold-gui` console script and `python gui.py`."""
    App().mainloop()


if __name__ == "__main__":
    main()
