# Themes

A theme is the set of colours a document prints in: the paper, body text, headings, links, tables, code and every part of a Mermaid diagram. Themes are data. A new palette is a JSON file, and no stylesheet changes are involved.

Fonts are chosen separately, with `--font` or a settings file, and are covered in [fonts and pagination](fonts.md).

For DOCX and XLSX, `--theme source` maps the document's DrawingML palette and major/minor typeface requests onto the same tokens. A DOC, ODT or RTF converted through an operator-installed tool can expose the resulting temporary DOCX theme through the same path, but conversion may change or lose the original theme. Plate|Fold lays out the content again; it does not reproduce Word pages or Excel's grid chrome. Missing or incomplete themes warn and use the configured named fallback. In a mixed folder, source is resolved per OOXML or converted legacy document while Markdown/CSV/TSV use that fallback. An explicit `--font` overrides imported fonts; otherwise installed source faces lead portable fallback stacks and are never downloaded.

`platefold template.docx --theme source --save-theme organisation` saves a complete reusable user theme. The same works with one XLSX workbook. Saving requires exactly one DOCX or XLSX and refuses an existing, linked or built-in name.

## The built-in themes

Plate|Fold ships ten. The swatches are read from the package's theme files when this site is built, and show paper, body ink, headings, accent, links, diagram fill and diagram stroke.

<!-- platefold:generate builtin-themes -->

Built-in theme colours can change between versions. If you depend on a palette looking exactly as it does today, save a complete copy of it as your own theme. A theme that only overrides a few tokens inherits everything else from its base, including later changes to that base.

### Dark themes print dark

`graphite`, `midnight` and `carbon` are for reading on a screen. They set a dark page colour, and that colour is part of the PDF: every page, margin and plate is filled with it. Printing one uses a lot of ink or toner and looks wrong on most paper. Pick a light theme for anything headed to a printer.

Missing-image and failed-diagram warnings keep their own colours in every theme, so a problem stays visible on a dark page.

## Make your own theme

Save this as `themes/blue-notes.json` inside the folder you export:

```json
{
  "name": "blue-notes",
  "label": "Blue notes",
  "description": "The default palette with a deeper blue accent.",
  "mode": "light",
  "base": "default",
  "colors": {
    "accent": "#24507A",
    "link": "#24507A",
    "diagram_stroke": "#24507A"
  }
}
```

Then export with it:

```console
platefold notes --theme blue-notes
```

Or pass the file directly, `--theme themes/blue-notes.json`. A relative path is resolved from the input folder.

`base` is the theme to start from, and `colors` holds only what you change. Leave out `base` and the theme has to define every token itself.

The theme file is checked against [the theme schema](#the-rules-a-theme-file-follows) when it loads. Unknown tokens, colours that aren't six-digit hex, a `base` that doesn't exist and a chain of bases that loops back on itself are all errors, and the export stops with an error.

### A worked example: a print-safe dark look

To print a dark theme's look, put its accents on a light page. This one keeps `midnight`'s cyan on white:

```json
{
  "name": "midnight-paper",
  "label": "Midnight on paper",
  "description": "Midnight's accents on a white page, for printing.",
  "mode": "light",
  "base": "default",
  "colors": {
    "accent": "#0E7490",
    "heading": "#0E7490",
    "link": "#0E7490",
    "diagram_stroke": "#0E7490",
    "diagram_fill": "#E6F4F7"
  }
}
```

`mode` describes the palette and doesn't change any colours. Set it to match what the palette is, because the export report records it.

### A worked example: settings-file overrides

For a one-off change, you don't need a theme file at all. The `theme` section of a settings file picks a base and overrides tokens directly:

```yaml
theme:
  base: bristol
  link: "#8B2545"
  diagram_stroke: "#8B2545"
```

Quote hex colours in YAML. An unquoted `#` starts a comment.

## Where themes are found

Plate|Fold reads themes from four places, in this order. When two define the same name, the later one wins and Plate|Fold warns about the collision.

1. The built-in themes inside the package.
2. Your user theme folder.
3. A `themes` folder inside the input folder.
4. A file named directly with `--theme`.

| Platform | User theme folder |
| --- | --- |
| Windows | `%APPDATA%\platefold\themes` |
| macOS | `~/Library/Application Support/platefold/themes` |
| Linux | `$XDG_CONFIG_HOME/platefold/themes`, or `~/.config/platefold/themes` |

Keep personal palettes in the user folder. Reinstalling or upgrading the package doesn't touch it. A `themes` folder beside your documents is the better home for a palette that belongs to one project and should travel with it.

## Changing diagram colours re-renders diagrams

Rendered diagrams are cached, and the cache key includes the diagram colours. Change any `diagram_*` token and every diagram renders again on the next export. That's usually only slower. If the change also affects the fonts or sizes Mermaid measures, a diagram can land on a different page. See [how pages are chosen](placement.md#what-moves-when-you-change-something).

## Themes in the desktop app

The [Windows desktop app](../windows-desktop.md) edits themes directly. Open **Customise palette** to change colours, with each colour's contrast shown as you type.

- **Save as a copy…** and **New from current…** write a complete copy to your user theme folder.
- **Overwrite** rewrites one of your own themes with its full palette and removes its `base`, so it stops following later changes to that base. The pane says so before you click.
- **Delete** asks first and names the file it will remove. PDFs you already exported don't change.
- **Revert** throws away edits you haven't saved.

Built-in themes and project themes, the ones in a `themes` folder beside your documents, are read-only in the app and marked **EDITS NEED A COPY**. Choosing a dark theme, or editing a light theme's page colour dark, shows a note that dark pages suit screen reading and use a lot of toner on paper.

## Contrast

Plate|Fold checks each theme against the colour pairings its stylesheet and diagrams actually draw, using WCAG 2.1 AA: 4.5:1 for text, and 3:1 for the document title and for rules, borders and diagram edges. The table comes from the package's own rule list.

<!-- platefold:generate contrast-rules -->

Three hairline pairings are reported with their ratio and not scored, because a fill or text beside each one carries the same information. The warning callout keeps fixed colours in every theme and passes if either its border or its fill stands out from the page.

**Built-in themes** are held to every scored pairing by the test suite, so a built-in can't ship below AA.

**Your own themes** aren't rejected. When a custom or project theme with a pairing below AA is selected, the command line prints a `ThemeContrastWarning` to stderr naming each failing pairing, then exports anyway, and the result's status stays `completed`. The desktop app shows the same verdicts live. A `theme` section in a settings file is checked after it's merged with its base.

These checks cover colour contrast in the palette. They don't make an exported PDF accessible; see [release status](../project/status.md#known-limitations).

## Token reference

Every colour a theme can set. All take six-digit hex, like `#1D1D1F`.

<!-- platefold:generate theme-tokens -->

## The rules a theme file follows

| Field | Required | Meaning |
| --- | --- | --- |
| `name` | yes | Lowercase letters, digits, `_` or `-`, starting with a letter. This is what `--theme` takes |
| `label` | no | A display name |
| `description` | no | What the palette is for |
| `mode` | no | `light` or `dark` |
| `base` | no | A theme name to inherit from |
| `colors` | no | Token overrides, from the table above |
| `fonts` | no | Font stacks, with all three of `body`, `head` and `mono` |

The schema itself ships inside the package at `platefold/schemas/theme.schema.json`. Point an editor at it for completion and validation while you write a theme.
