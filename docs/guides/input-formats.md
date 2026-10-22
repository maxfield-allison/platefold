# Input formats

Plate|Fold exports eight formats. Five of them it reads itself; three need a converter that is already on the machine.

| Format | Route | What you get |
| --- | --- | --- |
| Markdown (`.md`) | Read directly | Headings, tables, fenced code, local images and Mermaid diagrams |
| Word (`.docx`) | Read directly | Headings, lists, tables, links, embedded images and the document's own palette |
| Excel (`.xlsx`) | Read directly | One section and table per visible worksheet |
| CSV (`.csv`) | Read directly | One table, with the first row repeating as its header |
| TSV (`.tsv`) | Read directly | The same, tab-separated |
| Word 97 (`.doc`) | LibreOffice | Converted to a temporary DOCX, then the DOCX route |
| OpenDocument (`.odt`) | LibreOffice or Pandoc | The same |
| Rich text (`.rtf`) | LibreOffice or Pandoc | The same |

Every route ends in the same renderer, so themes, fonts, the page ladder and the export report behave the same way whatever you started from.

## Plate|Fold re-lays out; it does not reproduce pages

A converted document is not a picture of the original. Plate|Fold reads the structure that reaches its DOCX reader, including headings, lists, tables and images, and lays it out again with your chosen theme, fonts and paper. Page breaks, column widths and line breaks will not match Word or Excel, and they are not meant to. Plate|Fold carries the structure it can read, including authored heading levels, table headers and image descriptions, into tagged output. Structural tests cover those paths through a deterministic converted-DOCX fixture, but LibreOffice or Pandoc can change or discard source structure before Plate|Fold sees it. Native screen-reader acceptance is still pending.

If you need a faithful page-for-page copy of a Word file, print it from Word.

## Wide tables take the page ladder

A table too wide for the text column is treated like a figure. A twenty-four column CSV exported with the default settings produced a single 551.8 × 196.5 mm custom page, cut to the table:

```console
platefold data/wide.csv -o pdfs --font sora
```

Long narrow tables behave the other way and continue across ordinary body pages. To keep every page the same size instead, `--preset uniform-letter` scales the table to fit; the same file then fits one 215.9 × 279.4 mm page. [How pages are chosen](placement.md) covers the thresholds.

## Delimited text

The delimiter is detected from comma, semicolon, tab or pipe. Quoted delimiters, doubled quotes and multiline fields are preserved, so a cell containing `South, coastal` stays one cell and `a ""quoted"" cell` reaches the PDF as `a "quoted" cell`. Empty lines are skipped, but a quoted empty field (`""`) is a record: in a one-column file it stays in the table as an empty cell.

The first row becomes a repeating header. Pass `--no-header` when every row is data:

```console
platefold data/raw.tsv -o pdfs --no-header
```

That turns off the header relationship, and the export warns that the table has no authored headers, because Plate|Fold can't know which row should have been one. Files must be UTF-8; a byte-order mark is accepted.

## Word

`.docx` is read directly. A Mermaid fence written into the document survives as a vector diagram rather than a code block.

Merged table cells carry over. A row with fewer cells than the rest of its table prints short, as Word draws it, and the tagged PDF fills it out with empty cells so that assistive technology reads every row with the table's columns. Those cells hold no text, like the cells Markdown adds to a short table row, so they read as blank. A row that Word starts one or more columns in, or ends early on purpose, keeps its cells in their own columns, with the columns it skips left blank in the same way. A malformed Word table whose rows don't share one grid can have two merged cells claiming the same place; Plate|Fold narrows the later cell to the columns left free, keeps its text, and names the table in a `DOCX_TABLE_OVERLAP` warning.

`--theme source` derives a palette and requested heading and body fonts from the document's own DrawingML theme:

```console
platefold notes/report.docx -o pdfs --theme source
```

