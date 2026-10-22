# macOS

There are two ways to run Plate|Fold on a Mac: the desktop app, installed from a disk image, or the Python package run from a copy of the repository. The desktop app needs no Python, Node.js or separate browser.

!!! note "Apple Silicon release scope"

    The Apple Silicon desktop app is Developer ID signed, notarized, stapled and accepted by Gatekeeper. Its packaged worker and browser completed the automated signed-runtime smoke check on macOS 15.5. Nobody has yet walked through the app's interface by hand on a Mac, and in-app updating from one version to the next hasn't been tested on macOS, so macOS coverage is narrower than Windows. See [release status](../project/status.md).

## Install the desktop app

### What you need

- **A Mac with Apple silicon** (M1 or later). The app is built only for Apple silicon. There is no Intel build, and the app isn't universal.
- **macOS 15.1 or later.** The Python runtime inside the app is built for macOS 15.1, so earlier versions aren't supported.

The app includes its own PDF renderer (a headless Chromium), the Latin fonts of the default font set and a copy of the `platefold` command-line tool.

### Download and check it

From the release, download `PlateFold_<version>_macos-arm64.dmg` and the release's `SHA256SUMS`. The release also has a `PlateFold_<version>_macos-arm64.app.tar.gz`; that is the in-app updater's copy of the app, and you don't need it to install.

Put both files in one folder, open Terminal there and run:

```console
shasum -a 256 --check --ignore-missing SHA256SUMS
```

A match prints the DMG's name followed by `OK`. Any other result means the file isn't the one the release published; delete it and download it again. To also confirm that `SHA256SUMS` itself came from the release, check its signature as described in [Verifying downloads](../project/verification.md).

### Copy it to Applications

1. Double-click the DMG. macOS opens a window named **PlateFold** holding the **PlateFold** app and a shortcut to **Applications**.
2. Drag **PlateFold** onto **Applications**.
3. Eject the disk image from the Finder sidebar. You can then delete the DMG.

Run the app from Applications, not from the open disk image.

### Open it the first time

Open **PlateFold** from Applications or with Spotlight. macOS checks the app's signature and notarization the first time it opens. It may ask whether you want to open an app downloaded from the internet; choose **Open**. A stapled notarization ticket supports offline verification; other operating-system security checks may still use the network.

If macOS instead says that PlateFold is damaged, that Apple can't verify it is free of malware, or that the developer can't be verified, don't open it. These messages need investigation; they do not by themselves identify the cause. Don't use **Open Anyway** in System Settings, `xattr` or `spctl` to get past the check. Move the app to the Trash, download the DMG again, check it against `SHA256SUMS` and copy it to Applications again. If a checked download is still refused, report it on the [issue tracker](https://github.com/maxfield-allison/platefold/issues).

When you choose files or an output folder in Documents, Desktop or Downloads, macOS may ask whether PlateFold can access that folder. Allow it for the folders you export from and to. You can change the answer later in **System Settings → Privacy & Security → Files and Folders**.

### Export something

The app uses the shared export interface: choose a source file or folder, keep or change the output folder, which starts as your Downloads folder, pick a theme, paper and font set, and select **Export PDFs**. [Your first export](first-export.md#in-the-desktop-app) describes those controls with the sample document. The macOS interface walkthrough remains untested.

The app's command-line tool is inside the app bundle. It isn't added to your `PATH`:

```console
/Applications/PlateFold.app/Contents/Resources/runtime/platefold --version
```

It takes the same options as the `platefold` command described across this site.

### Update

The app's **Updates** dialog is off until you allow checks. When allowed, it checks for a newer signed release when you ask, and then asks separately before it downloads and verifies the update, before it replaces the app and before it restarts. If your account can't change the copy in Applications, for example because another account installed it, the dialog shows the manual steps instead: download the new DMG and drag the app to Applications again. [Desktop update checks](../guides/updates.md) explains each step.

To update by hand instead, quit PlateFold, download and check the new release's DMG as above, and drag its app onto **Applications**, choosing **Replace**. Your themes and settings live outside the app, so both ways keep them.

### Reinstall or uninstall

Quit PlateFold first. To uninstall, drag **PlateFold** from Applications to the Trash. To reinstall, do the same and then copy the app from the DMG again.

Removing the app leaves your documents, exported PDFs, themes and settings where they are. To remove those as well, delete these folders:

| What | Where |
| --- | --- |
| Your themes | `~/Library/Application Support/platefold` |
| Update check choices (`updates.json`) | `~/Library/Application Support/dev.probablyfine.platefold` |
| Diagram cache | `~/Library/Caches/platefold` |
| Data macOS keeps for the app's window, if present | `~/Library/WebKit/dev.probablyfine.platefold` and `~/Library/Caches/dev.probablyfine.platefold` |

`~/Library` is hidden in the Finder; choose **Go → Go to Folder…** and paste the path.

## Run from source

These steps run Plate|Fold from a copy of the repository in Terminal. They give you the `platefold` command and the older Tk interface, `platefold-gui`, and need Python and an installed browser.

### 1. Install Python and a browser

Install Python 3.11 or newer. The [python.org](https://www.python.org/downloads/macos/) installer includes Tk for `platefold-gui`. If you use Homebrew's Python, Tk comes from its separate `python-tk` formula for your Python version.

Install Chrome, Chromium, Edge or Brave into `/Applications` or `~/Applications`. Plate|Fold looks for the app bundle there, in that order.

### 2. Create an environment and install

From the repository folder:

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

### 3. Check it runs

```console
platefold --version
platefold --list-themes
```

The first command prints the version, for example `Plate|Fold 1.0.1`. The second lists the ten built-in themes and eight font sets.

### Where Plate|Fold keeps things

| What | Where |
| --- | --- |
| Your themes | `~/Library/Application Support/platefold/themes` |
| Diagram cache | `~/Library/Caches/platefold/diagram-cache` |
| Playwright's Chromium, if you downloaded one | `~/Library/Caches/ms-playwright` |

The desktop app and a source install share the themes and diagram cache folders.

## Fonts

The `apple` font set requests SF Pro for body text and headings and SF Mono for code, with fallback stacks. Documents exported with it on another platform fall back to other faces and can paginate differently.

Next: [your first export](first-export.md).
