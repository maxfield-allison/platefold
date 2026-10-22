# Other tools

Plate|Fold picks a page size per figure. The tools below offer other export controls and workflows, and custom CSS or plugins can change their behavior. If your documents don't have figures wider than a text column, or you don't need a PDF, one of them may suit you better.

The linked project documentation describes the following. This comparison covers documented export controls, not every possible customization.

## Obsidian's PDF export

**Use it when** your notes already live in Obsidian and one page size for the whole note is fine.

[Obsidian includes PDF export](https://github.com/obsidianmd/obsidian-help/blob/master/Release%20notes/v1.1.md) for notes. If you're already using it, try the built-in export on a representative note before adopting another tool. Plugins and print CSS can change its behavior, so check their export support separately.

**Plate|Fold is the better fit when** a note mixes normal prose with diagrams too wide for the page, when you're exporting a folder of notes in one go, or when a script needs to run the export. Plate|Fold keeps soft line breaks by default, as Obsidian does, but it doesn't understand Obsidian's own syntax: wikilinks and embeds stay as literal text, and callouts render as ordinary blockquotes.

## Typora

**Use it when** you want to write and export in the same window, with a theme you can see as you type.

Typora is a commercial Markdown editor with Mermaid support and a [PDF export](https://support.typora.io/Export/#pdf) that sets paper size, margins, headers and footers with page numbers, and a theme. Export settings are global. A document's front matter can override them when the corresponding export preference is enabled.

**Plate|Fold is the better fit when** diagrams need bigger pages than the body text, or when exports need to run unattended from a command line or CI.

## md-to-pdf

**Use it when** you're in a Node.js toolchain and want Markdown to PDF with CSS you control.

[md-to-pdf](https://github.com/simonhaenisch/md-to-pdf) is an MIT-licensed Node.js command that prints through Puppeteer and headless Chromium. It takes a stylesheet, front matter and `pdf_options` for page format and margins, which apply to the whole document. Its documentation doesn't describe built-in Mermaid rendering.

**Plate|Fold is the better fit when** Mermaid diagrams should render without extra setup, and when wide ones need their own pages. Plate|Fold doesn't need Node.js.

## md2pdf-mermaid

**Use it when** you want a small `pip install` that turns Markdown with Mermaid and emoji into a PDF, and a single page size is enough.

[md2pdf-mermaid](https://github.com/rbutinar/md2pdf-mermaid) is an MIT-licensed Python package, version 1.4.3 as of December 2025. It renders Mermaid diagrams to PNG images through Playwright's Chromium, and takes `--page-size` (A4, A3 or Letter) and `--orientation` flags that apply to the whole document. It installs from PyPI.

**Plate|Fold is the better fit when** diagrams should stay vector in the PDF, and when one document needs portrait text pages and landscape or oversized figure pages together.

## Pandoc

**Use it when** you need [output formats other than PDF, citations, or a LaTeX typesetting pipeline](https://pandoc.org/MANUAL.html).

Plate|Fold only produces PDF, prints through a browser rather than LaTeX, and has no bibliography support.

## When not to use Plate|Fold

- You need an already qualified screen-reader workflow. Plate|Fold produces tagged PDFs, but [native accessibility acceptance](../guides/accessibility.md) is still pending.
- You need identical pagination on every machine. [Fonts and browser versions](../guides/fonts.md) still move line breaks.
- You need commercial support or a compatibility service level. Plate|Fold offers neither; see [release status](status.md).
