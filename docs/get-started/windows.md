# Windows

There are two ways to run Plate|Fold on Windows: the desktop app, from a signed installer or a portable ZIP, or the Python package run from a copy of the repository. The desktop app needs no Python, Node.js or separate browser. Recorded Windows tests cover installation, preview, export, cancellation, upgrade and uninstall.

## Install the desktop app

The desktop app runs on 64-bit Windows on an x64 processor. There is no Windows on Arm build. The [Windows desktop app](../windows-desktop.md) page has the full details of each download and of the app itself.

### Download and check it

Each release has three Windows downloads. Pick one:

| Download | Choose it when |
| --- | --- |
| `PlateFold_<version>_x64-online-setup.exe` | You have an internet connection during installation. Setup downloads Microsoft's WebView2 runtime only if it is missing. |
| `PlateFold_<version>_x64-offline-setup.exe` | The machine has no internet access. It carries the WebView2 installer. |
| `PlateFold_<version>_windows-x64.zip` | You want a portable folder instead of an installed app. It uses the WebView2 runtime already on the machine, which Windows 11 normally has. |

Download the release's `SHA256SUMS` as well, put it beside your download, and in PowerShell run, with your file's name:

```powershell
Get-FileHash .\PlateFold_1.0.3_x64-online-setup.exe -Algorithm SHA256
```

Compare the `Hash` value with the line for that file in `SHA256SUMS`; case doesn't matter. [Verifying downloads](../project/verification.md) also shows how to check the signature on `SHA256SUMS` itself.

The setup programs are signed. In File Explorer, open the setup file's **Properties → Digital Signatures**; the signer is **Maxfield Allison**, with a Microsoft timestamp.

### Install and open it

1. Close PlateFold if an earlier version is running.
2. Run the setup program. It installs for your account only, into `%LOCALAPPDATA%\PlateFold`, and needs no administrator rights. It upgrades an existing installation in place, so you don't need to uninstall first.
3. Open **PlateFold** from the Start menu.

Microsoft Defender SmartScreen may warn about an unrecognized app even when it has a valid signature. Reputation and signature validity are separate checks. Confirm the download's hash and its **Properties → Digital Signatures** entry as described in [Verifying downloads](../project/verification.md). Choose **More info** and check the publisher; proceed with **Run anyway** only after those checks pass and it shows Maxfield Allison. If the hash or signature fails, or the publisher is unknown or different, don't run the file.

For the ZIP, extract the whole folder and run `platefold-desktop.exe` inside it. Keep the executable and its `runtime` folder together.

### Export something

Choose a source file or folder, keep or change the output folder, which starts as your Downloads folder, pick a theme, paper and font set, and select **Export PDFs**. [Your first export](first-export.md#in-the-desktop-app) walks through it with the sample document.

The app's command-line tool is `runtime\platefold.exe` in the installation or ZIP folder. The installer doesn't add it to your `PATH`:

```powershell
& "$env:LOCALAPPDATA\PlateFold\runtime\platefold.exe" --version
```

### Update

The app's **Updates** dialog is off until you allow checks. When allowed, it checks for a newer signed release when you ask, and then asks separately before it downloads and verifies the update, before it installs and before it restarts. [Desktop update checks](../guides/updates.md) explains each step. You can also download and run the next release's setup program yourself; it upgrades in place.

The ZIP doesn't update itself. Check the new release's ZIP, close PlateFold and replace the folder's contents, keeping the old copy until the new one works.

### Uninstall

Close PlateFold, then open **Settings → Apps → Installed apps**, find **PlateFold** and choose **Uninstall**. For the ZIP, delete its folder.

Uninstalling keeps your documents, exported PDFs, themes and diagram cache. The uninstaller's **Delete the application data** option removes only the app's update choices and its WebView2 data. To remove everything, also delete these folders:

| What | Where |
| --- | --- |
| Your themes | `%APPDATA%\platefold` |
| Diagram cache | `%LOCALAPPDATA%\platefold\diagram-cache` |
| Update check choices and WebView2 data | `%APPDATA%\dev.probablyfine.platefold` and `%LOCALAPPDATA%\dev.probablyfine.platefold` |

The diagram cache lives in the same `%LOCALAPPDATA%\PlateFold` folder the installer uses, which is why that folder can remain after uninstalling.

## Run from source

These steps run Plate|Fold from a copy of the repository in PowerShell. They give you the `platefold` command and the older Tk interface, `platefold-gui`, and need Python and an installed browser.

### 1. Install Python and a browser

Install Python 3.11 or newer from [python.org](https://www.python.org/downloads/windows/). The standard installer includes Tk, which `platefold-gui` uses.

Windows 11 already has Microsoft Edge, and Plate|Fold tries it first among installed browsers. Chrome, Chromium and Brave also work.

### 2. Create an environment and install

From the repository folder:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e .
```

If PowerShell refuses to run the activation script, allow local scripts for your user once with `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`, then activate again.

### 3. Check it runs

```powershell
platefold --version
platefold --list-themes
```

The first command prints the version, for example `Plate|Fold 1.0.3`. The second lists the ten built-in themes and eight font sets.

### Where Plate|Fold keeps things

| What | Where |
| --- | --- |
| Your themes | `%APPDATA%\platefold\themes` |
| Diagram cache | `%LOCALAPPDATA%\platefold\diagram-cache` |
| Playwright's Chromium, if you downloaded one | `%LOCALAPPDATA%\ms-playwright` |

Nothing is written beside the installed package, so reinstalling doesn't touch your themes. The desktop app and a source install share the themes and diagram cache folders.

Next: [your first export](first-export.md).
