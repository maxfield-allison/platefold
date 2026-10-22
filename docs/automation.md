# Automating exports

The installed `platefold` command accepts DOC, DOCX, ODT, RTF, XLSX, Markdown, CSV and TSV files or folders. DOC, ODT and RTF require a compatible operator-installed converter and are first converted to DOCX; Plate|Fold does not bundle one. Python callers can use the export service directly. All entry points run the same renderer used by the desktop interface.

```console
platefold ./documents -o ./pdfs
platefold ./documents -o ./pdfs --json > result.json
platefold --list-themes --json
```

With `--json`, stdout contains one JSON object. Progress, timing output and diagnostic warnings go to stderr. Add `--quiet` to suppress progress. Help and version requests keep their ordinary text output.

The result has `schema_version: 1`, a `command`, a `status` and an `exit_code`. Export results also contain the output directory, report path, elapsed seconds, number of source documents and the full export report. The report records the rendering engine, platform, selected settings, per-document effective theme/font stacks, page counts, missing images, diagram errors, warnings, the faces that rendered representative text (`font_resolution`) and the characters printed in fallback fonts (`font_fallback`). In Tiled print, each document's `tiled_figures` lists every tiled figure with its figure number, columns, rows, sheet count and scale; see [printing and assembling tiles](guides/tiles.md#export-reports). The output directory and report path describe the machine running the export. They aren't portable download URLs.

The report, which is also written beside the PDFs as `_export-report.json`, leaves out the folder paths of the machine that ran the export. Document files are named relative to the exported folder. Each document's `theme_source` says where its theme came from: `builtin:<name>`, `user:<name>`, `project:<path>` for a theme file inside the exported folder, `document:<path>` for a theme imported from the document itself with `--theme source`, or `file:<file name>` for a theme file elsewhere. With `--save-theme`, `saved_theme` holds the new theme's `name` and `theme_source`. When a warning or error message quotes a path inside the exported folder, the output folder, the diagram cache, the settings folder, the application, the temporary folder or the home folder, that folder is written as `<source>`, `<output>`, `<cache>`, `<config>`, `<application>`, `<temporary>` or `~`. Text taken from the document, such as a missing image's reference, is reported as written apart from those folders, so an absolute reference elsewhere stays visible.

| JSON-mode exit code | Meaning |
| --- | --- |
| `0` | Completed, possibly with warnings |
| `1` | Export failed: an input document couldn't be read, or a runtime or I/O error occurred |
| `2` | Cancelled safely. The result includes documents completed before cancellation |
| `64` | Invalid command-line arguments or settings file |
| `130` | Interrupted with Ctrl+C. A complete result report may not exist |

Missing images, failed diagrams and warnings, including font fallback, don't stop the export. The export status is `completed_with_warnings`, with details in `report.documents`. Check that status if your workflow requires a clean export. A failure object instead contains `error.code` and `error.message`. Additional fields may be added within schema version 1, so consumers should ignore fields they don't use.

Each document's `font_fallback` object lists the characters its requested fonts don't have:

| Field | Meaning |
| --- | --- |
| `status` | `complete`, `partial` when the check stopped at its limit, or `unverified` when the renderer couldn't report |
| `characters_checked` | Distinct visible characters checked, counting an emoji sequence or a letter with its combining marks as one |
| `fallbacks` | One entry per requested face and the face that printed its missing characters |
| `fallbacks[].font` | The face that sets the text, such as `Sora` |
| `fallbacks[].fallback` | The face that printed those characters, or `null` when no installed font has them and they print as placeholder boxes |
| `fallbacks[].characters`, `occurrences` | Distinct characters, and how often they appear |
| `fallbacks[].scripts` | Unicode scripts of those characters, most frequent first, such as `Han`, `Arabic` or `Emoji` |
| `fallbacks[].sample` | Up to 12 of the characters, in document order |

Each entry also produces one `FONT_FALLBACK` or `FONT_GLYPHS_MISSING` warning in the document's `warnings`. Face names are the ones the renderer reports on the exporting machine, so they differ between Windows, macOS and Linux.

The Python service is synchronous:

```python
from platefold.config import make_settings
from platefold.service import ExportRequest, export

request = ExportRequest(
    input_path="documents",
    output_dir="pdfs",
    settings=make_settings("a4"),
    diagram_cache="work/diagram-cache",
)
result = export(request)
print(result.as_dict())
```

It accepts `on_progress(done, total, relative_path, record)` and `should_cancel()` callbacks. A progress record is `None` before a document starts and a result dictionary after it finishes. Cancellation is checked while each document is read, between major rendering operations and while waiting for the browser; a document interrupted before publication is omitted from the result. The service returns results without printing or parsing arguments. Input errors raise `ValueError`, and runtime errors propagate to the caller.

PDFs and `_export-report.json` are written to temporary files in their destination directories, validated, then atomically replaced. A cancelled or failed replacement leaves an existing good output intact. Callers should still avoid having multiple exports target the same output directory concurrently.

Each call owns its browser. Concurrent workers must have separate output and cache directories, or take turns accessing shared ones. The desktop uses a versioned worker protocol over local pipes; the optional [HTTP server](guides/server.md) provides isolated authenticated jobs. These examples run locally and don't start a network service.

The desktop limits each worker response to 16 MiB, including its newline. If progress diagnostics exceed that limit, the event keeps the file and counts and records that diagnostics were omitted. If the final result is too large to display, the desktop gives the location of the full report and completed PDFs. The files on disk remain complete, and the worker can accept another job.
