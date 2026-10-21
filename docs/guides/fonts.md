# Fonts and pagination

A font set names three typefaces: one for body text, one for headings and one for code. Each is a stack, a list of faces in order of preference. The browser uses the first face in the stack that exists on the machine doing the export.

<!-- platefold:generate font-sets -->

The default is `sora`, the bundled set, on every surface: the desktop app, the browser interface, the HTTP API, the command line and the Python service. Choose another with `--font`, with `typography.font_set` in a settings file, or with a `fonts` object inside a theme. A font set from the command line or a settings file wins over a theme's fonts.

## Why the same file can paginate differently

Font metrics set line breaks, and line breaks set page breaks. Diagram labels are measured with the same fonts, so a figure's size and the page it lands on can move too. Swap in a font with slightly wider letters and a document can gain a page. That happens whenever a stack falls through to a later face. `calibri` asks for Calibri first, and on a machine without it the browser moves on to Carlito, then Liberation Sans, then Arial, each with different metrics. The Windows and Office sets end with Linux faces like these so a bare container still renders, and pages set in them won't match an export made on Windows.

## The bundled set

`sora` uses Sora and JetBrains Mono, which ship inside the package as Latin-subset web fonts under the SIL Open Font License 1.1. They're embedded in the PDF, running header and footer included, so every machine starts from the same faces.

Pagination can still differ between machines:

- Characters outside the Latin subset, such as CJK text, Arabic, emoji or many symbols, print in whatever face the system offers for them. The export [reports detected fallbacks](#fallback-warnings).
- A different browser version can shape and break text differently.

## Fallback warnings

A face can lack some of the characters a document uses. The browser then prints those characters in another face: a later one in the stack, or one the operating system picks. When no installed face has a character, it prints as a placeholder box.

Each export checks visible characters in the document and running header and footer against the fonts that print them, up to 20,000 distinct characters or 4,000 renderer queries. It reports detected fallbacks as follows. If the scan reaches a limit, it also reports `FONT_FALLBACK_INCOMPLETE`; if it cannot run, it reports `FONT_FALLBACK_UNVERIFIED`.

- `FONT_FALLBACK` names the requested face, the face that printed the characters, their scripts and a few examples, such as `Sora has no glyph for 6 characters (Han, Hiragana); they print in Noto Sans CJK SC: 漢 字 か な 標 題`. There's one warning per requested face and fallback face, however many characters are involved.
- `FONT_GLYPHS_MISSING` lists characters that no installed face has.
- `FONT_SUBSTITUTED` means the first face in a stack isn't available at all, so the whole text uses a later one.

The warnings appear in the export summary, in the result lists of the desktop app and browser interface, and in `_export-report.json` and `--json` output, where each document's `font_fallback` object holds the same details as data. See [automating exports](../automation.md) for its fields. The browser reports the face names, so the same document names different faces on Windows, macOS and Linux.

A warning doesn't stop the export; the status is `completed_with_warnings`. To choose the faces yourself, pick a font set, or an installed font in the desktop app, that covers the characters. Text inside images isn't checked. There's no strict font mode that refuses to export when a fallback face would be used.

## Getting repeatable exports

1. Keep the default `sora` set, or choose another set you know is installed everywhere the export runs.
2. Keep the browser version fixed. The export report's `engine` field records which one ran.
3. Compare the PDFs themselves. The report's page counts are a quick first check.

Desktop packages bundle Chromium, removing the renderer version as a variable between machines running the same build. A user may opt into a compatible installed browser after PlateFold checks it; that choice can produce different pagination.
