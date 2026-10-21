# Things that will bite you

## Line breaks you didn't expect

**Single newlines are kept by default.** A line break inside a paragraph stays a line break in the PDF, the way Obsidian and many editors show it. Label and value pairs on consecutive lines, or a column of arrows between steps, depend on this.

If your Markdown is hard-wrapped at 80 columns, that same rule gives you ragged lines everywhere. Turn it off:

```console
platefold notes --no-soft-breaks
```

or in `platefold.yml`:

```yaml
behaviour:
  soft_breaks: false
```

## A screenshot got an enormous page

Raster images are measured in pixels, and one pixel counts as one CSS pixel. A 2400 pixel wide screenshot is treated as a figure about 635 mm wide, too big for either plate, so a 2400 × 900 pixel PNG gets a custom page of about 660 × 277 mm. Resize the image before exporting if it should sit in the text column, or export with `--preset uniform-letter` to keep every page Letter. More in [how figures are measured](placement.md#how-figures-are-measured).

## An image is reported missing, but the file is right there

Images have to be inside the input folder. For a folder export, that's the folder you named. For a single file, it's the folder holding that file. A reference that climbs out with `../`, an absolute path, or a link that resolves somewhere outside is reported as missing, even when the file exists.

Remote images aren't downloaded either. Save them into the source folder and reference the local copy.

## The export "succeeded" with broken images

Missing images and diagrams that fail to parse don't stop an export, and the exit code is still `0`. The PDF gets a visible warning box at each spot, the summary line says `MISSING IMAGES` or `DIAGRAM ERRORS`, and `_export-report.json` lists each one.

An image file that exists but can't be drawn counts as missing too. TIFF, BMP and other formats the browser can't draw are converted to PNG automatically; a file that is damaged, or in a format Plate|Fold can't read, such as HEIC, gets the same warning box and an `IMAGE_UNDECODABLE` warning naming it. The same warning names a TIFF embedded inside an SVG or a stylesheet, which can't be converted. Save the image as PNG or JPEG and export again.

One case is not reported. A damaged PNG, JPEG, GIF, WebP or AVIF inside an SVG file used as an image, or inside a stylesheet background, prints as blank space with no warning: Plate|Fold checks the nested image's format, but can't see whether the browser drew it. If such an image is missing from the PDF, open the SVG in a browser to check it.

In a script, check the [JSON result](../automation.md)'s `status`. `completed_with_warnings` means the export finished with warnings; inspect the report to see which ones.

## Pages moved between two machines

Different fonts or a different browser version change text metrics, which moves line breaks, page breaks and sometimes a diagram's plate. The bundled `sora` font set and a fixed browser version remove most of the difference; [fonts and pagination](fonts.md) covers the rest.

## A heading sits alone on a page

When a heading and a short paragraph come right before a wide diagram, the diagram goes to its own plate on the next page and the heading stays behind on a mostly empty body page. There's no setting for this yet. Add more text before the figure, or move the heading.

## No contents page

A contents page is only built when the document has at least nine headings across its top two heading levels. A short document skips it. Change the threshold with `toc_min_headings`, or turn contents pages off with `--no-toc`.

## A contrast warning about my theme

`ThemeContrastWarning: theme 'name' ... has N colour pairing(s) below WCAG 2.1 AA` means a custom or project theme has text or graphics that are hard to read against their background. The export still runs. The message names each pairing and its ratio; [contrast](themes.md#contrast) lists what's checked. Darken the text colour or lighten the ground until the pairing clears its minimum.

## A dark theme printed badly

`graphite`, `midnight` and `carbon` fill every page with a dark colour, including margins and plates. That's intended for reading on screen. Use a light theme for paper.

## Links in the PDF aren't clickable

Only `http:`, `https:` and `mailto:` links, plus links to headings in the same document, stay clickable. `file:` links, relative paths to other files and custom protocols remain as visible text without a link, and the export report warns when that happens.

## "Multiple settings files found"

Plate|Fold looks for `platefold.yml`, `platefold.yaml` or `platefold.json` beside the input. If more than one is there, it stops and asks you to choose with `--config`.

An unknown key or section is also an error, and the message lists the valid keys. In YAML, quote hex colours, because an unquoted `#` starts a comment.

## "Plate|Fold no longer reads TOML settings"

Builds before 1.0.0 read settings from `export.toml`. Plate|Fold now reads only YAML and JSON, and it stops rather than export with settings it would have to ignore. Convert the file as described in [moving from export.toml](../reference/configuration.md#moving-from-exporttoml), then delete `export.toml`. The message appears until the TOML file is gone.

## "No Chromium-family browser found"

Install Chrome, Chromium, Edge or Brave, download a Chromium with Playwright, or point at an executable with `--browser /path/to/chrome`. Plate|Fold looks in [these places](../get-started/index.md#how-the-browser-is-found).

## The output went somewhere odd

Without `-o`, output goes to a `PDF Export` folder beside the input folder. For a single file, that's beside the folder containing the file, one level above it. Pass `-o` to choose.

## Diagrams didn't pick up a change

The diagram cache is keyed on every setting that changes a diagram's appearance, so a theme or font change re-renders on its own. If you suspect a stale render anyway, `--clear-cache` renders every diagram again.

## Two exports clobbered each other

Each export writes PDFs and its report to temporary files and swaps them in when they're complete, so a failed export doesn't replace a good PDF. That doesn't make two exports into the same output folder at the same time safe. Give concurrent runs separate output and cache folders.

## An export stopped with a limit error

Plate|Fold caps document size, XML elements in Word and Excel files, spreadsheet area, image size and pixels, animation frames, embedded asset totals, Mermaid source and SVG size, PDF size and total export time, with the defaults in the [limits table](../reference/configuration.md#limits). A document that hits one stops with an explicit error, and PDFs already finished in the same batch are kept. Raise a limit deliberately in the `limits` section of a settings file. [Large tables](input-formats.md#large-tables) explains the table-cell limits and how large documents print in parts.

## Every export complains about the document language

```text
DOCUMENT_LANGUAGE: Document language is not set or was not recognized. Set the document language to en for English, or to the language your document uses. This helps screen readers pronounce the PDF correctly.
  To set it: add --document-language en, or the tag for your document's language, or set behaviour.document_language in a settings file. Markdown can also declare it with a "Language: en" line below the title.
```

The PDF is written and tagged `und` (undetermined). Reading software then has no declared document language to guide automatic voice selection, although a reader can still choose a language or voice manually. Markdown carries a `Language: en-GB` line below its opening title, and DOCX and XLSX use their own language properties. Converted DOC, ODT and RTF can inherit a language when the installed converter carries it into the temporary DOCX. CSV and TSV have nowhere to put one, so they warn on every run until you pass `--document-language en-GB` or set `behaviour.document_language` in a settings file.

Plate|Fold won't guess from your computer's locale, because the machine doing the export usually isn't evidence of what language the document is in.

## "Source files map to the same output PDF"

Two sources in a folder whose names differ only by extension, such as `report.csv` and `report.docx`, both want to become `report.pdf`. Rather than let one overwrite the other, the whole export stops before writing anything and names the pair. Rename one of them.

## The server won't start

Two startup failures come up most often.

```text
ValueError: Job storage must be an owned, private, unlinked absolute directory
```

The `--data-dir` exists and is reachable by the group or by everyone. The server creates a missing directory at mode 0700, but it won't adopt a loose one. `chmod 700` it and make sure the user running the server owns it. A volume mounted by Docker or a kubelet is the usual culprit.

```text
Invalid server configuration: Credential file must be owned by this user and private (0600)
```

The text after the colon describes the problem. The most common causes are a `--tokens-file` that isn't mode 0600, a token the server considers too predictable, and a plain `http://` origin on an address other than loopback. A file the server can't parse, such as invalid JSON, produces `Invalid server configuration; check credentials, origins, mounts and limits.` instead; check the file's permissions first, then its JSON, then `--origin` and any mounts.

## An Excel chart came out as a warning box

Charts aren't redrawn. `XLSX_CHART_UNSUPPORTED` means each chart in the workbook became a visible warning box. Paste the chart into the worksheet as a static image and export again; embedded images are retained.

## An Excel number came out unformatted

`XLSX_NUMBER_FORMAT_UNSUPPORTED` names each number format Plate|Fold did not apply. Those cells show their stored value, unrounded, rather than a guess at the format. [Number formats](input-formats.md#number-formats) lists what is supported; change the cell to a supported format, such as a fixed number of decimal places, and export again.

## `platefold-gui` won't start on Linux

It needs Tk, which many Linux Python installs ship separately. On Debian and Ubuntu, install `python3-tk`.
