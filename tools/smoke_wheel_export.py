#!/usr/bin/env python3
"""Install a built wheel with its newest dependencies and export real documents with it.

uv.lock and the requirements files pin what the tests and the packaged builds
use. Someone who installs the wheel with pip instead gets the newest release
of each dependency that its declared range allows, so a dependency release
can break that install while every pinned check still passes. This creates a
virtual environment outside the checkout, installs the wheel into it with no
constraints, and exports SAMPLE.md and the documentation's ladder-tour sample
with the installed command. The ladder tour's page sizes must match the
receipt the documentation publishes beside its PDF.

    python tools/smoke_wheel_export.py dist/platefold-<version>-py3-none-any.whl

It needs uv and a Chromium-family browser that Plate|Fold can discover.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "SAMPLE.md"
LADDER = ROOT / "docs" / "samples" / "ladder-tour.md"
LADDER_RECEIPT = ROOT / "docs" / "assets" / "samples" / "ladder-tour.json"
# The receipt's sizes are rounded to 0.1 mm; a custom page follows its
# diagram's measured width, which can move by a fraction of a millimetre.
SIZE_TOLERANCE_MM = 1.0
POINTS_PER_MM = 72 / 25.4


class SmokeError(Exception):
    pass


def run(command: list[str], cwd: Path) -> str:
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        # --json reports a failed export on stdout.
        detail = result.stderr.strip() or result.stdout[-2000:]
        raise SmokeError(f"{command[0]} {command[1]} exited {result.returncode}:\n{detail}")
    return result.stdout


def install(wheel: Path, work: Path) -> Path:
    """A fresh environment with the wheel and the newest dependencies it allows.

    uv rather than pip creates and fills it, because the CI image's Python has
    no ensurepip. Like pip, `uv pip install` picks each dependency's newest
    release in the declared range; --no-config keeps any project settings out.
    """
    environment = work / "venv"
    uv = ["uv", "--no-config"]
    run([*uv, "venv", "--quiet", "--python", sys.executable, str(environment)], work)
    bin_dir = environment / ("Scripts" if sys.platform == "win32" else "bin")
    python = str(bin_dir / "python")
    run([*uv, "pip", "install", "--quiet", "--python", python, str(wheel)], work)
    print(run([*uv, "pip", "freeze", "--python", python], work), end="")
    return bin_dir


def export(bin_dir: Path, source: Path, work: Path, *options: str) -> list[tuple[float, float]]:
    """Export `source` and return each page's (width, height) in millimetres."""
    output = work / source.stem
    command = [str(bin_dir / "platefold"), str(source), "-o", str(output), "--json", *options]
    result = json.loads(run(command, work))
    if result["status"] != "completed":
        raise SmokeError(f"{source.name}: export {result['status']}: {result}")
    document = result["report"]["documents"][0]
    if document["warnings"] or document["diagram_errors"] or document["missing_images"]:
        raise SmokeError(f"{source.name}: export reported problems: {document}")
    script = (
        "import json, sys\n"
        "from pypdf import PdfReader\n"
        "boxes = [page.mediabox for page in PdfReader(sys.argv[1]).pages]\n"
        "print(json.dumps([[float(b.width), float(b.height)] for b in boxes]))\n"
    )
    pdf = output / f"{source.stem}.pdf"
    points = json.loads(run([str(bin_dir / "python"), "-c", script, str(pdf)], work))
    pages = [(w / POINTS_PER_MM, h / POINTS_PER_MM) for w, h in points]
    print(f"{source.name}: {len(pages)} pages, " + ", ".join(f"{w:.1f}x{h:.1f}" for w, h in pages))
    return pages


def check_ladder_sizes(pages: list[tuple[float, float]]) -> None:
    receipt = json.loads(LADDER_RECEIPT.read_text(encoding="utf-8"))
    expected = [(page["width_mm"], page["height_mm"]) for page in receipt["pages"]]
    if len(pages) != len(expected):
        raise SmokeError(f"ladder-tour: {len(pages)} pages, receipt has {len(expected)}")
    for number, (got, want) in enumerate(zip(pages, expected, strict=True), 1):
        if any(abs(g - w) > SIZE_TOLERANCE_MM for g, w in zip(got, want, strict=True)):
            raise SmokeError(f"ladder-tour page {number}: {got} mm, receipt has {want} mm")


def smoke(wheel: Path, work: Path) -> None:
    bin_dir = install(wheel, work)
    print(run([str(bin_dir / "platefold"), "--version"], work), end="")
    if not export(bin_dir, SAMPLE, work):
        raise SmokeError("SAMPLE.md produced no pages")
    # The receipt records the options its PDF was exported with.
    options = ["--theme", "bristol", "--font", "sora", "--preset", "letter"]
    check_ladder_sizes(export(bin_dir, LADDER, work, *options))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--work", type=Path, help="keep the environment and PDFs here")
    args = parser.parse_args(argv)
    try:
        if args.work:
            args.work.mkdir(parents=True, exist_ok=True)
            smoke(args.wheel.resolve(), args.work.resolve())
        else:
            with tempfile.TemporaryDirectory() as work:
                smoke(args.wheel.resolve(), Path(work))
    except SmokeError as error:
        print(f"smoke_wheel_export: {error}", file=sys.stderr)
        return 1
    print("smoke_wheel_export: the installed wheel exported both documents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
