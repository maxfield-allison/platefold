"""Measure real exports with a process-local CDP flag override, never product edits.

Run with the project's dev+server environment. Each trial is a fresh subprocess.
Capture runs are separate: copying PDFs and HTML does not pollute timed trials.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import platform
import shutil
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import psutil

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from corpus import create  # noqa: E402
from inspect_pdf import inspect  # noqa: E402

from platefold import cdp, service  # noqa: E402
from platefold import pdf as pdf_pipeline  # noqa: E402
from platefold.browser import find_bundled  # noqa: E402
from platefold.config import make_settings  # noqa: E402


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def worker(args):
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    settings = json.loads(args.settings)
    request = service.ExportRequest(
        str(args.source),
        str(out / "final"),
        settings=make_settings(**settings),
        diagram_cache=str(out / "cache"),
        timings=True,
        browser="bundled",
    )
    original_send, original_print = cdp.Page._send, service.print_pdf
    print_count = 0

    class CloneWriter(pdf_pipeline.PdfWriter):
        def append(self, reader):
            self.clone_document_from_reader(reader)

    def send(page, method, params=None):
        if method == "Page.printToPDF":
            params = dict(params, generateTaggedPDF=args.tagged)
        return original_send(page, method, params)

    def capture(page, html, destination, *rest):
        nonlocal print_count
        print_count += 1
        original_print(page, html, destination, *rest)
        shutil.copyfile(destination, out / f"print-{print_count}.pdf")
        (out / f"print-{print_count}.html").write_text(html, encoding="utf-8")

    stop = threading.Event()
    samples = []
    process = psutil.Process()

    def sample():
        while not stop.is_set():
            total = 0
            for member in [process, *process.children(recursive=True)]:
                with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
                    total += member.memory_info().rss
            samples.append(total)
            stop.wait(0.01)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    started = time.perf_counter()
    try:
        with (
            patch.object(cdp.Page, "_send", send),
            patch.object(service, "print_pdf", capture if args.capture else original_print),
            patch.object(
                pdf_pipeline,
                "PdfWriter",
                CloneWriter if args.clone_bookmarks else pdf_pipeline.PdfWriter,
            ),
        ):
            result = service.export(request)
        elapsed = time.perf_counter() - started
    finally:
        stop.set()
        sampler.join()
    pdf = next((out / "final").glob("*.pdf"))
    write_json(
        out / "metrics.json",
        {
            "tagged": args.tagged,
            "capture": args.capture,
            "clone_bookmarks": args.clone_bookmarks,
            "settings": settings,
            "elapsed_seconds": elapsed,
            "peak_tree_rss_bytes": max(samples),
            "rss_samples": len(samples),
            "final_bytes": pdf.stat().st_size,
            "phases_seconds": dict(result.timings.total),
            "phase_counts": dict(result.timings.counts),
            "status": result.status,
            "report": result.report,
        },
    )


def trial(args, name, source, settings, tagged, suffix, capture=False, clone=False):
    out = args.output / name / f"{'on' if tagged else 'off'}-{suffix}"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--source",
        str(source),
        "--output",
        str(out),
        "--settings",
        json.dumps(settings),
    ]
    if tagged:
        command.append("--tagged")
    if capture:
        command.append("--capture")
    if clone:
        command.append("--clone-bookmarks")
    subprocess.run(command, check=True, timeout=240, cwd=ROOT)
    if capture:
        files = list(out.glob("print-*.pdf")) + list((out / "final").glob("*.pdf"))
        for pdf in files:
            write_json(pdf.with_suffix(".inspection.json"), inspect(pdf))
    return json.loads((out / "metrics.json").read_text())


def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    browser = Path(find_bundled())
    write_json(
        args.output / "environment.json",
        {
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip(),
            "python": sys.version,
            "platform": platform.platform(),
            "browser_path": str(browser),
            "browser_sha256": hashlib.sha256(browser.read_bytes()).hexdigest(),
            "browser_version": subprocess.check_output(
                [str(browser), "--version"], text=True
            ).strip(),
            "packages": {
                n: importlib.metadata.version(n)
                for n in ("pypdf", "psutil", "playwright", "pypdfium2")
            },
            "method": "Fresh process/browser/cache per trial; 10ms summed process-tree RSS (shared pages double-counted); captures excluded; warm OS cache; alternating off/on order; no conformance inference.",
        },
    )
    cases = create(args.output / "corpus")
    summary = {}
    for name, (source, settings) in cases.items():
        measures = {False: [], True: []}
        for index in range(args.repeats):
            for tagged in (False, True) if index % 2 == 0 else (True, False):
                measures[tagged].append(trial(args, name, source, settings, tagged, str(index)))
        summary[name] = {}
        for tagged in (False, True):
            values = measures[tagged]
            summary[name]["on" if tagged else "off"] = {
                key: {
                    "median": statistics.median(v[key] for v in values),
                    "min": min(v[key] for v in values),
                    "max": max(v[key] for v in values),
                }
                for key in ("elapsed_seconds", "peak_tree_rss_bytes", "final_bytes")
            }
            trial(args, name, source, settings, tagged, "capture", capture=True)
        # Existing no-bookmarks option isolates the final rewrite from Chromium.
        trial(
            args, name, source, settings | {"bookmarks": False}, True, "no-bookmarks", capture=True
        )
        write_json(args.output / "summary.json", summary)
        print(f"Completed {name}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--settings", default="{}")
    parser.add_argument("--tagged", action="store_true")
    parser.add_argument("--capture", action="store_true")
    parser.add_argument("--clone-bookmarks", action="store_true")
    options = parser.parse_args()
    if options.repeats < 1:
        parser.error("--repeats must be positive")
    worker(options) if options.worker else run(options)
