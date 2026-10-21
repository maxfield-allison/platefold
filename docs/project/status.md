# Release status

**Plate|Fold 1.0.0 is the first public release.** Nothing on this page is a support commitment.

A platform is only listed as working when a real run on that platform has been recorded. Unit tests passing, or a package building, doesn't count on its own.

## What has been tested

| | Windows | Linux | macOS |
| --- | --- | --- | --- |
| Export engine | Packaged export and source tests | Browser export in CI | Packaged signed-runtime smoke |
| Desktop package | Signed online/offline installers and unpacked ZIP | AppImage and `.deb`, each with embedded CLI and browser | Signed and notarized Apple Silicon DMG |
| Native acceptance | Recorded tests: install, picker, preview, export, cancel/close, upgrade and uninstall | Standard-user Ubuntu preview plus package smoke in minimal Debian. The `.deb` installs, starts and purges cleanly in Ubuntu 24.04 and Debian 12 containers, and exports on Ubuntu 24.04 through the browser fallback; not yet walked through on an Ubuntu desktop | Gatekeeper and packaged-runtime automation |
| In-app update path | Native older-to-newer rehearsal, including tamper, interruption and recovery cases | Native older-to-newer rehearsal from a user-owned AppImage, including tamper, interruption and failed-replacement cases. The administrator-managed `/opt` installation and the `.deb` update by hand | Signed artifact check and packaged update artifact |

"Run in CI" means browser integration tests export real PDFs as an unprivileged user, with Chromium's sandbox on. Windows has the broadest hands-on coverage. The macOS package has signed-runtime evidence on Apple Silicon, but nobody has walked through its interface by hand, and closing the app during an export has not been observed on a real Mac. Linux has package and container evidence but less desktop interaction coverage.

## What is not part of 1.0.0

- Native screen-reader acceptance or a PDF/UA conformance claim
- Intel macOS, Windows on Arm, Linux on Arm desktop packages, `.rpm` or MSI packages. The Linux container supports amd64 and arm64.
- A promise that pagination matches across operating systems and font installations
- Commercial support or a compatibility service level

Planned work for some of these is on [in development](in-development.md).

## What's in place now

- An authenticated Linux [browser export interface](../guides/server.md), with upload, cancellation, recovery and downloads tested against real PDFs
- Exports from a source install, the CLI, JSON results, themes and settings files, as described across this site
- All eight [input formats](../guides/input-formats.md), DOC, DOCX, ODT, RTF, XLSX, Markdown, CSV and TSV, with source-palette and font import from DOCX and XLSX. Legacy conversion depends on an operator-installed LibreOffice or Pandoc, which Plate|Fold never bundles. Platform package qualification remains separate.
- An English interface catalog, with `--ui-locale` selection and English fallback. No other language ships, and none will be machine-translated; see [translating](translating.md).
- Resource limits and link handling, described in [security](security.md)
- Release checks for lint, unit, browser and desktop-shell tests, plus secrets and dependency vulnerability scans and signed package builds. Build and signing CI runs separately from the public source repository.
- SHA-256 checksums, source-bound build receipts, signed Windows and macOS packages, and signed update metadata and artifacts

## Known limitations

- **Pagination isn't identical across machines.** [Fonts and browser versions](../guides/fonts.md) change line breaks.
- **No strict font mode.** Characters a font set lacks print in another installed font, or as placeholder boxes when no installed font has them. Detected cases are warnings in the export report, the JSON result and the desktop app. An incomplete or unavailable check also warns; none of these warnings stops the export.
- **Native accessibility acceptance is pending.** Tagged output and source semantics are implemented and structurally tested; screen-reader listening and final packaged-app qualification remain open. See [accessibility](../guides/accessibility.md).
- **The legacy Tk window isn't available to screen readers.** Use the desktop app for the implemented accessibility semantics; its native acceptance remains pending.
- **Plates aren't numbered separately** from inline figures.
- **Dark themes print dark.** The page colour is part of the PDF.
- **SVG image files aren't measured in Online mode**, the default, so there they're planned at the text column's width. Print and Tiled print measure them from their width and height or `viewBox`.
- **Some macOS volumes require manual updates.** If the volume cannot exchange app bundles atomically, PlateFold refuses the update and leaves the installed app unchanged. Download and verify the new DMG, then replace the app by hand. See [updates](../guides/updates.md#if-the-app-is-missing-after-an-update).
- **Closing during installation can wait beyond 60 seconds.** After that interval from the close request, PlateFold prevents further replacement and closes once no replacement or installer launch is running. A step already running must finish. On Windows, a close withdraws a requested restart only before the updater reads the restart choice for launch. See [updates](../guides/updates.md#closing-during-an-update).
- **An app your account can't change doesn't update itself.** A macOS app another account installed, or the root-owned `/opt/platefold` AppImage, shows manual update steps instead of asking for an administrator password. See [updates](../guides/updates.md).
- **A damaged image inside an SVG file or a stylesheet prints blank without a warning.** Top-level images are checked; see [troubleshooting](../guides/troubleshooting.md#the-export-succeeded-with-broken-images).
- **An Excel value can print as negative zero.** Under a number format such as `0`, a negative value that rounds to zero prints as `-0`.
- **The update metadata key can select an older signed release.** It must still be newer than the installed version, but need not be the latest release. Artifact signatures prevent relabeling or changing the installer. See [update trust](security.md#desktop-update-trust).
- **Bundled Mermaid has known dependency advisories.** Their prerequisites are absent from the configured rendering path; the affected dependencies still remain in the bundle. See [security](security.md#known-advisories-in-bundled-components).
- **Excel charts aren't redrawn.** Each one becomes a visible warning box in the PDF; paste a static image into the worksheet instead.
- **Some common Markdown extensions aren't supported.** Tables, fenced code with syntax highlighting, lists, attribute lists, HTML blocks and metadata lines work. Footnotes, task lists, maths, wikilinks and callouts don't.
