#!/usr/bin/env python3
"""Export the documentation sample PDF and prove it shows every ladder rung.

The docs site claims one Markdown file produces a body page, a Letter landscape
plate, an A3 landscape plate and a custom page. This script makes that claim
checkable: it exports docs/samples/ladder-tour.md with the real renderer, reads
the page sizes back out of the PDF, fails unless each rung is present, and
writes a receipt recording what produced the file.

    python tools/build_docs_sample.py            # export, inspect, write
    python tools/build_docs_sample.py --inspect  # inspect the committed PDF only

The export needs a Chromium-family browser, so it is run by hand when the
sample or the renderer changes. --inspect needs only pypdf and runs anywhere.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile

from pypdf import PdfReader

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCE = os.path.join(REPO, "docs", "samples", "ladder-tour.md")
OUT_DIR = os.path.join(REPO, "docs", "assets", "samples")
PDF = os.path.join(OUT_DIR, "ladder-tour.pdf")
RECEIPT = os.path.join(OUT_DIR, "ladder-tour.json")
EXPORT_ARGS = ["--theme", "bristol", "--font", "sora", "--preset", "letter"]

PT_PER_MM = 72 / 25.4
TOLERANCE_MM = 0.5  # Chromium stores sizes in points; 420 mm reads back as 420.2
LETTER_PORTRAIT = (215.9, 279.4)
LETTER_LANDSCAPE = (279.4, 215.9)
A3_LANDSCAPE = (420.0, 297.0)
MAX_CUSTOM_WIDTH = 900.0


def page_sizes(path: str) -> list[tuple[float, float]]:
    reader = PdfReader(path)
    return [
        (
            round(float(page.mediabox.width) / PT_PER_MM, 1),
            round(float(page.mediabox.height) / PT_PER_MM, 1),
        )
        for page in reader.pages
    ]


def classify(size: tuple[float, float]) -> str:
    def near(expected: tuple[float, float]) -> bool:
        return all(abs(a - b) <= TOLERANCE_MM for a, b in zip(size, expected, strict=True))

    if near(LETTER_PORTRAIT):
        return "body"
    if near(LETTER_LANDSCAPE):
        return "letter-landscape"
    if near(A3_LANDSCAPE):
        return "a3-landscape"
    if size[0] <= MAX_CUSTOM_WIDTH + TOLERANCE_MM:
        return "custom"
    return "unexpected"


def inspect(path: str) -> list[dict]:
    pages = [
        {"page": index, "width_mm": w, "height_mm": h, "kind": classify((w, h))}
        for index, (w, h) in enumerate(page_sizes(path), start=1)
    ]
    kinds = {page["kind"] for page in pages}
    missing = {"body", "letter-landscape", "a3-landscape", "custom"} - kinds
    if missing or "unexpected" in kinds:
        raise SystemExit(
            f"sample PDF does not show the full ladder: missing {sorted(missing)}, pages {pages}"
        )
    return pages


def sha256(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def export() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        command = [sys.executable, "-m", "platefold", SOURCE, "-o", tmp, "--json", *EXPORT_ARGS]
        result = subprocess.run(command, cwd=REPO, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise SystemExit(f"export failed ({result.returncode}):\n{result.stderr}")
        outcome = json.loads(result.stdout)
        if outcome.get("status") != "completed":
            raise SystemExit(
                f"export finished as {outcome.get('status')}; the sample must be clean"
            )
        os.makedirs(OUT_DIR, exist_ok=True)
        with open(os.path.join(tmp, "ladder-tour.pdf"), "rb") as src, open(PDF, "wb") as dst:
            dst.write(src.read())
        return outcome


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="check the committed PDF only")
    args = parser.parse_args()

    if args.inspect:
        pages = inspect(PDF)
        with open(RECEIPT, encoding="utf-8") as handle:
            receipt = json.load(handle)
        if receipt.get("sha256") != sha256(PDF):
            raise SystemExit("ladder-tour.pdf does not match the sha256 in its receipt")
        if receipt.get("pages") != pages:
            raise SystemExit("ladder-tour.pdf page sizes do not match its receipt")
        if receipt.get("source_sha256") != sha256(SOURCE):
            raise SystemExit("ladder-tour.md changed since the sample PDF was exported")
        print(f"sample PDF shows every rung across {len(pages)} pages")
        return 0

    outcome = export()
    pages = inspect(PDF)
    report = outcome["report"]
    receipt = {
        "source": "docs/samples/ladder-tour.md",
        "command": "platefold docs/samples/ladder-tour.md -o <dir> " + " ".join(EXPORT_ARGS),
        # The exact source bytes, which any reader can check. A commit hash would
        # name history that isn't public.
        "source_sha256": sha256(SOURCE),
        "engine": report.get("engine"),
        "platform": report.get("platform"),
        "font_set": report["settings"].get("font_set"),
        "theme": report["settings"].get("theme"),
        "warnings": report["documents"][0].get("warnings"),
        "sha256": sha256(PDF),
        "pages": pages,
    }
    with open(RECEIPT, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(receipt, handle, indent=2)
        handle.write("\n")
    print(f"wrote {os.path.relpath(PDF, REPO)} ({len(pages)} pages) and its receipt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
