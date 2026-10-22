# Sample PDF

[Download the sample PDF](../assets/samples/ladder-tour.pdf){ .md-button .md-button--primary }

Seven pages exported from one Markdown file with four diagrams. Each diagram is just wide enough to need the next rung of the page ladder, so the PDF has every page type Plate|Fold produces.

## What's in it

These sizes were read back out of the exported PDF:

| Page | Size (mm) | What it is |
| --- | --- | --- |
| 1 | 215.9 × 279.4 | Body page with an inline diagram |
| 2 | 279.4 × 215.9 | Letter landscape plate |
| 3 | 215.9 × 279.4 | Body page |
| 4 | 420.2 × 297.0 | A3 landscape plate |
| 5 | 215.9 × 279.4 | Body page |
| 6 | 686.1 × 90.0 | Custom page, cut to the diagram |
| 7 | 215.9 × 279.4 | Body page with a table and code |

A3 reads back as 420.2 mm wide because the PDF stores page sizes in points, and the conversion rounds. The custom page is 90 mm tall because that's the minimum for a custom page, and the diagram itself is much shallower.

Pages 3 and 5 are mostly empty, because the [heading and lead-in before each wide diagram](../guides/placement.md#what-the-page-around-a-plate-looks-like) stay on the body page and the diagram gets the next page to itself.

## How it was made

```console
platefold docs/samples/ladder-tour.md -o pdfs --theme bristol --font sora --preset letter
```

`tools/build_docs_sample.py` runs that export, reads every page size back out of the PDF, and refuses to write the sample unless all four rungs are present. It records a receipt beside the PDF with the SHA-256 of the source file, the browser that rendered it, the theme and fonts, any warnings, the file's SHA-256 and the page list. This copy was rendered by Chromium 151 on Linux with the bundled Sora and JetBrains Mono fonts, and finished without warnings.

Exporting the same file yourself should give the same page types, with line breaks that can differ under another browser version or [font setup](../guides/fonts.md).

## The source

This is the complete input, `docs/samples/ladder-tour.md`:

````markdown
--8<-- "ladder-tour.md"
````
