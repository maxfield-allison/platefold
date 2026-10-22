# Signed desktop update contract

Release builds implement signed discovery and a Tauri installation adapter. `update-policy.json` pins independent production metadata and artifact public keys and the feed URLs. Publishing a signed feed entry remains a separate release action after the exact artifacts are available.

## Where the feeds are served

The stable and prerelease feeds are `https://maxfield-allison.github.io/platefold-updates/stable.json` and `https://maxfield-allison.github.io/platefold-updates/prerelease.json`. GitHub Pages serves them from the root of the `main` branch of their own repository, [maxfield-allison/platefold-updates](https://github.com/maxfield-allison/platefold-updates), which holds the two signed files, a README, the licence and a `.nojekyll` marker, and nothing else. This repository carries no feed, and its documentation site serves none.

Each feed is valid for at most seven days, so an automated job signs both again every day with the metadata key, changing only `issued_at` and `expires_at`, and commits them to that repository as a fast-forward. Its push credential can write to the feed repository alone. Whoever holds that credential can therefore change what the feed repository serves, but not this repository's source, history or releases, and the app refuses any feed the metadata key did not sign. The metadata key in turn decides only which already-signed release a feed offers and how long the offer is fresh: each installer's own signature names its version, update target and file (see [artifact release binding](#artifact-release-binding)), so a feed cannot offer an older build as a newer version. New versions reach the feeds only with a reviewed release.

## Trust and feed format

`packaging/update-policy.json` is compiled into the native application and included in the desktop build receipt's input hashes. It contains `public_keys` for metadata, `artifact_public_keys` for installers (one to four raw base64 Minisign public-key lines in each set), and `stable` and `prerelease` (HTTPS feed URLs or null). Frontend commands, local preference files and environment variables cannot replace this policy. Metadata and artifacts may use different trust roots. Configuring either is a reviewed release change after trust ownership is decided.

The service returns HTTP 200 with one document that is both PlateFold's signed envelope and a [Tauri static update response](https://v2.tauri.app/plugin/updater/#static-json-file). `payload` is a JSON **string** containing the exact UTF-8 bytes signed by the metadata key. `signature` is the full multiline detached metadata signature over those bytes. The outer `version`, `notes`, and every `platforms` URL and signature must exactly duplicate claims in the signed payload. PlateFold rejects an unsigned outer-field substitution before giving the response to Tauri. Do not parse and reserialize the payload between signing and verification. The complete response is at most 65,536 bytes.

The payload object has these fields; unknown fields are rejected:

```json
{
  "schema_version": 1,
  "version": "1.0.0",
  "channel": "stable",
  "published": true,
  "issued_at": 2000000000,
  "expires_at": 2000003600,
  "notes": "Release notes as plain text.",
  "artifacts": {
    "windows-x86_64-nsis": {
      "url": "https://example.invalid/releases/platefold-setup.exe",
      "sha256": "64 lowercase hexadecimal characters",
      "size": 123456,
      "signature": "base64 of the complete Tauri .sig file, bound to this release",
      "public_key": "raw Minisign public-key line pinned in artifact_public_keys"
    }
  }
}
```

The outer object also contains the Tauri fields:

```json
{
  "payload": "the exact JSON string above",
  "signature": "full metadata signature",
  "version": "1.0.0",
  "notes": "Release notes as plain text.",
  "platforms": {
    "windows-x86_64-nsis": {
      "url": "https://example.invalid/releases/platefold-setup.exe",
      "signature": "the same artifact signature as the signed payload"
    }
  }
}
```

These are schematic fragments, not a valid signed release. The artifact URL, SHA-256, byte size, artifact signature and selected pinned key must identify the feed's exact immutable artifact, and the artifact signature must name the payload's version, the target and the URL's file name. Do not regenerate or repackage an artifact after signing. Only set `published: true` once those exact bytes are available to the intended feed consumers. An isolated private qualification feed may use disposable test keys and test builds; those keys and builds must never enter the public stable feed. The prerelease feed may carry a published prerelease or stable release; choosing stable never accepts a prerelease version.

## Artifact release binding

Each artifact signature is a prehashed Minisign signature by a pinned artifact key whose trusted comment is exactly `platefold-artifact v1 version=<version> platform=<target> file=<file name>`, for example `platefold-artifact v1 version=1.0.0 platform=windows-x86_64-nsis file=PlateFold_1.0.0_x64-online-setup.exe`. Minisign's global signature covers the trusted comment, so only the artifact key can write it. The metadata key therefore cannot relabel an older signed build as a newer version, move a build to another target, or offer it under another file name.

The checker compares every listed artifact's comment with the payload's `version`, the artifact's target and the last path segment of its URL, and refuses a missing, older-format or different comment before offering the update. After the download, it verifies the signature over the exact bytes, which authenticates the comment, and compares the comment again before enabling installation. Tauri's own signature check on the downloaded bytes verifies the same global signature, so a rewritten comment fails both. Version, target and file are each a single token without spaces, so no two releases produce the same comment.

The release build signs the final, renamed updater artifacts with `desktop/src-tauri/examples/sign_updater_artifact.rs`, replacing the signature Tauri wrote during the build, whose comment carries only a timestamp and file name. The signer takes the artifact key from `TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`, refuses a key that is not pinned in `artifact_public_keys`, and never prints key material. `desktop/src-tauri/examples/verify_updater_artifact.rs --binding <version> <target> FILE SIG` then verifies each signature as the app does, and `tools/build_update_feed.py` refuses to sign a feed unless every artifact it lists verifies with that binding. The format is defined once, in `desktop/src-tauri/src/updates/binding.rs`, which the app and both examples share.

A package-manager installation never downloads through the app, so the notice that a newer version exists rests on the metadata signature alone; the package itself comes from the release page and its signed `SHA256SUMS`.

Targets use Rust OS and architecture names plus the actual Tauri bundle marker: for example `windows-x86_64-nsis`, `linux-x86_64-appimage`, `macos-aarch64-app`, `linux-x86_64-deb`, or `windows-x86_64-portable`. An absent/unrecognized bundle marker is portable; Windows alone does not imply an installation. Artifacts must match all three components. The packaging recipe stamps NSIS installers and restores the portable marker before archiving portable copies. `tools/build_deb.py` stamps the `.deb`'s program with the DEB marker.

A package-manager installation (`linux-*-deb` or `linux-*-rpm`) never replaces itself, so the feed need not list an artifact for it. When the signed metadata offers a newer version, the check returns that version, its notes and the release page (`https://github.com/maxfield-allison/platefold/releases/tag/v<version>`) with no artifact, and the app tells the user to download the new `.deb` from that page and install it with the package manager. Every other metadata check still applies; only the missing artifact is accepted. If a feed does list an artifact for the target, its URL and SHA-256 are shown, and still nothing is downloaded. Release builds therefore produce no updater signature for the `.deb`; the release's signed `SHA256SUMS` is its provenance.

Metadata expires within seven days of issuance. The checker allows up to five minutes of future clock skew, rejects expired metadata and compares SemVer precedence with the installed version. Build metadata on offered versions is forbidden. An equal version is current; a lower version is rejected. Expiry bounds replay but does not prevent a compromised feed from withholding updates or offering any previously signed release newer than the installed one, including one older than the latest. There is no persistent highest-seen-version ledger and no release-key-signed index yet, so that choice stays with the metadata key. Every artifact URL must be `artifact_url_prefix` from the compiled policy followed by `v<version>/<file>`; the metadata key cannot point a download at another host.

Metadata requests use HTTPS certificate validation, no redirects, a five-second connection timeout and a fifteen-second total timeout. Both declared and streamed body sizes are bounded. Artifact downloads use Tauri's HTTPS updater transport and permit at most two redirects, only to `release-assets.githubusercontent.com` or `objects.githubusercontent.com`, without credentials or a custom port. Downloads are capped at 512 MiB because Tauri verifies an in-memory buffer. PlateFold rejects a declared or streamed size different from the signed size, aborts excess data, and accepts bytes only after Tauri signature, exact size, SHA-256, an independent Minisign verification and the release binding all pass. No document names or export contents are sent.

PlateFold constructs the native update from authenticated release fields without another metadata request. Its pinned Tauri dependency patch also bounds artifact buffering before each append and enforces a five-minute download timeout with a five-second connection timeout; see [dependency maintenance](../desktop/src-tauri/vendor/tauri-plugin-updater/PLATEFOLD.md). Installation moves the verified buffer into the installer and returns ownership after a handled failure. A task failure with an unknown replacement outcome requires checking the installed version and downloading again.

## Key rotation and recovery before activation

The release owner must choose independent custody and recovery arrangements for OS signing/notarization, updater artifact signing, discovery metadata signing, and provenance. These are different checks. Public keys belong in reviewed source; secret keys do not. Discovery and artifact signing may use distinct keys. Tests generate ephemeral signing keys in memory and retain no secret key material.

For a planned discovery-key rotation, ship a reviewed build trusting both old and new keys while the feed is still signed by the old key. After the supported installed population can verify the new key, switch the feed to the new signature. Remove the old key in a subsequent qualified build. At most four pinned keys are accepted; tests exercise the overlap and rejection of an untrusted key.

A compromised key cannot safely authorize its own replacement. Stop publishing the affected feed and distribute a separately verified, OS-signed recovery installer through the independently authenticated release channel. Document the revoked key and the minimum safe version. If the key is lost, restore it through the owner-approved recovery procedure or use the same independent installer route. Do not silently substitute a new feed or disable signature verification.

Before enabling a feed, record the owner, key identifiers, storage and recovery procedure, rotation window, immutable release location, expiry renewal job, supported platforms, and the signed N-to-N+1 acceptance evidence. The private release record tracks custody, recovery and activation separately from this compiled policy.

## Installation and recovery behavior

The adapter uses the established [Tauri signed updater](https://v2.tauri.app/plugin/updater/) for NSIS/MSI, AppImage and macOS app bundles. Discovery, download, installation and restart remain distinct user actions. Installation requires an approval checkbox; restart requires its own separate checkbox or a later **Restart PlateFold now** action. Windows may close the application as part of launching its installer, but only after installation consent. DEB/RPM stay with the package manager: the app detects the installation from its bundle marker, never offers a download, and points to the release page for the next `.deb`. Portable, browser and container distributions do not self-update.

The native export path rejects new exports while a download or installation is active, and the UI disables export for the same interval. An export already running prevents update checking, download, installation and deferred restart. Restart also reserves the native export exclusion until process exit. Cancelling a download drops all bytes. Handled download or signature failures return to the available state. Installation failures that permit a retry retain the verified bytes in memory for retry or cancel. A refused macOS bundle exchange clears the pending download and offers no retry.

The new AppImage or macOS app is staged beside the installed copy on the same filesystem. An AppImage replaces the installed file in one atomic rename. A macOS app uses `renamex_np(RENAME_SWAP)` to exchange the staged and installed bundles in one step, then deletes the previous copy from the staging folder. Both keep the installed path occupied throughout replacement. If the macOS volume cannot exchange bundles, the updater returns `Error::ReplacementUnsupported` without moving the installed app. PlateFold tells the user to update by hand; the user guide's [update page](../docs/guides/updates.md#if-the-app-is-missing-after-an-update) gives the steps.

A close during installation normally waits for the attempt's result. After 60 seconds from the close request, the watcher abandons further replacement and resumes the close once the commit gate is idle. An active replacement or Windows installer launch must finish, so this is not a hard closing deadline. Abandoning further replacement does not undo an installed update. A close also withdraws a requested Windows restart before the updater reads the restart choice while preparing the installer launch.

PlateFold never asks for an administrator password to replace itself: a macOS app or AppImage whose bundle or folder this account can't change is offered manual steps before any download, and the updater stops before anything moves if it gets that far. Windows hands the verified installer to NSIS or MSI, which replace the application themselves. No downloaded bytes persist across an application restart. An atomic marker records an active download, verified artifact or installation; finding it at startup discards pending state and reports that the previous operation was interrupted. Update preferences remain in their separate `updates.json` file.

Ordinary builds set `createUpdaterArtifacts` to false. A signing operator explicitly uses `python tools/build_desktop.py --updater-artifacts --output-dir <new-directory>` with `TAURI_SIGNING_PRIVATE_KEY` (and its password variable when applicable). The command applies `packaging/tauri-updater.conf.json`, refuses portable mode, signs each final updater artifact with its release binding, verifies it against the compiled policy, and retains the updater payload/signature in the checksummed build receipt. Test code creates disposable in-memory Minisign keys; it never reads production keys.

This implementation's recovery tests are simulated state/adapter checks. The replacement sequence is tested on Linux by failing each of its filesystem steps in turn, and it has not run on a Mac. They do **not** establish that a real installed N package can survive process termination during OS-level replacement, that N+1 launches, or that rollback to N works. OS signing/notarization, provenance and production feed readiness are also separate qualifications.

## Native N-to-N+1 qualification

Recorded Windows NSIS and user-owned Linux AppImage rehearsals cover older-to-newer signed builds, including tamper, transport interruption and handled installation failures. Those records do not qualify later builds; repeat qualification against each release's exact signed bytes. Interrupting the application during the OS replacement itself was not tested, and the macOS app has not been through the native procedure. Repeat on every supported self-updating bundle (Windows NSIS/MSI if shipped, Linux AppImage, macOS app) without using production keys:

1. Generate a disposable Tauri signing key outside the repository. Build version N and N+1 from exact clean commits with `--updater-artifacts`; retain both receipts, hashes and signatures. Configure a local HTTPS test feed and compile only the disposable metadata/artifact public keys and endpoint into N.
2. Install N through the real OS package path, create an identifiable user theme and update preferences, export a fixture, close it, reopen it, and record the installed version and file locations.
3. Serve the published, unexpired N+1 envelope. In N, approve check, download and installation separately. First leave restart unapproved and verify no restart occurs; then explicitly restart. Confirm N+1 launches, the package identity changed, the theme/preferences survived, and the fixture still exports.
4. Repeat with restart pre-approved. Record the exact installer behavior, including Windows process exit. Confirm the version after relaunch rather than inferring success from an installer return value.
5. Tamper one artifact byte without changing the signed claims; confirm refusal before installation. Repeat with untrusted metadata signing, untrusted artifact signing, a URL that differs from the signed claim, a mismatched signature, the wrong target or channel, rollback, expiry, a metadata redirect or an artifact redirect outside the allowed hosts, an unavailable feed, an artifact larger than its signed size, and N's artifact and signature offered in metadata that claims N+1.
6. Interrupt transport mid-download and confirm retry starts from zero. Force a handled installer failure and confirm N remains launchable and an attended retry/cancel is offered. Finally interrupt the application during the OS replacement boundary and verify the platform's actual recovery/rollback behavior. This last experiment is the evidence simulated tests cannot supply.
7. Remove the disposable feed and keys, archive the test evidence, and restore the test system to its clean baseline. Do not enable production policy or publish a release from this procedure.

## Verification

```console
cd desktop
npm ci
npm run build
cd src-tauri
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
cargo check --locked --target x86_64-pc-windows-gnu
```

With the Python development environment and sandbox-capable Chromium available, run `python -m pytest tests/test_desktop_ui.py tests/test_server_web.py`. Native tests sign synthetic metadata using fresh in-memory keys, exercise the real bounded HTTP reader against loopback fixtures, and verify preference recovery and state races. UI tests use the controlled Tauri adapter; no public feed is contacted.
