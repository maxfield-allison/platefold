# Linux

There are two ways to run Plate|Fold on Linux: the desktop app, from a `.deb` or an AppImage, or the Python package run from a copy of the repository. The desktop packages need no Python, Node.js or separate browser.

## Choose a package

Each Linux release has two x86-64 packages with the same desktop app, CLI and bundled Chromium:

| Package | Choose it for | How it updates |
| --- | --- | --- |
| `.deb` | Ubuntu (23.10 and later, 24.04) and Debian 12 or 13 | Install the next release's `.deb` |
| AppImage | Other distributions, or running without installing | From the app, when the AppImage is in a folder you own |

**On Ubuntu, choose the `.deb`.** Ubuntu 23.10 and later let only programs covered by an AppArmor profile create the user namespaces Chromium's sandbox needs. The `.deb` installs a profile for its own bundled Chromium, so exports work with the sandbox on and nothing else installed. An AppImage run from your home folder has no profile. Both packages need glibc 2.36 or newer, so Ubuntu 22.04 is not supported.

## Install the .deb

Check the download against the release's `SHA256SUMS` (see [Verifying downloads](../project/verification.md)), then install it with apt, using the file name you downloaded:

```console
sha256sum --check --ignore-missing SHA256SUMS
sudo apt install ./platefold_1.0.3_amd64.deb
```

Open **PlateFold** from the application menu, or run `platefold-desktop`, then follow [your first export](first-export.md#in-the-desktop-app). The `platefold` command is the CLI, and the `.deb` puts it on your `PATH`. To update, install the next release's `.deb` the same way; the app's **Updates** dialog tells you when one exists but does not download it. `sudo apt remove platefold` removes the program and its AppArmor profile and keeps your themes and settings. `LINUX-INSTALL.md`, which comes with each Linux release, has the details, including an administrator-managed AppImage at `/opt/platefold`.

CI installs the exact `.deb` in clean Ubuntu 24.04 and Debian 12 containers, runs its CLI, starts its desktop program, then removes and purges it and checks that nothing is left. A container cannot load AppArmor policy, and the CI host restricts user namespaces as Ubuntu does, so there the `.deb`'s own Chromium gives the coded sandbox refusal, and an Ubuntu 24.04 run exports through the fallback described next. The `.deb` has not yet been walked through on an Ubuntu desktop, where its profile is loaded.

## Use the AppImage

Keep the AppImage in a folder your account owns, such as `~/Applications`: the app can update an AppImage only where your account can replace the file. Check it against the release's `SHA256SUMS`, then install and start it, using the file name you downloaded:

```console
sha256sum --check --ignore-missing SHA256SUMS
mkdir -p ~/Applications
install -m 0755 PlateFold_1.0.3_linux-x86_64.AppImage ~/Applications/PlateFold.AppImage
~/Applications/PlateFold.AppImage
```

The AppImage adds no menu entry and no `platefold` command; start it from that path. Then follow [your first export](first-export.md#in-the-desktop-app). On Ubuntu 23.10 and later, read the next section first.

To update, allow checks in the app's **Updates** dialog. When a newer signed release exists, the app asks separately before it downloads and verifies it, before it replaces the AppImage file and before it restarts; see [desktop update checks](../guides/updates.md). To update by hand, check the new release's AppImage and install it over the old file the same way. To remove it, quit PlateFold and delete the file. Both keep your themes and settings, listed under [where Plate|Fold keeps things](#where-platefold-keeps-things).

## If the sandbox is refused

With the AppImage on Ubuntu 23.10 and later, AppArmor can deny the bundled Chromium its sandbox even when the desktop opens. Plate|Fold then looks for an installed Chrome, Chromium, Edge or Brave that passes its rendering check and exports with that, naming it in the report. If none is installed, the export stops with an error saying the sandbox is unavailable and pointing to the `.deb` and `LINUX-INSTALL.md`. Plate|Fold never turns the sandbox off. CI checks the refusal and the fallback on an AppArmor-restricted Ubuntu host. Running from a user-owned folder with Google Chrome or Ubuntu's Chromium snap is not yet qualified on an Ubuntu desktop.

## Run from source

Browser exports on Linux run in CI as an unprivileged user with Chromium's sandbox on. These steps run Plate|Fold from a copy of the repository.

### 1. Install Python and a browser

You need Python 3.11 or newer with `venv`. On Debian and Ubuntu:

```console
sudo apt install python3 python3-venv python3-tk
```

`python3-tk` is only for `platefold-gui`. The CLI doesn't need it.

For the browser, either install Chrome, Chromium, Edge or Brave so its command is on your `PATH`, or download a Chromium into Playwright's cache after the next step.

### 2. Create an environment and install

From the repository folder:

```console
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

To use Playwright's Chromium instead of an installed browser:

```console
python -m pip install playwright
python -m playwright install chromium
```

Plate|Fold finds that download on its own. Playwright only downloads the browser.

### 3. Check it runs

```console
platefold --version
platefold --list-themes
```

The first command prints the version, for example `Plate|Fold 1.0.3`. The second lists the ten built-in themes and eight font sets.

## Where Plate|Fold keeps things

| What | Where |
| --- | --- |
| Your themes | `$XDG_CONFIG_HOME/platefold/themes`, or `~/.config/platefold/themes` |
| Diagram cache | `$XDG_CACHE_HOME/platefold/diagram-cache`, or `~/.cache/platefold/diagram-cache` |
| Playwright's Chromium | `$XDG_CACHE_HOME/ms-playwright`, or `~/.cache/ms-playwright` |
| Desktop app's update check choices (`updates.json`) | `$XDG_CONFIG_HOME/dev.probablyfine.platefold`, or `~/.config/dev.probablyfine.platefold` |
| Desktop app's web view data | `$XDG_DATA_HOME/dev.probablyfine.platefold`, or `~/.local/share/dev.probablyfine.platefold` |

The desktop packages and a source install share the themes and diagram cache folders. Removing a package, or deleting the AppImage, leaves all of these in place; delete the folders to remove them. A relative `XDG_CONFIG_HOME` or `XDG_CACHE_HOME` is ignored in favour of the default under your home folder.

## Fonts

A minimal system may have none of the faces the Office-style font sets ask for. Their stacks end with Carlito, Liberation and DejaVu, which have different metrics and change pagination. `--font sora` uses the Latin fonts bundled with the package.

Next: [your first export](first-export.md).
