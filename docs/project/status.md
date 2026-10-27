# Release status

**Plate|Fold 1.0.3 is available.** The [release](https://github.com/maxfield-allison/platefold/releases/tag/v1.0.3) contains Windows installers, a portable Windows ZIP, Linux packages, an Apple Silicon DMG and Python packages, with checksums and signature evidence. Signed [Linux containers](../guides/container.md) are available for amd64 and arm64 on Docker Hub and GHCR. The published 1.0.0, 1.0.1 and 1.0.2 source tags remain available. Nothing on this page is a support commitment.

A platform is only listed as working when a real run on that platform has been recorded. Unit tests passing, or a package building, doesn't count on its own.

## Recorded 1.0.3 platform checks

These checks used the final 1.0.3 packages. The release manifest binds their exact source and delivered files; older package tests are separate historical evidence.

| | Windows | Linux | macOS |
| --- | --- | --- | --- |
| Export engine | Final packaged PDF export and source tests | Final Debian and administrator-managed AppImage PDF export; sandboxed container exports on amd64 and arm64 | Packaged signed-runtime smoke |
| Desktop package | Signed online/offline installers and unpacked ZIP | AppImage and `.deb`, each with embedded CLI and browser | Signed and notarized Apple Silicon DMG |
| Native acceptance | Standard-user installation, launch, PDF export, saved settings and removal | Debian GUI, three-page PDF export and removal with user data retained; administrator-managed AppImage GUI and export | Gatekeeper and packaged-runtime automation; native GUI walkthrough remains unqualified |
| In-app update path | Authenticated older-to-newer installation and manual relaunch with settings retained; invalid metadata, wrong hash, corrupt stream and interrupted-download rejection/retry | Authenticated older-to-newer replacement of a user-owned AppImage, manual relaunch and retained preferences; invalid metadata, wrong hash, corrupt stream, interrupted-download retry and safe staging refusal before replacement. The administrator-managed `/opt` installation and the `.deb` update by hand | Signed updater artifact verified; native update handoff remains unqualified |

"Run in CI" means browser integration tests export real PDFs as an unprivileged user, with Chromium's sandbox on. The Apple Silicon package is signed and notarized, but a native GUI/update walkthrough and closing during an export remain unqualified. Windows rollback after an installer has removed the previous version, portable Windows GUI use, missing-WebView bootstrap and automatic restart remain unqualified. Linux staging refusal was checked before replacement; failure after the final rename was not checked. Updating a user-owned AppImage does not qualify its bundled-browser rendering under Ubuntu's default sandbox policy.

The earlier 1.0.2 offline Windows candidate separately passed standard-user installation, PDF export, a saved-theme load after an operating-system restart, and uninstall with user data retained. SmartScreen showed an unrecognized-app reputation warning despite a valid publisher signature. Those historical checks do not establish 1.0.3 SmartScreen reputation or acceptance after an operating-system restart.

## Scope and limits

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
- Release tooling for SHA-256 checksums, source-bound build receipts, platform-signed packages, and signed update metadata and artifacts; package and feed publication remain separate release steps

## Known limitations

- **Ubuntu can block bundled-browser rendering from a user-owned AppImage.** Use the documented administrator-managed installation or Debian package; see [Linux installation](../get-started/linux.md). No sandbox bypass is needed for the qualified routes.
- **Update guidance can stay stale after installation-folder permissions change.** If you change those permissions while the update dialog is open, turn manual update checks off and on to refresh it.
- **Pagination isn't identical across machines.** [Fonts and browser versions](../guides/fonts.md) change line breaks.
- **No strict font mode.** Characters a font set lacks print in another installed font, or as placeholder boxes when no installed font has them. Detected cases are warnings in the export report, the JSON result and the desktop app. An incomplete or unavailable check also warns; none of these warnings stops the export.
- **Native accessibility acceptance is pending.** Tagged output and source semantics are implemented and structurally tested; screen-reader listening and native accessibility acceptance for the final packages remain open. See [accessibility](../guides/accessibility.md).
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
- **Renderer dependencies need continuing updates.** The 1.0.1 source bundles patched DOMPurify and KaTeX from the reviewed lockfile. See [bundled renderer dependencies](security.md#bundled-renderer-dependencies); a dependency audit is not a guarantee that every malicious document is safe.
- **Excel charts aren't redrawn.** Each one becomes a visible warning box in the PDF; paste a static image into the worksheet instead.
- **Some common Markdown extensions aren't supported.** Tables, fenced code with syntax highlighting, lists, attribute lists, HTML blocks and metadata lines work. Footnotes, task lists, maths, wikilinks and callouts don't.
