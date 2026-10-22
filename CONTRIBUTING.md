# Contributing

Plate|Fold targets Python 3.11+ on Windows, macOS and Linux. Windows has clean-machine installer evidence, macOS has Developer ID, notarization and packaged-runtime checks, and Linux has browser and native-package CI gates. A passing unit suite alone doesn't establish that a platform's browser, fonts or desktop interface work.

## Set up

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then create the exact locked development environment:

```console
uv sync --locked --all-extras
uv run playwright install chromium
```

Commit `uv.lock` whenever an intentional dependency change updates it. `requirements-runtime.txt` and `requirements-build.txt` are separate exact inputs for frozen native packages; do not regenerate or relax them as a side effect of ordinary development.

The browser download supports integration tests and bundled builds. Playwright isn't part of the export runtime; Plate|Fold drives Chromium through its own DevTools Protocol client. Mermaid JavaScript is vendored in `platefold/vendor`, so normal development and export don't require npm. Node.js is needed when updating that vendored dependency. Run `npm ci --ignore-scripts` and `python tools/vendor_mermaid.py` after reviewing changes to `package.json` and `package-lock.json`. The tool bundles the locked module graph, including the DOMPurify and KaTeX overrides, so patched dependencies reach the shipped JavaScript. It also refreshes the dependency inventory, license notices and bundle hash.

Human interface translations follow the [translation contribution workflow](docs/project/translating.md). Generated or machine-translated catalogs are not accepted; pseudolocalization exists only as test data.

The `justfile` and batch launchers are Windows conveniences. The Python commands below are the portable entry points.

## Check a change

```console
uv run ruff check .
uv run ruff check --preview --select PLR1702 platefold
uv run pyright platefold
uv run python tools/check_i18n.py
uv run pytest -m "not integration"
uv run pytest
uv run python -m build
```

For rendering changes, also export a fixture and check its pages:

```console
uv run python -m platefold SAMPLE.md -o _smoke
uv run python tools/check_blank_pages.py _smoke
```

For documentation changes, build the site the way CI does:

```console
uv pip install -r requirements-docs.txt
uv run --no-sync python tools/prepare_docs.py
uv run --no-sync zensical build --clean --strict
uv run --no-sync python tools/check_docs_site.py site --sources build/docs-site
```

`prepare_docs.py` stages `docs/` into `build/docs-site` with the generated reference tables and the vendored Mermaid. Edit the files under `docs/`; the staged copy is replaced on every run.

Read skipped-test reasons. Integration tests need a real browser, and a run that skips them doesn't verify PDF output. Changes to page rules, fonts or placement need the integration suite and inspection of representative output.

Geometry tests should establish that a figure fits its assigned page at the claimed scale and that a smaller page wouldn't satisfy the threshold. Integration tests cover what those calculations can't: actual page dimensions, repeated heading destinations, contents page numbers and blank pages. Keep fixtures synthetic and small enough to explain the failure.

## Work and review

Work on a focused branch off `main` and open a pull request. Keep commits limited to the paths changed for the task.

Use Conventional Commit titles such as `fix(layout): preserve plate caption space`. Describe the user-visible change and its validation in the review. For rendering changes, include the browser used and any font assumptions. Fix the cause of a failing check. Exceptions need a reason beside the configuration that grants them.

## Build artifacts

`python -m build` produces the wheel and source distribution. Check an installed wheel from outside the source tree so missing package resources don't hide behind the checkout. Themes, schemas and vendored Mermaid must all be present.

The release build retains a Trivy-generated CycloneDX source/dependency SBOM and a `SHA256SUMS` file beside those Python artifacts. The SBOM is inventory evidence, not a signed provenance statement or a substitute for testing the installed artifact.

The legacy PyInstaller folder, with the Tk interface and the CLI, uses `packaging/platefold.spec`: `just exe` makes a slim folder using an installed browser, and `just exe-bundled` includes Chromium. Run `just exe-check` against the resulting folder. Artifact sizes depend on the dependency versions and should be measured for each build.

Native desktop packages use a separate, pinned build environment. Release builds install it from a hash-pinned file for the build platform (`linux-x86_64`, `windows-x86_64` or `macos-arm64`), and fetch the bundled browser and, on Linux, the AppImage tools only as the archives whose SHA-256 digests `packaging/native-downloads.json` records:

```console
python -m pip install --require-hashes --only-binary=:all: -r packaging/native-requirements/linux-x86_64.txt
python -m pip install --no-deps --no-build-isolation --no-index -e .
PLAYWRIGHT_BROWSERS_PATH=build/playwright-browsers python tools/native_downloads.py prepare --receipt build/native-downloads.json
PLAYWRIGHT_BROWSERS_PATH=build/playwright-browsers python tools/build_desktop.py --verified-downloads build/native-downloads.json
```

`requirements-build.txt` and `requirements-runtime.txt` remain the reviewed version pins. After an intentional change to either, `python tools/native_requirements.py lock` regenerates the per-platform files with uv and PyPI's published digests, and the test suite fails until it has. A new Playwright release changes the browser archives: update the revisions and paths in `packaging/native-downloads.json` to the ones the new release's `browsers.json` names, record their digests with `python tools/native_downloads.py pin`, review the new bytes, and commit the manifest with the new pin.

The host determines the package type: NSIS and a portable ZIP on Windows, AppImage and `.deb` on Linux, and app/DMG on macOS. The Linux build needs `dpkg-deb`; `tools/build_deb.py` assembles the `.deb` reproducibly from the same program and runtime as the AppImage. Each build writes `SHA256SUMS` and `BUILD-RECEIPT.json`. The Linux release build also extracts the AppImage in a minimal Debian environment and checks the receipt, CLI, browser identity and shared-library closure, and `tools/check_linux_deb.py` installs, exercises and purges the `.deb` in clean Ubuntu and Debian containers. The actual desktop and PDF export still need a hands-on check on a real desktop before release.

Release artifacts require the platform signing, updater-signing, package checks and exact-source receipts used for `1.0.0`. Building a wheel or installer alone does not make it a release candidate.

## Before changing layout

Preserve the tests for blank-page handling, tight SVG measurements and the print-pass invariants. Add a focused regression whenever one of those rules changes.
