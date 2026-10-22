# Changelog

All notable changes to Plate|Fold are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

<!-- towncrier release notes start -->

## [1.0.1] - 2026-10-22

### Security

- Rebuild the bundled diagram renderer with patched DOMPurify and KaTeX dependencies, and require certificate and hostname validation for updater requests.


## [1.0.0] - 2026-10-20

First public release.

### Added

#### Input formats
- **Markdown**: headings, tables, code, local images and vector Mermaid diagrams, including files with Windows line endings.
- **Word (`.docx`)**: semantic headings, lists, tables, links, metadata and embedded images. A preformatted Mermaid fence stays a vector diagram. A short table row prints short, as in Word, and is tagged with the table's full width; a row Word indents keeps its cells in their columns. Where a malformed table's merged cells overlap, the later cell is narrowed to the columns left free, keeps its text and is reported.
- **Excel (`.xlsx`)**: each visible sheet becomes a section and table, with Excel number formats (fractions, zero padding, scaling, literals, sections and conditions), ISO dates, table headers, merged cells, embedded images and wide-sheet page routing. A number format outside the supported set shows the stored value and a warning instead of a rounded guess. `--sheets` selects sheets by exact name or `#` index, and `name:` forces an exact name. Hidden content is left out. Charts become a visible warning box, and external references and linked objects are never fetched.
- **CSV and TSV**: dialect detection, quoted and multiline fields, and a repeating header row. `--no-header` treats every row as data.
- **Legacy documents (`.doc`, `.odt`, `.rtf`)**: DOC converts through an installed LibreOffice, and ODT and RTF through LibreOffice or a sandbox-capable Pandoc, then read like DOCX. Neither converter ships with Plate|Fold.
- **XLSX times and durations**: fractional seconds are kept, as in `12:34:56.789`. Negative durations shorter than a second keep their sign, as in `-0:00:00.250`.

#### Page layout
- **Page ladder**: each figure is measured and placed inline, on a landscape plate, on a larger sheet or on a page sized to it, up to 900 mm wide.
- **Print mode**: keeps diagrams whole on Letter, A4 and other standard or plotter sheets. Tiled print keeps a diagram's size across sheets for assembly.
- **Tiled print assembly**: a configurable overlap (`tile_overlap`, 10 mm by default), trim and overlap marks outside the diagram, a label on every sheet with the figure and its row and column, and tile counts in export reports.
- **Paper and margins**: presets, custom sheet sizes, exact edge margins, fit thresholds, text size and image resolution, with paper chosen from the device locale unless set.
- **Contents and bookmarks**: a contents page with page numbers and a PDF outline, both optional.
- **Large tables**: documents of up to 1,000,000 table cells print in parts of 25,000 cells and join into one PDF with the pages a single pass would print.

#### Themes and fonts
- **Ten built-in themes**: light and dark palettes for print and screen. `platefold --list-themes` shows them.
- **Source themes**: `--theme source` uses a DOCX or XLSX document's own palette and requested fonts. `--save-theme` keeps the result as a reusable theme.
- **Custom themes**: JSON files that inherit from a base palette and change only the colours they name.
- **Contrast checks**: CI checks every built-in theme's colour pairs against the WCAG 2.1 AA contrast ratio. A user theme below that ratio warns when selected.
- **Fonts**: the bundled Sora and JetBrains Mono set is the default everywhere. The Calibri, Segoe, Cambria, Georgia, Arial, Apple and GNOME sets are available with `--font`. The app also lists installed font families, and export reports record which faces actually rendered.
- **Font fallback warnings**: a bounded scan attributes detected fallback characters to the face that printed them, or reports them as missing when no installed font has them. It warns when the check is incomplete or unavailable. The export report, JSON results and the desktop app list them by face, with counts, scripts and examples.

