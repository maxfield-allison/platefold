# Accessibility

Plate|Fold writes tagged PDFs by default. Its structural test corpus verifies that authored headings, lists, links, table headers and image alternatives survive the final bookmark and document-property rewrite in the tested online, print and tiled paths. Converter fidelity and native screen-reader behavior remain separate checks.

Page backgrounds, borders and rules are marked as artifacts so readers skip them, bold and italic text is role-mapped to a standard span, an empty table cell keeps its place in its row, list items keep their content in a list body, every link carries a description (its address, or the heading or anchor it leads to), and the file includes XMP metadata with its title and language. When a document has no title heading, its contents page heading is level 1 so heading levels never skip.

The structural test PDFs are validated with veraPDF's PDF/UA-1 profile. Every machine-checkable rule passes except the PDF/UA identification entry, which Plate|Fold deliberately doesn't write. That entry is a conformance claim, and a validator can't check the parts of PDF/UA that need human judgement, such as whether the reading order makes sense or a description is useful. Profiles built on PDF 2.0 (PDF/UA-2 and WTPDF) report further differences, because the renderer writes PDF 1.x structure.

These are structural checks. Native screen-reader acceptance is still pending, and Plate|Fold doesn't claim PDF/UA or WCAG conformance. A file with tags can still have an incorrect reading order or an unhelpful description. Review the final PDF in the reader and viewer your audience uses.

## Language

Set **Document language** in the app, use `--document-language en-GB`, or add this to a settings file:

```yaml
behaviour:
  document_language: en-GB
```

Leave it empty to use source metadata. Markdown accepts `Language: en-GB` below its opening title. DOCX uses its default language or core language property. XLSX uses the workbook language property. DOC, ODT and RTF can inherit a language when the installed converter carries it into the temporary DOCX, but converter fidelity is not guaranteed. The structural tests verify authored DOCX run-language changes and HTML `lang` attributes through the final PDF rewrite. Document language is separate from interface language and automatic paper selection. Missing or invalid source language produces a warning and `und` (undetermined), rather than a guess based on your computer's locale.

## Images and diagrams

Write image alternatives for their purpose in the document. `![Description](image.png)` supplies the alternative. `![](image.png)` explicitly marks a decorative image. DOCX image descriptions and its decorative flag have the same meaning. Missing descriptions produce warnings. Plate|Fold doesn't invent them.

For Mermaid, supply an `accTitle` and `accDescr`:

```text
flowchart LR
    accTitle: Approval flow
    accDescr: Start proceeds to Review, then to Finish.
    A[Start] --> B[Review] --> C[Finish]
```

A tiled diagram exposes its full authored alternative once. The clipped copies remain visible for printing and assembly, but are excluded from the reading sequence. Give the description enough context to stand on its own. A title alone may be insufficient.

## Tables and application controls

Use real header cells and meaningful headings in the source. CSV/TSV uses its first row as headers unless you turn that option off. DOCX repeating header rows and merged cells retain their structure. Headerless tables produce a warning because Plate|Fold can't know which data should be a header. In print and tiled modes, tables paginate on the selected sheet while large diagrams use tiles in tiled mode. Tables that exceed the printable width are scaled to retain every cell; the export warns when this happens. Choose wider paper or online mode for larger text.

The app supports keyboard navigation, dialog focus return, export progress and result announcements. The first-page preview is a visual aid. Open the PDF to read the document.

## Display settings

The app and the browser interface follow these operating-system and browser preferences:

- **Contrast themes and forced colours** (Windows high contrast): the system palette replaces the app's colours, and buttons, the selected theme, focus and the preview progress bar stay visible. Theme swatches keep their own colours because they show a theme.
- **Reduced motion**: animations and transitions stop. The preview progress bar becomes a still bar.
- **Increased contrast**: secondary text reaches a 7:1 contrast ratio and field borders 3:1.
- **Zoom and small windows**: when the page is narrower than 560 CSS pixels or shorter than 400, the interface becomes one scrolling column. Zoom up to 400 percent keeps every control reachable without sideways scrolling. In the desktop app, Ctrl+plus and Ctrl+minus zoom the interface and Ctrl+0 resets it (Command on macOS); on Windows, Ctrl with the mouse wheel also zooms. Operating-system display scaling enlarges the app too.

Automated browser tests check each interface state in light, dark and increased-contrast appearances against the axe-core WCAG 2.2 A and AA rules. They also check that every control can be reached, isn't clipped or covered, and shows a visible focus indicator at each keyboard stop, under emulated forced colours, reduced motion, increased contrast and zoom. These are Chromium checks. They don't establish how NVDA, VoiceOver or Orca behaves in a packaged app, nor do they replace testing under the operating system's own settings.

The legacy `platefold-gui` window is built with Tk, which doesn't expose its controls to screen readers. It can be used with the keyboard alone: Tab moves between controls, Space or Enter presses a button, and the export log is read-only. Use the desktop app or the command line with a screen reader.
