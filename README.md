# Plate|Fold

Plate|Fold turns DOC, DOCX, ODT, RTF, XLSX, Markdown, CSV and TSV into semantic, print-quality PDFs with vector Mermaid diagrams, embedded images and readable tables. DOC, ODT and RTF require a compatible LibreOffice or Pandoc already installed on the machine; Plate|Fold does not bundle either converter. A wide diagram or table gets a page large enough to read it: first a landscape plate, then a larger sheet, and finally a page cut to its proportions. Body text and narrow tables can stay on Letter or A4 pages in the same document.

**Version 1.0.2.** Published downloads are listed on the [releases page](https://github.com/maxfield-allison/platefold/releases). Desktop releases require publisher-signed and timestamped Windows installers, a signed and notarized Apple Silicon app, and qualified Linux packages. See the [release status](https://maxfield-allison.github.io/platefold/project/status/) for recorded platform checks, availability and remaining qualification.

## Run from source

The desktop app for Windows, macOS and Linux provides the designed interface with its own browser and CLI; [Get started](https://maxfield-allison.github.io/platefold/get-started/) covers installing it. These steps run the Python package from a copy of the repository instead.

Use Python 3.11 or newer and an installed Chromium-family browser. Download the [source archive](https://github.com/maxfield-allison/platefold/releases) or clone the repository, then enter its folder:

```console
git clone https://github.com/maxfield-allison/platefold.git
cd platefold
```

Create and activate a virtual environment:

```console
python -m venv .venv
```

On Windows, activate it with `.venv\Scripts\Activate.ps1`. On macOS or Linux, use `source .venv/bin/activate`. Then install and run:

```console
python -m pip install -e .
platefold SAMPLE.md -o output
platefold-gui
```

`python -m platefold` also runs the CLI. Mermaid is included in the package. Normal exports don't need Node.js. `platefold-gui` opens the older Tk window, which is separate from the Tauri desktop app described above. Some Linux Python installations supply Tk as a separate system package.

## Export documents

```console
platefold docs -o output
platefold docs/notes.md -o output --theme bristol
platefold docs/brief.docx -o output --theme source
platefold docs/archive.doc -o output
platefold docs/open-document.odt -o output
platefold docs/rich-text.rtf -o output
platefold docs/template.docx -o output --theme source --save-theme organisation
platefold data/workbook.xlsx -o output --sheets Summary "Quarter 2"
platefold data/report.csv -o output
platefold data/raw.tsv -o output --no-header
platefold docs --preset uniform-letter
platefold docs --browser chrome --font arial
platefold --list-themes
```

Folder exports preserve the input directory structure. `_export-report.json` records the rendering engine, settings, page counts and document warnings. Missing images and failed diagrams also leave visible warning boxes in the PDF so they don't disappear into an otherwise finished document.

| Input | Behavior |
| --- | --- |
| Word (`.docx`) | Semantic headings, lists, tables, links and embedded images via Mammoth. Plate|Fold re-lays out the content; it does not reproduce Word pages. A preformatted Mermaid fence remains a vector diagram. |
| Legacy documents (`.doc`, `.odt`, `.rtf`) | Converted to a temporary DOCX by a detected compatible LibreOffice or Pandoc installation, then processed through the DOCX path. Binary `.doc` requires LibreOffice; Pandoc is considered only for ODT/RTF and only when it advertises the input, DOCX output and `--sandbox`. Conversion can change styles, layout and source-theme details. |
| Excel (`.xlsx`) | Each visible sheet becomes a semantic section and table. Excel number formats, table headers, merged-cell spans, embedded images and source themes are retained; dates show in ISO form, a number format outside the supported set shows the stored value with a warning, and hidden content is omitted. Charts and linked content produce visible warnings instead of being fetched or silently dropped. |
| Markdown (`.md`) | Headings, tables, code, local images and Mermaid diagrams. |
| CSV (`.csv`) | Detected comma, semicolon, tab or pipe dialect; quoted and multiline fields; first row repeats as the header. |
| TSV (`.tsv`) | Tab-separated fallback with the same quoting, header and wide-table behavior as CSV. |

Delimited text must be UTF-8 (a UTF-8 byte-order mark is accepted). Use `--no-header` when every row is data. Use `--sheets NAME [NAME ...]` for exact XLSX sheet names or one-based indexes; the default is all visible worksheets. Long narrow tables continue across body pages; wide tables use the same landscape, larger-sheet and custom-page ladder as figures.

For scripts, add `--json` to receive a structured result on stdout. The [automation guide](https://maxfield-allison.github.io/platefold/automation/) covers the result contract, exit codes and direct Python service.

Keep referenced images inside the selected input folder. For a single-file export, that boundary is the file's parent folder. References outside it are reported as missing. The browser blocks remote resources, and document scripts are disabled by the content policy; remote images must be downloaded into the source folder first.

External conversion uses a private temporary input copy, home/profile and output directory, an argument list without a shell, bounded diagnostics and temporary files, the export deadline, and process-tree cleanup on cancellation. Pandoc runs with its own `--sandbox`. LibreOffice safe mode and a private profile do not form a filesystem or network sandbox: use trusted legacy files or run Plate|Fold inside an operating-system sandbox or container. The [security policy](https://github.com/maxfield-allison/platefold/blob/main/SECURITY.md) describes this boundary.

| Option | What it changes |
| --- | --- |
| `--preset letter\|a4\|uniform-letter` | Body page geometry. `uniform-letter` keeps every page Letter portrait and scales wide figures down. |
| `--theme NAME_OR_FILE` | A discovered theme name or JSON file; `source` imports each usable DOCX or XLSX palette and requested typefaces, including from a converted legacy document. Conversion can change those details. |
| `--save-theme NAME` | With one DOCX or XLSX and `--theme source`, save a complete reusable user theme without replacing an existing name. |
| `--sheets SHEET [SHEET ...]` | Export exact XLSX sheet names or one-based indexes; `#2` always means the second sheet and `name:#2` the sheet named `#2`. Hidden sheets cannot be selected. |
| `--font NAME` | Font stacks for body text, headings and code. Default `sora`, the bundled Latin fonts. |
| `--config PATH` | The settings file to use. |
| `--dpi N` | Raster image resolution at the placed size. Default 300 DPI. |
| `--no-soft-breaks` | Collapse single newlines for hard-wrapped Markdown. |
| `--no-header` | Treat every CSV/TSV row as data; by default the first row repeats as the header. |
| `--no-toc`, `--no-bookmarks` | Omit the contents page or PDF outline. |
| `--clear-cache` | Render diagrams again. |
| `--timings`, `--quiet` | Show phase timings or suppress document progress. |
| `--json` | Write a versioned JSON result; progress goes to stderr. |

`--browser` accepts `auto`, `bundled`, `system`, `edge`, `chrome`, `chromium`, `brave`, or an executable path. Automatic selection tries a bundled or development-cache Chromium first, then installed browsers. `system` uses an installed browser that passes a rendering check and falls back to the bundled one. Browser versions and installed fonts can change line breaks and page counts, even with the same settings.

## Settings and themes

Put one `platefold.yml`, `platefold.yaml` or `platefold.json` at the input folder's root, or beside a single input file. If more than one settings file is present, choose with `--config`. CLI flags override file settings. TOML settings are not read: an `export.toml` beside the input stops the export with steps to convert it to YAML.

```yaml
page:
  preset: letter
theme:
  base: bristol
typography:
  font_set: arial
behaviour:
  image_dpi: 200
```

YAML and JSON share one set of sections and keys, checked against [settings.schema.json](https://github.com/maxfield-allison/platefold/blob/main/platefold/schemas/settings.schema.json). [platefold.example.yml](https://github.com/maxfield-allison/platefold/blob/main/platefold.example.yml) shows every setting with its default. Unknown keys and invalid values produce errors. The advanced `limits` section can lower the default resource ceilings for untrusted workloads, including OOXML entry-count and decompressed-size ceilings; [SECURITY.md](https://github.com/maxfield-allison/platefold/blob/main/SECURITY.md) describes what is bounded and how a stopped batch preserves completed output.

Ten themes are included: `bristol`, `bond`, `newsprint`, `graphite`, `midnight`, `carbon`, `default`, `slate`, `mono` and `forest`. The first three are light. Graphite, midnight and carbon are dark palettes for reading on a screen. Dark page backgrounds are included in print output too.

Custom themes are JSON files with a base palette and only the tokens you want to change. A source theme maps DrawingML colours onto the same tokens and requests the DOCX or XLSX major/minor typefaces when installed, while retaining portable fallbacks and never downloading fonts. See [THEMING.md](https://github.com/maxfield-allison/platefold/blob/main/THEMING.md) for discovery locations, examples and font behavior.

## How pages are chosen

Each figure is measured before placement; delimited-table width is estimated from its bounded cell content. The default ladder accepts it inline at 80% scale or greater, then tries landscape plates at 85% or greater, then a custom page capped at 900 mm wide. Table height does not force a wider page: long narrow tables keep flowing across body pages, and header rows repeat. `uniform-letter` disables the larger pages.

Mermaid renders to SVG in a headless browser. The diagram cache includes rendering settings because changing fonts or colors must invalidate old results. Documents with a contents page use a measuring print pass to find heading pages before printing the final page numbers. The contents number column stays fixed-width so filling it doesn't move the text.

For development setup and the rendering checks, see [CONTRIBUTING.md](https://github.com/maxfield-allison/platefold/blob/main/CONTRIBUTING.md).

## License

Plate|Fold is MIT-licensed. See [LICENSE](https://github.com/maxfield-allison/platefold/blob/main/LICENSE). Vendored Mermaid retains its license under `platefold/vendor`. Browser distributions carry their own licenses and notices.
