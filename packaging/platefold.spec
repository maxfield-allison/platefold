# PyInstaller spec: a Windows folder nobody has to set up.
#
#     just exe            slim build, renders with the installed Edge
#     just exe-bundled    self-contained, carries a pinned Chromium
#
# The result contains both entry points:
#
#     dist/platefold/platefold-gui.exe   double-click front end
#     dist/platefold/platefold.exe       command line
#
# One-folder rather than one-file, deliberately: a one-file build unpacks to a
# temporary directory on *every* launch, which is slow and looks broken.

import os
from pathlib import Path

PROJECT = Path(SPECPATH).parent

# ---------------------------------------------------------------------------
# Bundled resources
# ---------------------------------------------------------------------------

from PyInstaller.utils.hooks import collect_data_files, copy_metadata

datas = collect_data_files("platefold")
datas += copy_metadata("tinycss2") + copy_metadata("webencodings")

# ---------------------------------------------------------------------------
# The browser, for the self-contained build only
#
# The renderer talks the DevTools Protocol over a websocket (see platefold/cdp.py),
# so nothing here needs Playwright or its Node runtime. Playwright is still the
# most convenient way to *download* a pinned Chromium, which is why the
# exe-bundled recipe invokes it and this reads from its cache.
# ---------------------------------------------------------------------------

BUNDLE_BROWSER = os.environ.get("PLATEFOLD_BUNDLE_BROWSER") == "1"

# Payload a headless print job never touches. Locales are 42 MB of translated
# UI strings for a browser with no UI; SwiftShader is a software Vulkan
# rasteriser for WebGL we never run; hyphenation dictionaries are dead because
# the stylesheet sets `hyphens: none`.
DROP_DIRS = {"hyphen-data"}
KEEP_LOCALES = {"en-US.pak", "en-GB.pak"}
DROP_FILES = {"vk_swiftshader.dll", "vulkan-1.dll"}


def browser_payload(root):
    """Walk a browser directory, skipping payload this tool cannot use."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        top = rel_dir.parts[0] if rel_dir.parts else ""
        if top in DROP_DIRS:
            dirnames[:] = []
            continue
        for filename in filenames:
            if "locales" in rel_dir.parts and filename not in KEEP_LOCALES:
                continue
            if filename in DROP_FILES:
                continue
            yield Path(dirpath) / filename, rel_dir


if BUNDLE_BROWSER:
    from platefold.browser import find_bundled
    executable = find_bundled()
    if not executable:
        raise SystemExit("No downloaded Chromium found; run python -m playwright install chromium")
    engine = Path(executable).parent
    for src, rel_dir in browser_payload(engine):
        datas.append((str(src), str(Path("platefold/browser") / rel_dir)))

hiddenimports = [
    "PIL._tkinter_finder",
    "pygments.formatters.html",
    "pygments.lexers",
]

# Never needed at runtime. pdfplumber and its dependency stack exist only for
# the blank-page regression check, which is a development tool; playwright is
# only ever used to *download* a browser.
excludes = [
    "pdfplumber",
    "pdfminer",
    "cryptography",
    "pypdfium2",
    "pypdfium2_raw",
    "playwright",
    "greenlet",
    "pyee",
    "pytest",
    "_pytest",
    "ruff",
    "setuptools",
    "pip",
    "wheel",
    "unittest",
    "pydoc_data",
    "doctest",
]

block_cipher = None

cli_analysis = Analysis(
    [str(PROJECT / "packaging/cli_entry.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)

gui_analysis = Analysis(
    [str(PROJECT / "packaging/gui_entry.py")],
    pathex=[str(PROJECT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    cipher=block_cipher,
    noarchive=False,
)

MERGE((cli_analysis, "platefold", "platefold"), (gui_analysis, "platefold-gui", "platefold-gui"))

cli_pyz = PYZ(cli_analysis.pure, cli_analysis.zipped_data, cipher=block_cipher)
gui_pyz = PYZ(gui_analysis.pure, gui_analysis.zipped_data, cipher=block_cipher)

cli_exe = EXE(
    cli_pyz,
    cli_analysis.scripts,
    [],
    exclude_binaries=True,
    name="platefold",
    console=True,
    disable_windowed_traceback=False,
    upx=False,
)

gui_exe = EXE(
    gui_pyz,
    gui_analysis.scripts,
    [],
    exclude_binaries=True,
    name="platefold-gui",
    console=False,  # no console window behind the GUI
    disable_windowed_traceback=False,
    upx=False,
)

coll = COLLECT(
    cli_exe,
    cli_analysis.binaries,
    cli_analysis.zipfiles,
    cli_analysis.datas,
    gui_exe,
    gui_analysis.binaries,
    gui_analysis.zipfiles,
    gui_analysis.datas,
    strip=False,
    upx=False,
    name="platefold",
)
