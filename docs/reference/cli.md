# Command line

`platefold` exports DOC, DOCX, ODT, RTF, XLSX, Markdown, CSV and TSV files or folders to PDF. `python -m platefold` runs the same command. DOC, ODT and RTF need a compatible LibreOffice or Pandoc already installed, on `PATH` or in its standard install location ([where Plate|Fold looks](../guides/input-formats.md#legacy-documents)); no converter is included with Plate|Fold. These formats are converted to a temporary DOCX, then laid out again from their structure. Their original pages are not reproduced. Each visible XLSX worksheet becomes a section and table. CSV and TSV dialects are detected from comma, tab, semicolon or pipe delimiters; quoted delimiters, doubled quotes and multiline fields are preserved. The first row becomes a repeating header unless `--no-header` is set.

This is the help text from the version this site was built with, generated from the command's own argument parser:

<!-- platefold:generate cli-help -->

## Common runs

```console
platefold notes -o pdfs
platefold notes/report.md -o pdfs --theme bristol
platefold notes/report.docx -o pdfs --theme source
platefold notes/archive.doc -o pdfs
platefold notes/open-document.odt -o pdfs
platefold notes/rich-text.rtf -o pdfs
platefold notes/report.docx -o pdfs --theme source --save-theme office-template
platefold data/workbook.xlsx -o pdfs --sheets Summary "Quarter 2"
platefold data/workbook.xlsx -o pdfs --theme source
platefold data/report.csv -o pdfs
platefold data/raw.tsv -o pdfs --no-header
platefold notes -o pdfs --output-mode print --preset a4
platefold diagram.md -o pdfs --output-mode tiled --preset letter --margins bottom-right
platefold notes -o pdfs --browser chrome --font arial
platefold notes -o pdfs --json > result.json
platefold --list-themes --json
```

`--theme source` derives an effective palette and requested heading/body fonts independently for each DOCX or XLSX, including the temporary DOCX produced from a legacy document. Conversion can change or lose the original theme details. Inputs without a complete DrawingML theme visibly warn and use the selected settings-file/default fallback. An explicit named `--theme` selects that named theme instead. `--save-theme` requires `--theme source` and exactly one DOCX or XLSX; it creates a complete user theme and refuses name collisions.

`--sheets SHEET [SHEET ...]` limits an XLSX export to exact sheet names or one-based workbook indexes. The default is every visible worksheet. `#` followed by a whole number from 1 without leading zeros, such as `#2`, always selects by position. Any other text is an exact name, including names that begin with `#`, such as `#Summary`; a bare number such as `2` selects the sheet named `2` if there is one and otherwise the second sheet. Prefix a selector with `name:` to make it an exact name whatever it looks like: `name:#2` selects the sheet named `#2`. Excel doesn't allow a colon in a sheet name, so the prefix never clashes with one. Hidden sheets and chart sheets cannot be selected.

## Choosing a browser

`--browser` takes `auto`, `bundled`, `system`, `edge`, `chrome`, `chromium`, `brave`, or a path to an executable.

- `auto`, the default, tries a bundled or cached Chromium, then installed browsers.
- `bundled` uses only a bundled or cached Chromium and fails if there isn't one.
- `system` tries installed browsers that pass a rendering check first, then falls back to the bundled Chromium.
- A browser name uses only that browser.

## Output

Without `--json`, progress goes to the terminal: one line per document, then a summary. `--quiet` drops the per-document lines. `--timings` adds a breakdown of where the time went.

With `--json`, stdout carries exactly one JSON object and everything else goes to stderr. The [result and exit codes](../automation.md) are documented with automation.

`--ui-locale` selects human-readable command-line messages and falls back to English when Plate|Fold does not ship a matching human translation. When `--ui-locale` isn't given, `PLATEFOLD_UI_LOCALE` makes the same choice, for the command line and for the older desktop interface. It is separate from `--locale`, which only supplies a regional paper default. Neither option sets or translates the source document's language. JSON field names, status values and error codes are stable across interface locales.

Every export writes `_export-report.json` into the output folder, with or without `--json`.

For `.doc`, Plate|Fold uses a detected LibreOffice `soffice` route. For `.odt` and `.rtf`, it can also use Pandoc when that exact installation advertises the input format, DOCX output and `--sandbox`, then passes a small sandboxed RTF-to-DOCX self-test. Some Pandoc builds advertise the formats but cannot use their DOCX writer while sandboxed; Plate|Fold reports those builds as unsupported. Pandoc is never offered for binary DOC. If no supported route is available, the command exits nonzero, names the converter options and suggests re-saving as DOCX. Successful reports include the converter name, version, input/output formats and containment mode, plus any converter warnings.

Conversion may lose layout, styles, active content and source-theme details before DOCX ingestion; `--theme source` never promises to reproduce the original legacy document's theme. Pandoc runs with its own sandbox. LibreOffice safe mode and a private profile are not a filesystem or network sandbox; see the [security policy](../project/security.md) before converting untrusted files.

## The other entry point

`platefold-gui` opens the older Tk interface. The newer [desktop app](../windows-desktop.md) is a separate build.
