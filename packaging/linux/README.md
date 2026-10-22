# Linux installation

Plate|Fold for Linux x86-64 comes in two packages. Both contain the same desktop program, CLI, Python worker, Chromium and Latin fonts, and neither needs a separate Python or Node installation.

| Package | Use it for | Updates |
| --- | --- | --- |
| `platefold_<version>_amd64.deb` | Ubuntu 23.10 and later, Ubuntu 24.04, Debian 12 and 13 | Install the next release's `.deb` |
| `PlateFold_<version>_linux-x86_64.AppImage` | Other distributions, or running without installing | From the app, when the AppImage is in a folder you own |

On Ubuntu, use the `.deb`. It installs an AppArmor profile that lets the bundled Chromium keep its sandbox, which a user-owned AppImage does not get (see below). The `.deb` needs glibc 2.36 or newer, so Ubuntu 22.04 is not supported; the AppImage has the same requirement.

Check each download against the release's signed `SHA256SUMS` before installing it; see [Verifying downloads](https://maxfield-allison.github.io/platefold/project/verification/).

## Install the .deb

Start in the folder holding the `.deb` and `SHA256SUMS` from the same release, and replace the file name with the one you downloaded:

```sh
sha256sum --check --ignore-missing SHA256SUMS
sudo apt install ./platefold_1.0.0_amd64.deb
```

`apt` installs the WebKitGTK and GTK libraries the desktop program uses. The package installs:

- `platefold-desktop`, the desktop app, with a menu entry named PlateFold;
- `platefold`, the command-line tool;
- the runtime and bundled Chromium under `/usr/lib/PlateFold`;
- on systems whose AppArmor understands it (Ubuntu 23.10 and later, Debian 13), the profile `/etc/apparmor.d/platefold-bundled-chromium`. It grants the user-namespace permission to exactly `/usr/lib/PlateFold/runtime/_internal/platefold/browser/chrome-headless-shell`, which only root can replace. Debian 12's AppArmor cannot read the profile, and Debian 12 does not restrict user namespaces, so the package skips it there.

The package loads the profile when it installs. If loading fails, for example inside a container, the package says so and AppArmor loads it at the next boot. To add local rules, create `/etc/apparmor.d/local/platefold-bundled-chromium`.

Settings, themes and caches live in your home folder, as for the AppImage.

### Update the .deb

The Updates dialog in the app tells you when a newer signed release exists and shows its release page. It does not download or install anything for a `.deb` installation. Download the new `.deb` from that page, check it against the release's `SHA256SUMS`, quit Plate|Fold, and install it the same way:

```sh
sudo apt install ./platefold_1.0.1_amd64.deb
```

Settings and themes are kept.

### Remove the .deb

```sh
sudo apt remove platefold
```

This removes the program, the runtime and the AppArmor profile, and unloads the profile. `sudo apt purge platefold` also clears dpkg's record of the package. Neither touches your documents, exported PDFs, themes, caches or preferences.

## The AppImage and Ubuntu's Chromium sandbox restriction

On Ubuntu 24.04, the AppImage can open and its native picker can work while the bundled Chromium is refused its sandbox. PlateFold then uses an installed Chrome, Chromium, Edge or Brave that passes its rendering check, and the export report names that browser. Without one, preview and export stop with "Chromium could not start its security sandbox". The kernel log identifies `chrome-headless`, `userns_create`, and a denied `sys_admin` operation under `unprivileged_userns`. Installing FUSE does not resolve that browser error. PlateFold never runs Chromium with its sandbox turned off.

Read-only diagnosis:

```sh
sysctl kernel.apparmor_restrict_unprivileged_userns
sudo journalctl -k --since '10 minutes ago' | grep -E 'userns|chrome-headless'
```

Ubuntu allows an application-specific AppArmor profile to grant user-namespace access. The supplied profile attaches to one root-owned AppImage path. Its `unconfined` mode grants that application and its descendants user-namespace access without adding filesystem confinement. It is not a complete application sandbox; Chromium still supplies its own namespace and seccomp sandbox.

See Ubuntu's [user-namespace policy](https://ubuntu.com/blog/ubuntu-23-10-restricted-unprivileged-user-namespaces) and [AppArmor profile syntax](https://manpages.ubuntu.com/manpages/noble/man5/apparmor.d.5.html).

## Administrator-managed AppImage

On Ubuntu the `.deb` above is the simpler route. This recipe keeps the AppImage but gives it the same sandbox permission, for administrators who prefer a single file. The commands change the machine and require an administrator. Start in the directory containing the AppImage, `SHA256SUMS` and `platefold.apparmor` from the same build. Replace `APPIMAGE` with that build's actual filename. Review the profile before loading it.

```sh
APPIMAGE=PlateFold_1.0.0_linux-x86_64.AppImage
sha256sum --check --ignore-missing SHA256SUMS
sudo install -d -o root -g root -m 0755 /opt/platefold
sudo install -o root -g root -m 0755 "$APPIMAGE" /opt/platefold/PlateFold.AppImage
sudo install -o root -g root -m 0644 platefold.apparmor /etc/apparmor.d/platefold
sudo apparmor_parser -r /etc/apparmor.d/platefold
```

Use a fresh directory for first installation. If `/opt/platefold` or the profile already exists, inspect its ownership and contents before replacing anything. Close all PlateFold processes before an upgrade; preserve user settings and documents. Never make the installation writable by ordinary users or attach the profile to a wildcard covering user-controlled executables.

Launch as your normal desktop user, after quitting any earlier copy:

```sh
/opt/platefold/PlateFold.AppImage
```

The profile is selected when the program starts. An already-running copy, a copy launched from Downloads, or an extracted executable uses a different path. Choose a Markdown file and output directory, export, and inspect the PDF. Confirm the sysctl above still reads `1`. Do not use `--no-sandbox` or turn off the system-wide restriction to qualify this installation recipe.

On Linux distributions that already permit Chromium's sandbox, the executable AppImage can run from a user-owned folder without this administrative step. Ubuntu's restricted default is therefore a limitation of the administrator-free portable route. AppImage settings live in the user's application directories, not beside the executable. This recipe does not qualify other distributions or architectures.

## Update the AppImage

An AppImage in a folder your account can change, such as `~/Applications`, updates itself from **Updates** in the app after you approve each step.

The administrator-managed installation above can't update itself, because a normal user can't change `/opt/platefold`, and must not be able to. The app says so instead of offering a download. To update it, verify the new release as for a first installation, quit every PlateFold window, and replace the file. Set `APPIMAGE` to the new release's filename:

```sh
APPIMAGE=PlateFold_1.0.1_linux-x86_64.AppImage
sha256sum --check --ignore-missing SHA256SUMS
sudo install -o root -g root -m 0755 "$APPIMAGE" /opt/platefold/PlateFold.AppImage
```

The AppArmor profile attaches to the path, so it applies to the new file without reloading. Settings and themes are kept.

## Remove the administrator-managed AppImage

Quit PlateFold and close its PDF viewer first. Remove only this installation's profile and executable, then remove the empty application directory:

```sh
sudo apparmor_parser -R /etc/apparmor.d/platefold
sudo rm /etc/apparmor.d/platefold /opt/platefold/PlateFold.AppImage
sudo rmdir /opt/platefold
```

Inputs, exported PDFs, themes, caches and preferences are retained. Unloading the profile is part of removal; deleting its file alone leaves policy loaded until AppArmor reloads or the machine restarts.
