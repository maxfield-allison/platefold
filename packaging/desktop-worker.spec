"""CLI and desktop worker, with its Python runtime and offline PDF browser."""

import sys
from pathlib import Path

from PyInstaller.depend.bindepend import get_imports
from PyInstaller.utils.hooks import collect_data_files, copy_metadata

PROJECT = Path(SPECPATH).parent
sys.path.insert(0, SPECPATH)
from platefold.browser import find_bundled
from runtime_trim import keep_data_entry, required_babel_locale_files, stripped_binaries

browser = find_bundled()
if not browser:
    raise SystemExit("Download pinned Chromium before building the desktop worker")
datas = collect_data_files("platefold")
datas += collect_data_files("pypdfium2") + collect_data_files("pypdfium2_raw")
# Include every pinned runtime's metadata and license files, including transitive
# requirements. PDFium's third-party notices live inside pypdfium2's metadata.
for requirement in (PROJECT / "requirements-runtime.txt").read_text(encoding="utf-8").splitlines():
    if requirement.strip() and not requirement.startswith("#"):
        name, version = requirement.split("==", 1)
        datas += copy_metadata(name, recursive=True)
        # Cobble 0.1.4 declares BSD-2-Clause upstream but its wheel omits the
        # license text. Supply the retained upstream notice beside its metadata.
        if name == "cobble":
            datas += [
                (
                    str(PROJECT / "packaging/licenses/cobble-BSD-2-Clause.txt"),
                    f"cobble-{version}.dist-info/licenses",
                )
            ]
# These are imported conditionally by the runtime dependency graph.
datas += copy_metadata("packaging") + copy_metadata("typing_extensions")
if sys.platform == "win32":
    datas += [(str(Path(sys.base_prefix) / "LICENSE.txt"), "licenses")]
datas += [(str(Path(browser).parent), "platefold/browser")]
datas += [(str(PROJECT / "LICENSE"), "licenses")]
browser_binaries = [(browser, "platefold/browser")]


def linux_browser_libraries(executable):
    """Collect Chromium's library closure while leaving glibc to the target system."""
    if not sys.platform.startswith("linux"):
        return []
    glibc = {
        "ld-linux-x86-64.so.2",
        "libc.so.6",
        "libdl.so.2",
        "libm.so.6",
        "libpthread.so.0",
        "libresolv.so.2",
        "librt.so.1",
        "libutil.so.1",
    }
    pending = [str(executable)]
    libraries = {}
    while pending:
        current = pending.pop()
        for name, source in get_imports(current):
            if source is None or name in glibc or name.startswith("ld-linux"):
                continue
            source = str(source)
            if source not in libraries:
                libraries[source] = name
                pending.append(source)
    return [(source, ".") for source in sorted(libraries)]


browser_binaries += linux_browser_libraries(browser)
analysis = Analysis(
    [str(PROJECT / "packaging/cli_entry.py")], pathex=[str(PROJECT)],
    binaries=browser_binaries, datas=datas,
    hiddenimports=["platefold.worker", "pygments.formatters.html", "pygments.lexers"],
    excludes=["tkinter", "_tkinter", "playwright", "pdfplumber", "pdfminer", "pytest", "ruff", "cryptography",
              "setuptools", "pip", "wheel", "build", "PyInstaller",
              # help() and pdb's documentation browser; the runtime has no interactive prompt.
              "pydoc", "pydoc_data"],
)
# See runtime_trim.py for why each of these trims cannot change behaviour.
babel_locale_files = required_babel_locale_files()
analysis.datas = [entry for entry in analysis.datas if keep_data_entry(entry[0], babel_locale_files)]
if sys.platform.startswith("linux"):
    analysis.binaries = stripped_binaries(analysis.binaries, workpath, keep_sources=[browser])
pyz = PYZ(analysis.pure)
exe = EXE(pyz, analysis.scripts, [], exclude_binaries=True, name="platefold", console=True, upx=False)
COLLECT(exe, analysis.binaries, analysis.datas, strip=False, upx=False, name="runtime")
