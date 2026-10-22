# Your first export

The first export uses the sample document from this documentation, `ladder-tour.md`: one Markdown file with four Mermaid diagrams of increasing width. In a copy of the repository it is `docs/samples/ladder-tour.md`. Without one, copy the complete text under [The source](../reference/sample.md#the-source) on the sample page into a plain-text file named `ladder-tour.md`. Any document of your own works too.

## In the desktop app

The desktop app shares its interface across Windows, macOS and Linux. Native behavior and test coverage differ; see [release status](../project/status.md).

1. Open **PlateFold**.
2. Under **Source**, select **Choose file** and pick `ladder-tour.md`. **Choose folder** exports every supported file in a folder and its subfolders instead. You can also expand **Enter source path**, paste a path and select **Use path**.
3. Under **Destination**, the output folder starts as your Downloads folder. Select **Choose output folder** to use another existing folder.
4. Keep the default theme, paper and **Sora + JetBrains Mono** font set for now. The centre pane previews the first page with those settings.
5. Select **Export PDFs**. The status line shows progress, and **Cancel export** stops it. When it finishes, **View results** lists each PDF with its page count and any warnings, and **Open PDF** opens one in your PDF viewer.

The sample gives a seven-page PDF with four diagrams: three of the diagrams were too wide for the text column, and each got a page of its own, chosen by the [page ladder](../guides/placement.md). The app writes the same `_export-report.json` into the output folder as the command line does; the rest of this page describes it.

## From the command line

From a source install, run these in the repository folder with your environment activated. The Linux `.deb` puts `platefold` on your `PATH`; use the path of your saved `ladder-tour.md` in place of `docs/samples/ladder-tour.md`. The Windows and macOS desktop apps carry the same command inside the app; the [Windows](windows.md#export-something) and [macOS](macos.md#export-something) pages give its path.

### Export one file

```console
platefold docs/samples/ladder-tour.md -o pdfs
```

Plate|Fold prints one line per document, the engine it used and a summary:

```text
7p  figures=4 ladder-tour.md
Rendered with bundled Chromium (HeadlessChrome/151.0.7922.34)

1 document, 7 pages in 2.6s → /home/you/notes/pdfs
```

Your browser version and timing will differ. Open `pdfs/ladder-tour.pdf`: four diagrams on seven pages, because three of the diagrams were too wide for the text column and each got a page of its own, chosen by the [page ladder](../guides/placement.md).

The default `sora` font set uses the Latin fonts bundled with the package, so the export doesn't depend on which fonts your system has. `--font calibri` and the other sets use installed fonts instead. Your PDF can still differ from the [published sample](../reference/sample.md) in where lines break; [fonts and pagination](../guides/fonts.md) says why.

### Export a folder

Point Plate|Fold at a folder and it exports every supported file in it and its subfolders, keeping that structure under the output directory. Files it doesn't handle are skipped, and links to folders aren't followed.

```console
platefold notes -o pdfs
```

A folder can mix formats. Four documents of four kinds export in one run:

```text
1p  figures=2 brief.docx
7p  figures=4 ladder-tour.md
1p  figures=0 report.csv
1p  figures=0 sub/workbook.xlsx
Rendered with bundled Chromium (HeadlessChrome/151.0.7922.34)

4 documents, 10 pages in 1.5s → /home/you/notes/pdfs
```

Two sources whose names differ only by extension would both want the same PDF. Plate|Fold stops before writing anything and names the pair rather than letting one overwrite the other:

```text
Source files map to the same output PDF: report.csv and report.docx
```

Leaving out `-o` writes to a `PDF Export` folder beside the input folder. For a single file, that's beside the folder holding the file, one level up. Pass `-o` to choose.

Every run also writes `_export-report.json` into the output folder. It records the engine, platform, the settings used, and each document's page and figure counts, missing images and diagram errors.

## What a problem looks like

Here is a file with an image that doesn't exist and a Mermaid block with a syntax error:

````markdown
# Notes

![A chart](charts/missing.png)

```mermaid
flowchart LR
  a -->
```
````

The export still finishes, and says what went wrong:

```text
1p  figures=0 notes.md  MISSING IMAGES: 1  DIAGRAM ERRORS: 1
DOCUMENT_LANGUAGE: Document language is not set or was not recognized. Set the document language to en for English, or to the language your document uses. This helps screen readers pronounce the PDF correctly.
  To set it: add --document-language en, or the tag for your document's language, or set behaviour.document_language in a settings file. Markdown can also declare it with a "Language: en" line below the title.
Rendered with bundled Chromium (HeadlessChrome/151.0.7922.34)

1 document, 1 pages in 0.8s → /home/you/notes/pdfs
1 referenced image is missing from the PDF — see _export-report.json
1 diagram failed to render — see _export-report.json
```

The second and third lines are a separate complaint: this file never said what language it is in, so the PDF is tagged `und`. That gives reading software no declared document language to guide its automatic voice selection; a reader can still choose a language or voice manually. [Set the document's language](../guides/input-formats.md#set-the-documents-language) to remove the warning.

The PDF has a visible warning box where each missing piece should be, and the report names them:

```json
"missing_images": ["charts/missing.png"],
"diagram_errors": ["Error: Parse error on line 4:"]
```

The exit code is still `0`. A script that needs a clean export should check the [JSON result](../automation.md)'s `status` for `completed_with_warnings`.

## Change the look

```console
platefold docs/samples/ladder-tour.md -o pdfs --theme newsprint
platefold docs/samples/ladder-tour.md -o pdfs --preset a4
platefold --list-themes
```

`--theme` picks a palette, `--preset a4` switches the body pages to A4, and `--list-themes` shows every theme and font set Plate|Fold can find. To keep choices with a set of documents, put them in a [settings file](../reference/configuration.md) instead of repeating flags.

In the desktop app, the theme, paper and font menus make the same choices, and the preview follows them.
