# Desktop update checks

The desktop **Updates** dialog is available in builds from current source. Checks are off by default. Select **Allow manual update checks**, choose **Stable** or opt into **Prerelease**, then select **Check for updates**. Allowing checks does not schedule background requests. Turning them off prevents subsequent checks; a request already underway can take up to 15 seconds to finish, and its result is discarded.

Release builds pin the update service and separate metadata and artifact trust keys in the application. The service is two signed files, `stable.json` and `prerelease.json`, at `https://maxfield-allison.github.io/platefold-updates/`, published from a repository that holds nothing else. They are signed again every day to keep them within their seven-day validity. If the service is unavailable, the app reports the failed check and leaves the installed version unchanged. A newer installer by itself is not evidence that in-app updates work.

The checker verifies the signature on release information before showing a version, release notes and artifact identity. It rejects expired information, older versions, unpublished releases and releases for another channel, platform, architecture or package type. Release notes display as plain text. Checking is unavailable during an export; an export that starts during a check discards its result.

The metadata signature authenticates the release description. It does not mean that an artifact has been downloaded. For a configured, qualified self-updating package, **Download and verify** is a separate action. PlateFold then checks the Tauri artifact signature, exact signed size and SHA-256 before enabling installation. The artifact signature must also name the offered version, platform and file, so the release information cannot present an older signed installer as a newer version. Release information is fetched without following redirects. An artifact download follows at most two HTTPS redirects, and only to GitHub's release-asset hosts, because GitHub serves every release download that way. Replacing the application and restarting each need separate consent. OS signing/notarization and provenance remain separate checks; see [Verifying downloads](../project/verification.md).

## How each distribution updates

| Distribution | Current behavior |
| --- | --- |
| Windows installer, Linux AppImage, macOS app | Signed discovery, separately approved download/verification, installation and restart. A Linux AppImage in a folder your account can't change, such as the administrator-managed `/opt/platefold` installation, shows the manual update steps instead of offering a download. So does a macOS app your account can't change, for example one another account installed; PlateFold never asks for an administrator password to replace itself. |
| Portable or unrecognized desktop package | Verify the downloaded release checksum and signature, close the app, and replace its application files. Keep the previous copy for recovery. |
| Linux `.deb` | The app checks the signed release information and, when a newer version exists, shows it with its release page. It never downloads or replaces anything itself. Download the new `.deb` from that page, check it against `SHA256SUMS`, and install it with `sudo apt install ./platefold_<version>_amd64.deb`. Settings and themes are kept. |
| Python CLI | Use the Python package/environment manager that installed it. |
| Browser and container | No desktop update controls. The operator deploys the replacement server or container. |

Update preferences are stored separately in the desktop application's configuration directory as `updates.json`. Saving them does not change document settings or themes. An unreadable or malformed preference file disables checks; saving a new choice recovers it.

Exports and update download/installation exclude one another. Cancelling or interrupting a download never preserves partial bytes. If an installation failure permits a retry, PlateFold keeps the verified download in memory for retry or cancel; restarting the app discards it. A refused macOS bundle exchange clears the pending download and offers no retry. A startup warning after interruption is recovery guidance, not proof that the operating system rolled back an interrupted package replacement. The native older-to-newer procedure in the repository's `packaging/UPDATES.md` covers real package replacement.

## Closing during an update

A close or quit during installation is recorded, and the status line says why the app stays open. PlateFold normally closes after the installation attempt finishes. After 60 seconds from the close request, it prevents further replacement and closes once no replacement or installer launch is running. If one of those steps is already running, it waits for the step to finish. The 60-second limit does not guarantee an exact closing time, and stopping the wait does not undo an update already installed.

On Windows, a close also withdraws a requested restart if it arrives before the updater reads that choice while preparing the installer launch. It cannot change launch parameters already prepared.

<a id="if-the-app-is-missing-after-an-update"></a>

## If an update is refused

PlateFold stages the new copy beside the installed one. An AppImage replaces the installed file in one atomic rename. A macOS app exchanges the staged and installed bundles atomically. Both keep the previous or the new copy at the installed path throughout replacement.

If the macOS volume cannot exchange the bundles, PlateFold refuses the update without moving the installed app. The app remains usable, and the Updates dialog tells you to install the update by hand. Retrying in the app on the same volume will be refused again.

To update by hand, quit PlateFold, download and verify the new release's DMG, then drag its app to Applications and choose **Replace**. Your themes and settings are kept.
