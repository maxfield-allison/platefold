# Theming

A theme supplies colors for body text, headings, tables, code and Mermaid diagrams. Fonts can be selected separately with `--font`, or supplied by a theme. Theme files contain data. Adding a palette doesn't require a stylesheet change.

## Built-in palettes

| Palette | Paper and accents |
| --- | --- |
| `bristol` | Ivory and amber |
| `bond` | White and burgundy |
| `newsprint` | Aged paper and sepia |
| `default` | White and blue |
| `forest` | Soft green and moss |
| `slate` | Cool grey and teal |
| `mono` | Black, white and grey |
| `carbon` | Dark charcoal and copper |
| `graphite` | Dark plum and violet |
| `midnight` | Navy and cyan |

Run `platefold --list-themes` for the available labels and font sets. Built-in names remain stable, but their colours can change between releases. Save a complete theme copy to keep a particular palette across upgrades; a sparse theme that inherits a built-in also inherits later colour changes.

Use `--theme source` with DOCX or XLSX to map its DrawingML palette and major/minor typeface requests onto these same tokens. This is a semantic re-layout, not a reproduction of Word pages or Excel grid chrome. Missing or incomplete source themes warn and use the configured named fallback. In a mixed folder, each OOXML file gets its own effective source theme while Markdown/CSV/TSV use the fallback. `--font` explicitly overrides imported fonts. Plate|Fold uses installed source fonts when present, retains portable fallback stacks, and never downloads them.

With exactly one DOCX or XLSX, `--theme source --save-theme organisation` creates a complete theme in the user theme directory. It refuses invalid, existing, linked or built-in names and publishes the finished JSON atomically.

Dark themes include a dark page background in the PDF. Use a light palette for documents headed to a printer unless that full-page fill is intentional. Warning boxes retain their own colors so a missing image or failed diagram stays visible.

## Make a custom theme

Save this as `docs/themes/blue-notes.json`:

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

Then run `platefold docs --theme blue-notes`. You can also pass a file directly: `platefold docs --theme themes/blue-notes.json`. A relative theme path is resolved from the input root.

Only overridden colors are stored. The rest inherit from `base`, so later corrections to that palette still reach your theme. A theme without `base` must provide every color token. Unknown tokens, invalid colors, missing bases and inheritance cycles are errors.

The schema is [platefold/schemas/theme.schema.json](https://github.com/maxfield-allison/platefold/blob/main/platefold/schemas/theme.schema.json). Names use lowercase letters, digits, underscores or hyphens and start with a letter. Colors use six-digit hex notation. `label`, `description` and `mode` describe the palette; `mode` is `light` or `dark`.

| Tokens | Used for |
| --- | --- |
| `page`, `surface`, `surface_alt` | Page background, table headers, code and alternate rows |
| `ink`, `ink_strong`, `ink_soft` | Body text, strong text, captions and metadata |
| `title`, `heading`, `link` | Titles, section headings and links |
| `accent`, `accent_soft`, `rule` | Accent marks and borders |
| `diagram_fill`, `diagram_stroke`, `diagram_text`, `diagram_alt_fill` | Diagram nodes, edges and labels |
| `diagram_note_fill`, `diagram_note_stroke` | Diagram notes |

## Where themes are found

Discovery reads these locations in order. A later definition wins when names collide, and the loader warns about the collision.

1. Package themes in `platefold/themes`.
2. The user's configuration directory, under `platefold/themes`.
3. `themes` beneath the input root.
4. A file explicitly selected with `--theme`.

User theme directories are `%APPDATA%\platefold\themes` on Windows, `~/Library/Application Support/platefold/themes` on macOS, and `$XDG_CONFIG_HOME/platefold/themes` on Linux, falling back to `~/.config/platefold/themes` when that variable is unset. Keep custom palettes in one of these writable locations so reinstalling the package doesn't replace them.

## Fonts and pagination

The font sets are `sora`, `calibri`, `segoe`, `cambria`, `georgia`, `arial`, `apple` and `gnome`. Each is a stack: the browser uses the first available face. The `sora` option embeds bundled Latin Sora and JetBrains Mono fonts and is the default on every surface. A theme can provide its own stacks with a `fonts` object containing all three keys, `body`, `head` and `mono`. A font set selected in settings or with `--font` takes precedence over the theme's fonts.

Font substitution changes text metrics. A document can gain a page, or a diagram can move to a larger plate, when exported on another machine. Even `arial` is a fallback choice rather than a guarantee of matching pagination. Glyphs outside the bundled Latin coverage can still use fallback fonts. The export names the faces used for detected fallbacks. If the check is incomplete or unavailable, it reports that too. There is no strict font mode that refuses the export. For repeatable exports, keep the browser version and fonts consistent and compare the resulting PDFs.

Windows exports have been tested locally and Linux exports in CI. The macOS package has automated signed-runtime export checks; its interface and in-app updating have not been tested by hand. Passing those checks does not establish identical pagination across platforms.