#### Desktop app
- **Windows, macOS and Linux**: a signed Windows installer (online and offline variants) and portable ZIP, a signed and notarized Apple Silicon DMG for macOS 15.1 or later, and a Linux AppImage.
- **Linux `.deb` for Ubuntu and Debian**: installs to `/usr` with the same desktop app, CLI and bundled Chromium as the AppImage, plus an AppArmor profile so that Chromium keeps its sandbox on Ubuntu 23.10 and later. Update by installing the next release's `.deb`.
- **Chromium's sandbox stays on**: where Ubuntu's AppArmor policy denies the bundled Chromium its sandbox, exports use an installed Chrome, Chromium, Edge or Brave that passes the rendering check. Without one, they stop with an error naming the cause and the supported fixes.
- **Live preview**: the first page renders with the export's fonts, colours and paper settings.
- **Theme manager**: create, edit, save, overwrite and delete user themes, with a live contrast readout for every colour pair.
- **Exports**: start in the Downloads folder, show progress and elapsed time, and can be cancelled mid-document, including while a large file is still being read. A failed or cancelled export keeps the last good PDF.
- **In-app updates**: off until you allow checks. Signed release discovery, with separate consent to download, install and restart, and every download checked for exact size, SHA-256 and a signature that names its version, platform and file before it can install. The portable ZIP is updated by hand.
- **Atomic app replacement**: an AppImage replaces its installed file in one rename, and a macOS app exchanges bundles atomically. The Linux updater uses the outer AppImage in a user-writable folder; administrator-managed copies require manual replacement. If the macOS volume cannot exchange bundles, PlateFold refuses the update and leaves the installed app unchanged.
- **Closing during installation**: a close normally waits for the attempt's result. After 60 seconds from the close request, PlateFold prevents further replacement and closes once no replacement or installer launch is running. A step already running must finish. On Windows, a close withdraws a requested restart only before the updater reads the restart choice for launch.
- **Update cleanup warnings**: for macOS and AppImage updates, the Updates dialog names an unused update folder if cleanup fails after a successful or failed installation attempt. A cleanup warning after success defers a requested restart so you can read it before restarting PlateFold.

#### Command line and automation
- **`platefold` command**: exports a file or a folder, preserving the folder structure, with every layout, theme and font option available as a flag or a settings file.
- **Settings files**: `platefold.yml`, `platefold.yaml` or `platefold.json`, checked against one published schema. Unknown keys are errors, and a TOML `export.toml` from a pre-release build stops the export with steps to convert it.
- **JSON results**: `--json` writes one versioned result object with stable exit codes for scripts.
- **Python package**: a wheel and source distribution that install with pip, with `platefold`, `platefold-gui` and `python -m platefold` entry points.

#### Server and container
- **Browser interface and HTTP API**: authenticated uploads, cancellation, recovery and downloads, with per-user limits.
- **Container images**: one multi-architecture image (amd64 and arm64) published to `ghcr.io/maxfield-allison/platefold` and `probablyfinedev/platefold` on Docker Hub. Each architecture passes the full container test, including Chromium's sandbox check, before release, and the published image is the exact one that was tested and scanned.
- **Deployment examples**: Docker Compose and Kubernetes, running as a non-root user with a read-only root filesystem and a seccomp profile.

#### Accessibility
- **Tagged PDFs**: document structure, language, table relationships and image and diagram descriptions carry into the PDF. Decoration is marked as artifacts, links are described, and the test PDFs are checked against veraPDF's PDF/UA-1 rules.
- **Keyboard use and assistive-technology semantics in the app**: focus recovery, progress announcements and text alternatives for the preview.
- **Display settings**: the app follows forced colours (Windows contrast themes), reduced motion and increased contrast. Ctrl+plus and Ctrl+minus zoom the desktop app, and the interface reflows into one column when zoomed.

