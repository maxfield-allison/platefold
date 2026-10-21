# Verifying downloads

Each release publishes four files beside its downloads:

`SHA256SUMS`
: The SHA-256 hash of every file in the release, including `release-manifest.json`.

`SHA256SUMS.minisig`
: A [minisign](https://jedisct1.github.io/minisign/) signature over `SHA256SUMS`, made with the PlateFold release-evidence key.

`SHA256SUMS.sigstore.json`
: A [Cosign](https://docs.sigstore.dev/cosign/system_config/installation/) signature bundle over the same `SHA256SUMS`, made with the PlateFold release Cosign key.

`release-manifest.json`
: The version, the public source commit and tree the release was built from, and each file's name, kind, size and hash.

Both signatures cover `SHA256SUMS`, and `SHA256SUMS` covers the manifest and every download, so checking either signature and then the hashes checks all of them. Use whichever tool you have.

## Check the release signature

The release-evidence public key is `packaging/release-evidence.pub` in the source repository:

```text
RWTsBdXrPo+KenB9vlSIsX9ph9NhQJziyCL/jqqQXhgADPRQIFpwlPP8
```

Put `SHA256SUMS`, `SHA256SUMS.minisig` and your downloads in one folder, then run:

```console
minisign -Vm SHA256SUMS -P RWTsBdXrPo+KenB9vlSIsX9ph9NhQJziyCL/jqqQXhgADPRQIFpwlPP8
```

A good signature prints `Signature and comment signature verified`. Then check the downloads against `SHA256SUMS` as described below.

From a source checkout, `python tools/assemble_release.py verify <folder> --partial` runs both checks and also compares every file with the manifest. It needs the `cryptography` package, which the `dev` extra installs.

## Check the Cosign signature

The release Cosign public key is `packaging/cosign.pub` in the source repository. The signature is recorded in the public Sigstore transparency log, so Cosign needs only that key. Put `SHA256SUMS`, `SHA256SUMS.sigstore.json` and the key, saved as `cosign.pub`, in one folder, then run:

```console
cosign verify-blob --key cosign.pub --bundle SHA256SUMS.sigstore.json SHA256SUMS
```

A good signature prints `Verified OK`. Then check the downloads against `SHA256SUMS` as described below. The same key signs the container images; the [container guide](../guides/container.md#verify-a-published-image) shows that check.

## What a build produces

Each local desktop package build writes two files beside its packages:

`SHA256SUMS`
: One line per package: its SHA-256 hash, two spaces, and its file name.

`BUILD-RECEIPT.json`
: A record of the build. It names the product and version, the source commit and whether the source tree had uncommitted changes, the platform and architecture, each package with its hash and size, the bundled browser file and its hash, and the Python, Node.js, npm and Rust versions used. A release build also records, under `verified_downloads`, the SHA-256 of each browser archive and, on Linux, each AppImage build tool it downloaded; the build refuses to start unless those match the reviewed values in `packaging/native-downloads.json`. A local build without that check records `verified_downloads` as `null`.

The Python package build writes its own `SHA256SUMS` covering the wheel, the source archive, a CycloneDX dependency inventory and its build receipt. These per-build files are build records; check downloads against the release's signed `SHA256SUMS` described above.

## Check a file against SHA256SUMS

Put the download and `SHA256SUMS` in the same folder.

=== "Linux"

    ```console
    sha256sum --check --ignore-missing SHA256SUMS
    ```

    A match prints the file name followed by `OK`.

=== "macOS"

    ```console
    shasum -a 256 --check --ignore-missing SHA256SUMS
    ```

    A match prints the file name followed by `OK`.

=== "Windows"

    ```powershell
    Get-FileHash .\PlateFold_<version>_x64-online-setup.exe -Algorithm SHA256
    ```

    Compare the `Hash` value with the matching line in `SHA256SUMS`. PowerShell prints it in upper case, and case doesn't matter when comparing. Use the name of the file you downloaded: the offline setup and the ZIP work the same way.

## What a matching hash proves, and what it doesn't

A matching hash proves the file you have is byte-for-byte the file the build recorded. It does not prove who made the build, because anyone who can replace a download can replace the `SHA256SUMS` beside it. For a published release, the signature checks above add that proof: a changed `SHA256SUMS` fails verification against the release-evidence key and the release Cosign key. Windows installers and executables carry the published Authenticode identity and a Microsoft timestamp. The macOS app is Developer ID signed, notarized and stapled. In-app updates also verify separately signed release metadata and artifacts. Check the operating-system identity as well as the hash.

## Operating system warnings

On Windows, the setup program's **Properties → Digital Signatures** should name Maxfield Allison as the signer, with a Microsoft timestamp. SmartScreen reputation is separate from signature validity and may still warn for a new publisher or low-download release. On macOS, Gatekeeper should accept the notarized app; do not bypass a signature or notarization failure.
