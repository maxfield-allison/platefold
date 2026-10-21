# Get started

There are two ways to run Plate|Fold.

**The desktop app.** The Tauri interface bundles its browser, Python runtime and CLI. System requirements and webview dependencies are covered in the platform guides below. Windows has signed online and offline installers plus an unpacked ZIP for x64, macOS has a signed and notarized DMG for Apple silicon and macOS 15.1 or later, and Linux has an x86-64 AppImage and a `.deb` for Ubuntu and Debian.

**From source, with Python.** Runs on Windows, macOS and Linux with Python 3.11 or newer and a Chromium-family browser. This gives you the `platefold` command and the older Tk interface, `platefold-gui`.

Both export Word, Excel, Markdown and delimited text on their own. DOC, ODT and RTF need LibreOffice or Pandoc already installed, because Plate|Fold never ships a converter. [Input formats](../guides/input-formats.md) covers all eight and what each one keeps.

Pick your platform. Each page covers the desktop app first, then running from source:

- [Windows](windows.md)
- [macOS](macos.md)
- [Linux](linux.md)

Then run [your first export](first-export.md).

## What running from source needs

| Requirement | Why |
| --- | --- |
| Python 3.11 or newer | The exporter is a Python package |
| A Chromium-family browser | Plate\|Fold prints through Chromium's PDF engine, and looks for Chrome, Chromium, Edge and Brave |
| A source checkout | The steps install from a copy of the repository |

Node.js isn't needed. Mermaid is vendored inside the package, and Plate|Fold talks to the browser over the DevTools Protocol directly. Playwright is only a convenient way to download a Chromium for development.

## How the browser is found

With the default `--browser auto`, Plate|Fold looks in this order and uses the first it finds:

1. A browser bundled with a packaged build.
2. When running from source, a Chromium downloaded by Playwright into its cache. Packaged builds skip this step.
3. An installed browser. Windows checks Edge, Chrome, Chromium, then Brave. macOS and Linux check Chrome, Chromium, Edge, then Brave.

You can name one instead, `--browser chrome`, or pass the path to an executable. The export report records which engine and version rendered each run, because browser versions can move line breaks and page counts.