### Security
- **Bounded inputs**: size, count and time limits on input files, archives, images, diagrams and output, which an operator can lower.
- **Image decoders**: Pillow 13.0.0 or newer is required; bundled builds pin the decoder wheels and verify their hashes.
- **Bounded Markdown**: a paragraph crowded with links, code spans or HTML tags, or HTML tags that never close, stops with a limit error naming the paragraph instead of taking minutes to convert.
- **Bounded Office documents**: DOCX and XLSX files are limited by XML elements and attributes, combined sheet area and repeated images, so a small crafted file stops with a limit error instead of exhausting memory.
- **No remote content**: the PDF renderer blocks network requests and document scripts, and image references cannot leave the source folder. LibreOffice conversion of legacy files runs outside that boundary, so use it only with trusted files.
- **Legacy conversion on servers**: the HTTP server refuses DOC, ODT and RTF unless its operator enables `legacy_conversion`, because LibreOffice is not a sandbox.
- **Server origins and credentials**: the HTTP server refuses a plain-http browser origin off loopback unless its operator passes `--allow-http-origin`, and refuses API tokens that are repetitive, sequential or drawn from a small alphabet.
- **Slow clients**: the HTTP server closes a connection that has not sent a complete request head within 10 seconds, and the documentation adds a reverse-proxy example and a Kubernetes NetworkPolicy.
- **Renderer sandbox reporting**: the server's capabilities report whether Chromium's sandbox was on in the most recent export, observed from its renderer processes.
- **Signed releases**: Windows and macOS packages are signed, update artifacts carry update signatures, and `SHA256SUMS`, which covers every download in the release, is signed with both minisign and Cosign. Container images on GHCR and Docker Hub are signed with the same Cosign key. The verification guide shows how to check each.

### Known limits
- Screen-reader output has not been checked with a native screen reader, and there is no PDF/UA conformance claim. The PDF carries no PDF/UA identifier.
- The legacy Tk window (`platefold-gui`) is not available to screen readers.
- The macOS app has signed-runtime and Gatekeeper automation but no hands-on interface walkthrough, and closing it during an export hasn't been tested on a real Mac.
- In-app updating has been tested from one installed version to the next on Windows and on Linux, with the AppImage in a user-owned folder, but not on macOS. The administrator-managed `/opt/platefold` installation on Linux is updated by hand.
- A macOS volume that cannot exchange app bundles requires a manual update. PlateFold refuses the in-app replacement, leaves the installed app unchanged and tells you to install the update by hand.
- A damaged image inside an SVG file or a stylesheet background prints as blank space without a warning.
- Under an Excel number format such as `0`, a negative value that rounds to zero prints as `-0`.
- A macOS app in a folder your account can't change, such as one another account installed, doesn't update itself; the Updates dialog shows the manual steps.
- The update metadata key can offer any signed release newer than the installed version, including one older than the latest. It cannot change or relabel the artifact. See [desktop update trust](https://github.com/maxfield-allison/platefold/blob/main/SECURITY.md#desktop-update-trust).
- Bundled Mermaid contains DOMPurify and KaTeX versions with known advisories whose prerequisites are absent from the configured rendering path. The affected dependencies remain bundled; see [security](https://github.com/maxfield-allison/platefold/blob/main/SECURITY.md#known-advisories-in-bundled-components).
- Pagination can differ between machines with different fonts or browser versions.
- There is no strict font mode. Characters the chosen fonts lack, such as CJK, Arabic or emoji with the bundled Latin fonts, print in another installed face, or as placeholder boxes when no installed face has them. Detected cases produce warnings, and an incomplete or unavailable check produces its own warning. These warnings don't stop the export.
- Dark themes print dark. The page colour is part of the PDF.
- In the default Online mode, SVG image files aren't measured, so they're placed at the text column's width. Print and Tiled print measure them.
- Plates aren't numbered separately from inline figures.
- Markdown footnotes, task lists, maths, wikilinks and callouts aren't supported.
- Excel charts become visible warning boxes rather than being redrawn. Use a static image in the worksheet instead.
- The interface is English only.
- Desktop packages are Windows x64, Linux x86-64 and Apple Silicon only. No Intel macOS, Windows on Arm, Linux arm64 desktop, `.rpm` or MSI packages.
