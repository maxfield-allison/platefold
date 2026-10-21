# Printing and assembling tiles

**Tiled print** splits a diagram that is too big for one sheet across several ordinary sheets, at the size it was rendered. You print the sheets, trim them and tape them together into one large diagram. [How pages are chosen](placement.md#printing-and-assembling-diagrams) explains when a figure is tiled and when it stays inline.

```console
platefold diagram.md -o pdfs --output-mode tiled --preset letter
```

!!! note "Verified digitally"
    The tile geometry is checked by an automated test that prints tiles, rasterises them at 300 dpi, finds each sheet from its printed marks and stitches the sheets back together. The stitched result matches a single-sheet print of the same figure to within half a pixel at 300 dpi. That test proves the PDF. It can't prove your printer: an assembly from real paper and tape hasn't been checked yet, and printers differ in how accurately they place the page.

## What each sheet carries

Before the first sheet, the PDF prints the figure's caption and a short note with the number of columns, rows and sheets. Then each sheet holds one part of the diagram, called its window, plus three things around it. None of them are drawn over the diagram.

- **Trim marks** are short lines at the corners of the window. They continue the window's edges into the margin, so a straight cut from one mark to the matching mark on the same edge removes the margin.
- **Overlap marks** are a short line with a small circle, on sheets that have a neighbour to the right or below. They show where that neighbour's trimmed edge belongs.
- **The label** sits in a strip under the window: the figure number and name on the left, and the sheet's position on the right, such as `Tile 2 of 6: row 1 of 2, column 2 of 3`.

Marks go into a margin only when it is at least 10 mm wide, because most printers can't print right up to the paper's edge. With the default Full margin every edge has marks. The strip under the window always has them, so every vertical cut and overlap line is marked at least once even with no margins.

## The overlap

Neighbouring sheets repeat a strip of the diagram where they meet: by default the last 10 mm of one sheet's window is also the first 10 mm of the next. The repeated strip gives you room for tape or glue, and it lets you line sheets up by the picture itself, so a cut that wanders slightly off the trim line doesn't show.

Change it with `tile_overlap`, in millimetres, from 0 to 50:

```yaml
page:
  output_mode: tiled

layout:
  tile_overlap: 15
```

```console
platefold diagram.md -o pdfs --output-mode tiled --tile-overlap 15
```

In the desktop app it's **Tile overlap (mm)** under **Advanced layout**. `0` butts sheets edge to edge: each trimmed edge meets the neighbour's trim marks, and there is no repeated strip to hide a crooked cut. The overlap also has to stay under a third of the window's width and height, so each sheet still shows mostly new content. The value is rounded to the nearest CSS pixel, about 0.26 mm. A larger overlap can add sheets: at 10 mm, a 1400 px wide diagram needs three Letter columns where 0 mm needs two.

## Putting the sheets together

1. Print every sheet at **actual size (100%)**. Turn off “fit to page”, “shrink oversized pages” and similar options; they change the scale and the joins.
2. Lay the sheets out by their labels: row 1 at the top, columns left to right.
3. On every sheet except those in the first column, cut off the left margin along the left trim marks. On every sheet except those in the first row, cut off the top margin along the top trim marks.
4. Join each row from left to right. Lay each sheet over its left neighbour so the trimmed edge sits on the neighbour's overlap marks, or on its right trim marks when the overlap is 0. The repeated strip of the diagram should line up exactly; tape it from the back.
5. Join the rows from top to bottom the same way, laying each row over the one above so its trimmed top edge sits on the horizontal overlap marks.
6. Trim the outer margins if you want a clean border.

The labels on the lower rows and the right-hand column end up under the neighbouring sheets or in the outer border, so the finished diagram shows only the diagram.

## Export reports

The export report and `--json` result record tiling, so a script can tell how many sheets a job will print:

- `settings.tile_overlap_mm` is the overlap setting used for the export.
- Each document has a `tiled_figures` list with one entry per tiled figure: its `figure` number, `columns`, `rows`, `sheets` and `scale`. Tiled print never resizes a diagram, so `scale` is always `1.0`. The list is empty in Online and Print modes and when no figure needed tiling.

```json
"tiled_figures": [
  {"figure": 2, "columns": 3, "rows": 2, "sheets": 6, "scale": 1.0}
]
```

The HTTP API's report includes the same fields.
