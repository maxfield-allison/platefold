---
title: Documents to PDF with every figure on a page that fits
description: Plate|Fold exports Word, Excel, Markdown and delimited text to PDF, measuring each figure and table and giving it a page large enough to read, so body text stays on Letter or A4 while wide Mermaid diagrams and wide tables get landscape plates or pages cut to their shape.
---

# Plate|Fold

**Documents to PDF with every figure on a page that fits.**

Export a document with one page size and a wide Mermaid flowchart gets squeezed into the text column. A twenty-four column spreadsheet does the same. On paper both print as grey fuzz you can't read.

Plate|Fold measures each figure and each table before it prints. If it fits the text column at a readable size, it stays there. If it doesn't, it moves to a landscape plate, then a larger sheet, and finally a page cut to its own proportions. Body text stays on Letter or A4 pages the whole way through, in the same PDF.

![Four pages drawn to scale: a portrait text column, a Letter landscape plate, an A3 landscape plate and a long custom page. Each figure stops at the first page that holds it.](assets/brand/ladder-light.svg#only-light){ .pf-figure }
![Four pages drawn to scale: a portrait text column, a Letter landscape plate, an A3 landscape plate and a long custom page. Each figure stops at the first page that holds it.](assets/brand/ladder-dark.svg#only-dark){ .pf-figure }

All four appear in the seven-page [sample PDF](reference/sample.md), exported from one Markdown file.

!!! note "Release 1.0.0"

    Windows installers are publisher-signed and timestamped, the Apple Silicon app is signed and notarized, and the Linux AppImage is checked in CI. Platform coverage differs; see [release status](project/status.md) before choosing a package.

## What it does

<div class="grid cards" markdown>

-   **Eight input formats**

    ---

    Word, Excel, Markdown and delimited text export directly. DOC, ODT and RTF go through a converter you already have installed; Plate|Fold never ships one.

    [Input formats](guides/input-formats.md)

-   **Pages chosen per figure**

    ---

    Inline at 80% of natural size or better, then Letter landscape and A3 landscape plates at 85% or better, then a custom page up to 900 mm wide. Wide tables take the same ladder.

    [How pages are chosen](guides/placement.md)

-   **Vector Mermaid**

    ---

    Diagrams render to SVG in a headless Chromium and stay vector in the PDF. Mermaid ships inside the package, so exports don't need Node.js or a network connection.

    [Your first export](get-started/first-export.md)

-   **Themes as data**

    ---

    Ten built-in palettes, from ivory paper to dark screen-reading themes. A custom theme is a small JSON file that overrides only the colours you change, and a Word or Excel file can supply its own.

    [Themes](guides/themes.md)

-   **Built for scripts too**

    ---

    `--json` returns a versioned result with page counts, warnings and exit codes, and the same export service is callable from Python.

    [Automation](automation.md)

-   **Export in a browser**

    ---

    An optional authenticated Linux server runs the same engine behind a job API, with uploads, cancellation and hash-checked downloads.

    [Browser exports](guides/server.md)

</div>

## Warnings and limits

A missing image or a Mermaid diagram that fails to parse gets a visible warning box in the PDF and an entry in `_export-report.json`. An Excel chart gets one too, because charts are not redrawn. The export still exits with `0`, so scripts should check the result's status.

Remote images aren't fetched, document scripts don't run, and references outside the input folder are reported as missing. The [security](project/security.md) page has the resource limits and link handling.