A source palette is not guaranteed to be readable. Pairings below WCAG 2.1 AA are reported as `SOURCE_THEME_CONTRAST` and the export continues. [Contrast](themes.md#contrast) lists what is checked.

### Source fonts are requested, never downloaded

With `--theme source`, Plate|Fold asks for the Word or Excel file's own heading and body typefaces, each followed by a portable fallback stack. It never downloads fonts or loads font files embedded in the source document. When a requested face isn't installed, the export says so and uses the next face in the stack:

```text
FONT_SUBSTITUTED: Aptos is unavailable to the renderer for body text; using Liberation Sans. Install the requested font, or choose a font set whose fonts are installed. Substitution can change page layout.
```

A `--font` choice replaces the file's typefaces, and with any other theme they aren't used. When a source file names no typefaces, `SOURCE_FONT_FALLBACK` names the font set used instead.

An export on a machine without those faces paginates differently from one that has them. [Fonts and pagination](fonts.md) covers the consequences and how to get repeatable output.

To keep a document's palette as a reusable theme, `--save-theme` writes a complete copy into your user theme directory. It requires `--theme source` and exactly one DOCX or XLSX, and it refuses a name that already exists:

```console
platefold notes/template.docx -o pdfs --theme source --save-theme office-template
```

## Excel

Each visible worksheet becomes a section and a table. Cell number formats, table headers, merged-cell spans and embedded images are retained.

Hidden sheets, rows, columns and drawings are omitted, and the export says so rather than dropping them silently:

```text
XLSX_HIDDEN_CONTENT: Skipped hidden content: 1 sheet(s), 0 row(s), 0 column(s), 0 drawing(s).
```

### Number formats

A cell shows the text Excel displays for its number format, not the bare stored number: `000000` shows 123 as `000123`, `# ?/?` shows 0.25 as `1/4`, and `0.0,," million"` shows 1250000 as `1.3 million`. These parts of Excel's format language are applied:

- Digit placeholders `0`, `#` and `?`, decimal places, thousands separators, and trailing commas that divide by a thousand.
- Percent, and scientific notation with one digit before the decimal point, such as `0.00E+00`.
- Fractions such as `# ?/?`, `# ??/??` and `?/?`, and fixed denominators such as `# ?/8`.
- Quoted text, backslash-escaped characters, `@` for a text cell's own text, and `General`.
- Up to four sections, for positive, negative, zero and text values, and conditions such as `[>=100]0.0;0.00`.
- Currency and locale brackets such as `[$€-407]`. Colour brackets are ignored, `_` spacing becomes a space, and `*` fill becomes one space or nothing.

Numbers round half away from zero, as Excel rounds them. The separators come from the format code, `,` for thousands and `.` for decimals, not from the regional settings of the computer that opens the workbook.

A negative number keeps its sign when it rounds to zero: under `0`, −0.3 shows as `-0`, and under `0.0`, −0.03 shows as `-0.0`. A format with its own negative section decides for itself, so `0;(0)` shows −0.3 as `(0)`. Spreadsheet programs differ here: SheetJS also shows `-0`, LibreOffice Calc shows `0`, and Plate|Fold has not been compared with Excel itself. To print `0`, round the value in the workbook, for example with `=ROUND(A1,0)`.

Plate|Fold does not guess at any other format. The cell shows its stored value without rounding, and the export names the format it skipped:

```text
XLSX_NUMBER_FORMAT_UNSUPPORTED: 2 cell(s) use Excel number formats that Plate|Fold does not render and show their stored values unformatted instead: "##0.0E+0".
```

The formats reported this way are engineering notation such as `##0.0E+0`, date and time codes on cells the XLSX reader does not recognise as dates, other bracketed codes such as `[DBNum1]`, more than one `%` in a section, a scaling comma directly before the decimal point, a negative number under a conditional format, a value that matches none of the conditions, and digit placeholders in a text section.

Cells the XLSX reader recognises as dates or times keep their value but not the workbook's own layout: they are shown in ISO form, such as `2026-09-14`, `14:30:00` or `2026-09-14 14:30:00`, and durations as hours, minutes and seconds.

### Selecting sheets

Select worksheets by exact name or one-based index. `#` followed by a whole number from 1 without leading zeros, such as `#2`, always selects by position. Any other text is an exact name, including a name that begins with `#`, such as `#Summary`. A bare number such as `2` selects the sheet named `2` if there is one and otherwise the second sheet.

To select a sheet whose name looks like a position, such as `#2`, put `name:` in front of it. A selector that starts with `name:` is always an exact name, and Excel doesn't allow a colon in a sheet name, so the prefix can't be mistaken for part of one:

```console
platefold data/workbook.xlsx -o pdfs --sheets Summary "Quarter 2"
platefold data/workbook.xlsx -o pdfs --sheets "#2"
platefold data/workbook.xlsx -o pdfs --sheets "#Summary" "name:#2"
```

The same rules apply to `sheets` in a settings file and to `xlsx_sheets` in the server API.

Hidden sheets and chart sheets can't be selected. Naming one stops the export:

```text
Selected XLSX sheet is hidden and cannot be exported: Hidden notes
```

### Charts are not redrawn

**An Excel chart becomes a visible warning box in the PDF, not a chart.** The export reports it and carries on:

```text
XLSX_CHART_UNSUPPORTED: 1 Excel chart(s) were replaced with visible warnings.
```

To get a chart into the PDF, paste it into the worksheet as a static image before exporting. An embedded image is retained; a live chart object is not. Linked content, external workbook references and OLE objects are never followed either, for the reasons in [security](../project/security.md).

## Large tables

Chromium lays out a whole document before it prints the first page, and its memory use grows with the number of table cells. A document with more than 25,000 table cells is therefore printed in parts. Each part is its own browser pass of about 25,000 cells, and the parts are joined into one PDF. Header rows repeat, page numbers, running headers and footers continue across the joins, and the bookmarks, contents page, links and tagged structure remain those of one document.

A document of up to 1,000,000 table cells is accepted, within each format's own limits: a CSV or TSV file is also limited to 100,000 rows and 512 columns, and a Word file to 1,000,000 XML elements and attributes. In a workbook, the used range of the selected sheets counts, empty cells included; in a CSV or TSV file, every field counts; in Markdown and Word files, every table cell counts. Export time and memory use depend on the content, browser and machine; these input ceilings do not guarantee that an export completes within its resource budgets.

A larger document stops before rendering, with an error that names the limit:

```text
XLSX selected sheets exceed the 1000000-cell combined used-range limit; select fewer sheets or split the workbook
```

Choose fewer sheets with `--sheets`, or split the file. Two settings in the [limits](../reference/configuration.md#limits) section of a settings file control this: `max_cells_per_pass` sets the size of each part, and with it the memory an export needs, and `max_document_cells` bounds the whole document. A few kinds of table print whole because a part could not reproduce them: a table inside another table, one with a repeating footer, or one with its own column widths or inline style. Rows joined by a cell that spans rows stay together in one part as well. Content that cannot be split this way prints in one pass of up to four times `max_cells_per_pass` cells, 100,000 by default. A document with more stops with an error that names the limit before the browser lays that content out; split the table, or its long merged cells, to export it.

## Legacy documents

`.doc`, `.odt` and `.rtf` are converted to a temporary DOCX by a converter **you** install, and then read through the DOCX route. Plate|Fold does not bundle LibreOffice or Pandoc in any Python, desktop, AppImage or container package.

- `.doc` needs LibreOffice.
- `.odt` and `.rtf` can use LibreOffice, or Pandoc when that exact installation advertises the input format, DOCX output and `--sandbox`, and passes a sandboxed RTF-to-DOCX self-test. Some Pandoc builds advertise the formats but can't reach their DOCX writer while sandboxed; Plate|Fold reports those as unsupported rather than retrying without the sandbox.

Plate|Fold looks on `PATH` first, then where the standard installers put each converter, and uses the first one that passes its checks:

| System | LibreOffice | Pandoc |
|---|---|---|
| Windows | `%ProgramFiles%\LibreOffice\program\soffice.exe` (and the `(x86)` folder), then the locations the installer records in the registry | `%LOCALAPPDATA%\Pandoc\pandoc.exe`, then `%ProgramFiles%\Pandoc\pandoc.exe` |
| macOS | `LibreOffice.app` in `/Applications` or `~/Applications` | Homebrew (`/opt/homebrew/bin`, `/usr/local/bin`), then MacPorts (`/opt/local/bin`) |
| Linux | `PATH` only | `PATH` only |

On Linux, distribution packages put `soffice` and `pandoc` on `PATH`. Snap and Flatpak launchers aren't searched: their sandboxes keep a private `/tmp` and home directory, so they may not be able to read the private folder Plate|Fold converts in. To use another install, put its folder first on `PATH`.

With no supported converter installed, the export stops and names the options:

```text
No supported external converter route is available. Install LibreOffice, or re-save the file as DOCX. Pandoc can handle ODT/RTF only when its sandboxed DOCX capability check passes.
```

That exits non-zero, unlike a missing image. A successful report records the converter name, version, input and output formats, containment mode and any converter warnings.

Conversion can change or lose styles, layout, active content and source-theme details before Plate|Fold ever sees the document, so `--theme source` on a converted file reflects the temporary DOCX rather than the original. LibreOffice runs in headless safe mode with a disposable profile, which is **not** a filesystem or network sandbox. Read [security](../project/security.md#operator-installed-legacy-document-converters) before converting a file you don't trust.

## Set the document's language

Markdown, DOCX and XLSX have direct source-language paths. Markdown takes a `Language:` line below its opening title, DOCX uses its default or core language property, and XLSX uses the workbook language property. DOC, ODT and RTF can inherit a language only when the installed converter carries it into the temporary DOCX; conversion can change or lose it. CSV and TSV have nowhere to put one.

Without a language, every export reports:

```text
DOCUMENT_LANGUAGE: Document language is not set or was not recognized. Set the document language to en for English, or to the language your document uses. This helps screen readers pronounce the PDF correctly.
  To set it: add --document-language en, or the tag for your document's language, or set behaviour.document_language in a settings file. Markdown can also declare it with a "Language: en" line below the title.
```

The PDF is still written, tagged with `und`. Supply one with `--document-language en-GB`, or in a settings file, and the warning stops. [Accessibility](accessibility.md#language) explains why a reader needs it.
