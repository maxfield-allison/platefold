# Configuration

Settings let a folder of documents carry its own page size, theme, fonts and layout thresholds, so every export of it uses the same settings without repeating flags.

The tables on this page are generated from the package when the site is built. Types and allowed values come from `platefold/schemas/settings.schema.json`, and defaults come from the code that applies them. If the two disagree, the documentation build fails.

## Where settings come from

Plate|Fold looks for one settings file beside the input: at the root of a folder export, or next to a single file. It recognises these names:

- `platefold.yml`
- `platefold.yaml`
- `platefold.json`

If there's more than one, the export stops and asks you to pick with `--config`. `--config PATH` also loads a file from anywhere.

YAML and JSON are two spellings of the same settings: the same sections and keys, checked against the same schema. TOML is not read. A leftover `export.toml` stops the export with conversion steps; see [moving from export.toml](#moving-from-exporttoml).

Values combine in this order, each overriding the one before:

1. Built-in defaults
2. The preset, if the file or `--preset` names one
3. The settings file
4. Command-line flags or explicit desktop overrides

`--theme source` is an explicit per-document selection and therefore overrides a settings file's `theme.base` when DOCX or XLSX ingestion finds a usable source theme. A converted DOC, ODT or RTF can expose its temporary DOCX theme through that same path, although conversion may change or lose the original details. The configured base remains the visible fallback for a missing or incomplete source theme and applies normally to Markdown/CSV/TSV in a mixed folder. An explicit named `--theme NAME` uses the named theme instead of importing source colours.

## A complete example

```yaml
page:
  preset: a4
  margin: 18

layout:
  inline_fit: 0.8
  plate_fit: 0.85
  max_custom_width: 600

typography:
  font_set: sora
  body_size_pt: 10.5

behaviour:
  soft_breaks: true
  sheets: [Summary, "Quarter 2"]
  toc_min_headings: 6
  image_dpi: 200

theme:
  base: bristol
  link: "#8B2545"
```

The same settings in JSON use the same sections and keys. Add `"$schema"` pointing at the settings schema if your editor validates JSON against it.

An unknown section, an unknown key or a value outside its allowed range is an error that names the problem.

## Presets

Paper presets are starting points. **Automatic** uses Letter body pages for Online and suggests Letter or A4 from your device locale for Print and Tiled print. You can also choose Letter, Legal, Tabloid, A5 through A0, or ANSI C through E. `uniform-letter` is the legacy option that disables larger pages. See [printing and placement](../guides/placement.md) for the output modes.

The command-line `--locale` option and HTTP `locale_hint` are paper-default hints only. They do not select the interface language or set the source document's language. `--ui-locale` controls command-line interface messages; source-document language and PDF semantics remain independent.

### Advanced layout

Open **Advanced layout** in the desktop to override sheet dimensions, exact margins, diagram fit thresholds, custom plate width, caption space, tile overlap, text sizes and raster DPI. The section starts collapsed and its fields start empty. Empty fields preserve document settings and preset defaults. An entered value stays in place when you change the paper preset; **Reset advanced overrides** clears those entries and returns to the document settings and selected preset. It does not edit the settings file. These desktop entries last for the current app session; use a settings file for a reusable setup.

`page.width` and `page.height` override the preset's dimensions **before orientation is applied**. For example, 230 × 340 mm with Landscape selected produces a 340 × 230 mm sheet. In Print and Tiled print, every page uses that size. Online uses it for body pages and can still add larger plates.

`page.margin_edges` sets exact margins in **top, right, bottom, left** order, in millimetres. It overrides the Full, None or Bottom/right margin selection on every page, including Online plates and custom pages. Running headers and footers are omitted with exact margins. The four numbers must be nonnegative and leave usable space for content and captions.

```yaml
page:
  preset: letter
  output_mode: tiled
  width: 230
  height: 340
  margin_edges: [0, 8, 12, 0]

typography:
  body_size_pt: 11

behaviour:
  image_dpi: 240
  mermaid_font_px: 18
  mermaid_wrap_px: 300
```

The same custom sheet and margins on the command line:

```bash
platefold diagram.md -o pdfs --output-mode tiled --preset letter \
  --page-width 230 --page-height 340 --margin-edges 0 8 12 0
```

`inline_fit` and `plate_fit` are minimum acceptable scales between 0 and 1, excluding zero. They decide when a whole figure moves onto a larger page; they do not enlarge figures. Plate fit and the custom plate width cap apply to Online only. Tiled print keeps figures at their rendered size, so the fit thresholds are disabled there. `tile_overlap` applies only to Tiled print; see [printing and assembling tiles](../guides/tiles.md). Changing Mermaid text size or wrap width changes the rendered diagram and its tile count. DPI controls raster downsampling, not vector quality.

The HTTP `settings` object accepts the corresponding flat names: `page_width`, `page_height`, `margin_edges`, `inline_fit`, `plate_fit`, `max_custom_width`, `caption_px`, `tile_overlap`, `body_size_pt`, `mermaid_font_px`, `mermaid_wrap_px` and `image_dpi`. Omit optional fields to use preset defaults. The HTTP image DPI range remains 36-600, with a default of 150; local exports default to 300. Server resource limits remain operator-owned. Export reports record the effective dimensions, margins and advanced layout values, and the tile count of each tiled figure.

## page

<!-- platefold:generate settings-page -->

## layout

These are the thresholds for [the page ladder](../guides/placement.md).

<!-- platefold:generate settings-layout -->

## typography

<!-- platefold:generate settings-typography -->

The font sets are listed in [fonts and pagination](../guides/fonts.md).

## behaviour

`sheets` is an optional list of exact XLSX sheet names or one-based workbook indexes. An empty list exports every visible worksheet. Prefix an index with `#` when a workbook also has a numeric sheet name. Hidden and chart sheets cannot be selected.

<!-- platefold:generate settings-behaviour -->

## theme

`base` picks a theme by name, or a path to a theme file relative to the settings file. Any colour token set here overrides that theme for this export only. See [themes](../guides/themes.md).

<!-- platefold:generate settings-theme -->

## limits

Resource ceilings for a single export. Lowering them reduces how much a hostile document can consume; raise one deliberately for an unusually large document.

<!-- platefold:generate settings-limits -->

Settings and theme files themselves have a fixed 1 MiB ceiling, because a file can't safely raise the limit used to read it. [Security](../project/security.md) describes what these limits cover and what they don't.

## Command-line equivalents

| Flag | Settings key |
| --- | --- |
| `--preset` | `page.preset` |
| `--theme` | `theme.base` |
| `--font` | `typography.font_set` |
| `--dpi` | `behaviour.image_dpi` |
| `--no-soft-breaks` | `behaviour.soft_breaks: false` |
| `--no-toc` | `behaviour.toc: false` |
| `--no-bookmarks` | `behaviour.bookmarks: false` |

The full flag list is in the [command-line reference](cli.md).

## Moving from export.toml

Builds before 1.0.0 read settings from a TOML file named `export.toml`. Plate|Fold no longer reads TOML. When it finds an `export.toml` beside the input, or is given a `.toml` file with `--config`, the export stops and the message explains how to convert it. It stops instead of carrying on because exporting with the default paper, theme and resource limits, when the file asked for others, would produce PDFs that look finished but are wrong.

The sections and keys did not change, so the conversion is mechanical:

- Write each `[section]` line as `section:`.
- Indent each `key = value` line under its section as `key: value`, keeping the value exactly as written. TOML strings, numbers, `true` and `false`, and `[...]` lists are already valid YAML, and quoted hex colours stay quoted.
- Save the result as `platefold.yml` in the same folder, then delete `export.toml`.

For example, this `export.toml`:

```toml
[page]
preset = "a4"
margin_edges = [0, 8, 12, 0]

[theme]
base = "mono"
accent = "#2f5d8c"  # comments carry over

[behaviour]
sheets = ["Summary", "#2"]
image_dpi = 150
```

becomes this `platefold.yml`:

```yaml
page:
  preset: "a4"
  margin_edges: [0, 8, 12, 0]

theme:
  base: "mono"
  accent: "#2f5d8c"  # comments carry over

behaviour:
  sheets: ["Summary", "#2"]
  image_dpi: 150
```

If Python 3.11 or later is installed, this command writes the same settings as `platefold.json` without comments:

```bash
python -c "import json, tomllib; json.dump(tomllib.load(open('export.toml', 'rb')), open('platefold.json', 'w'), indent=2)"
```

If `platefold.yml` or `platefold.json` already sits beside an `export.toml`, the message asks you to copy anything the TOML file still has and delete it.
