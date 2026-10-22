# Windows desktop app

The Windows desktop app runs the same Python export service as the CLI. It includes the browser used to render PDFs and open fonts for portable fallback. Packaged exports don't require a separate Python or Node installation. Release installers are signed under the published Windows publisher identity and timestamped by Microsoft.

The app supports theme selection and editing, paper and font choices, progress, cancellation and opening completed PDFs.

## Install or unpack

Both Windows setup variants install the same application for the current user and can upgrade an existing installation. Close PlateFold before running setup; a manual uninstall is not needed. Choose the download that fits the machine:

- **Online setup** (`*-online-setup.exe`) reuses an installed WebView2 UI runtime. If it is missing, setup downloads and installs it from Microsoft, so keep an internet connection available during installation.
- **Offline setup** (`*-offline-setup.exe`) carries the WebView2 installer for machines without internet access. It also reuses a suitable installed WebView2 runtime.

Both include the tested browser used to render PDFs. Online setup is smaller because it leaves out the offline WebView2 installer; it does not depend on a user's web browser for PDF export. The unpacked app folder uses the system WebView2 runtime, normally present on Windows 11. Keep the executable and its `runtime` folder together.

Setup uses zlib compression to favour extraction speed over download size. Installation time also depends on disk speed, executable scanning and whether WebView2 needs installation.

The unpacked download is portable application code. It still stores themes in `%APPDATA%\platefold\themes` and diagram caches in `%LOCALAPPDATA%\platefold\diagram-cache`. Desktop jobs use separate temporary caches and remove them after completion; forced termination can leave a cache folder behind. It doesn't keep user data beside the executable.

## First export

1. Start `platefold-desktop.exe`, or PlateFold from the installed shortcut.
2. Choose a DOC, DOCX, ODT, RTF, XLSX, Markdown, CSV or TSV file, or a folder containing them. DOC, ODT and RTF work only when a compatible LibreOffice or Pandoc is already installed; Plate|Fold does not include either. You can also expand **Enter source path**, paste its path and select **Use path**. For delimited text, leave **First CSV/TSV row is a header** selected unless every row is data. XLSX exports every visible worksheet by default; use a settings file's `behaviour.sheets` list when the desktop export should select specific sheets.
3. Choose an existing output folder, or expand **Enter output path**, paste its path and select **Use path**.
4. Select a theme, paper size and font set. **Source document** imports each DOCX or XLSX palette and requested heading/body fonts. A converted DOC, ODT or RTF can expose the temporary DOCX palette, but conversion may change or lose those details; incomplete themes and other inputs use the configured fallback. Source fonts are requested from the installed system with portable fallbacks and are never downloaded. **Sora + JetBrains Mono** embeds the bundled Latin fonts. Other sets use installed fonts and may paginate differently across machines.
5. Select **Export PDFs**. The result lists page counts and any missing images or diagram errors. **Open PDF** opens a completed document in its associated application.

After you choose a source, the centre pane renders its first PDF page with the selected settings. For a folder, it previews the first document. Changes to the theme, paper or fonts refresh that page. The sample palette remains available before a source is selected. Inspect the complete exported PDF before distributing it.

Cancellation can interrupt the document currently being read or rendered and keeps completed PDFs. Closing the app during an export immediately terminates its owned worker and PDF browser process tree. A failed or interrupted document is staged separately, so it cannot replace an earlier good PDF or report.

Expand **Customise palette** to edit colours for the next export, with a contrast verdict beside each colour. **Save as a copy…** creates a new user theme without changing a built-in or replacing an existing name. **Overwrite**, **Delete** and **Revert** work on your own themes, and built-in and project themes need a copy first. [Themes](guides/themes.md#themes-in-the-desktop-app) has the details. Application appearance and document theme are separate choices. Settings files beside the source documents remain supported. The choices in the interface override their corresponding export settings.

## Installed fonts and browser reuse

The font menu groups presets separately from **Installed on this computer**. **Refresh fonts** picks up newly installed or removed families. With **Source document**, leave **Use document fonts** selected to follow its theme, or choose an installed font to override it. A saved font choice remains selected if the font disappears; rendering uses the bundled fallback and reports a substitution. Fonts available only inside another application, such as an undownloaded Office cloud font, may not be available to PlateFold.

Export results include **Fonts used**, with requested and rendered families for representative body, heading and code text. Characters those fonts lack, such as CJK, Arabic or emoji, appear in the document's warnings with the face that printed them. See [fonts and pagination](guides/fonts.md#fallback-warnings).

**PDF renderer** defaults to the included renderer. **Use a compatible installed browser** checks a fresh private browser profile for tagged PDF output, mixed page sizes, text, local fonts and cancellation. It uses the included renderer if installed engines fail the check. This does not read browsing history or change the user's browser settings. Both installers keep the included renderer; an engine-free installer is not part of this release.

## Updates

The **Updates** dialog is off by default. When enabled, it checks the stable channel or an explicitly selected prerelease channel, verifies signed metadata and the exact downloaded artifact, and asks separately before installation and restart. [Desktop update checks](guides/updates.md) explains the controls and distribution-specific behavior.

## Uninstall

Close PlateFold, then open **Settings → Apps → Installed apps**, find **PlateFold** and choose **Uninstall**. For the unpacked app, delete its folder. Your themes and diagram cache stay; [Windows](get-started/windows.md#uninstall) lists every folder to delete for a complete removal.

## CLI in the desktop package

The CLI is included at `runtime\platefold.exe`. The installer doesn't change your PATH.

```powershell
& .\runtime\platefold.exe .\documents -o .\pdfs --font sora --json
& .\runtime\platefold.exe --list-themes --json
```

See [Automating exports](automation.md) for the result contract. The desktop worker uses versioned JSON messages over local pipes. It doesn't open a network server.

## Build and check

Building needs 64-bit Python 3.11 or newer with the packages in `requirements-build.txt`, which pin PyInstaller, plus Node.js 22, Rust and the Windows C++ build tools. Close running copies before rebuilding. The recipe downloads locked frontend dependencies and bundles the Chromium already downloaded for the Python renderer.

```powershell
python -m playwright install chromium
python tools/build_desktop.py
```

The online setup, offline setup, unpacked ZIP, `SHA256SUMS` and `BUILD-RECEIPT.json` appear under `dist/desktop`. The frozen Python/PDF runtime is built once and shared by both installers. The receipt records each installer's WebView2 policy and its own installed GUI hash. The native executable and runtime also appear under `desktop/src-tauri/target/release`. `--windows-installers online` or `--windows-installers offline` builds one setup variant; the default builds both. `--no-installer` builds the application and ZIP without an installer.

To verify the bytes of an existing installation against a receipt containing both setups, pass `--installed-directory <folder> --installer-variant online` (or `offline`) to `tools/check_windows_package.py`. Payload verification is separate from running the installer and checking upgrades or offline behavior.

The ordinary pytest suite covers the real worker protocol and built frontend. Build `desktop/dist` before running the UI tests. On Windows, this additional check drives the packaged application through its path-entry controls and interrupts a second export to check worker cleanup:

```powershell
python tools/smoke_windows_desktop.py desktop/src-tauri/target/release/platefold-desktop.exe tests/fixtures/wide_diagram.md C:\Temp\platefold-proof
```

Use a dedicated output directory: the check creates synthetic interruption fixtures and partial exports there. The Windows release path also has recorded clean installation, native picker, export cancellation, uninstall, upgrade and settings-preservation checks. A source-workstation smoke test alone does not establish those results for a changed package.
