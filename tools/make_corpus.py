#!/usr/bin/env python3
"""Generate a synthetic benchmark corpus.

    python tools/make_corpus.py _bench 25

Produces documents shaped like the real thing — a metadata block, enough
headings to trigger a contents page, prose, wide reference tables, code
blocks, and a mix of narrow and oversized mermaid diagrams — so that timings
reflect the work the exporter actually does rather than a toy file.

Diagrams are varied per document on purpose: a corpus of identical diagrams
would all hit the cache after the first one and flatter the numbers.
"""

from __future__ import annotations

import os
import sys

PROSE = (
    "The exporter measures every figure and places it on the smallest page "
    "that holds it near full size. This paragraph exists to give the layout "
    "engine realistic body copy to break across columns and pages, including "
    "enough words that widow and orphan control has something to do."
)


def narrow_diagram(n: int) -> str:
    return (
        "```mermaid\nflowchart LR\n"
        f'    a{n}["Stage {n} start"] --> b{n}["Validate"]\n'
        f'    b{n}["Validate"] --> c{n}["Finish"]\n'
        "```\n"
    )


def wide_diagram(n: int) -> str:
    nodes = " --> ".join(
        f'n{n}_{i}["Regional processing stage {i} with a deliberately long label"]'
        for i in range(7)
    )
    return f"```mermaid\nflowchart LR\n    {nodes}\n```\n"


def table(cols: int) -> str:
    head = "| " + " | ".join(f"Column {i}" for i in range(cols)) + " |"
    rule = "|" + "|".join(["---"] * cols) + "|"
    rows = "\n".join(
        "| " + " | ".join(f"row {r} cell {c}" for c in range(cols)) + " |" for r in range(8)
    )
    return f"{head}\n{rule}\n{rows}\n"


def code_block() -> str:
    body = "\n".join(f"    value_{i} = compute(index={i}, factor={i * 3})" for i in range(12))
    return f"```python\ndef pipeline():\n{body}\n    return value_11\n```\n"


def document(n: int) -> str:
    parts = [
        f"# Benchmark Document {n}\n",
        "Document: Synthetic benchmark corpus",
        "Version: 1.0.0",
        "Owner: Benchmark",
        "\n---\n",
    ]
    for section in range(1, 11):
        parts.append(f"\n# {section}. Section {section} of document {n}\n")
        parts.append(PROSE + "\n")
        parts.append(f"\n## {section}.1 Detail\n")
        parts.append(PROSE + "\n")
        if section % 3 == 1:
            parts.append("\n" + narrow_diagram(n * 100 + section))
        elif section % 3 == 2:
            parts.append("\n" + wide_diagram(n * 100 + section))
        if section % 2 == 0:
            parts.append("\n" + table(7))
        if section % 4 == 0:
            parts.append("\n" + code_block())
    return "\n".join(parts)


def main(target: str, count: int) -> int:
    os.makedirs(target, exist_ok=True)
    for n in range(count):
        path = os.path.join(target, "doc%02d.md" % n)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(document(n))
    print("wrote %d document(s) to %s" % (count, target))
    return 0


if __name__ == "__main__":
    sys.exit(
        main(
            sys.argv[1] if len(sys.argv) > 1 else "_bench",
            int(sys.argv[2]) if len(sys.argv) > 2 else 25,
        )
    )
