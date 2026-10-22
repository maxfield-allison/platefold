"""Discover Chromium-family engines on Windows, macOS and Linux."""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
from collections.abc import Callable
from pathlib import Path

from .cdp import Browser, CancelledError, SandboxUnavailableError
from .limits import ResourceLimitError
from .paths import is_frozen, resource_root

CHOICES = ("auto", "bundled", "system", "edge", "chrome", "chromium", "brave")
_WINDOWS = {
    "edge": "Microsoft/Edge/Application/msedge.exe",
    "chrome": "Google/Chrome/Application/chrome.exe",
    "chromium": "Chromium/Application/chrome.exe",
    "brave": "BraveSoftware/Brave-Browser/Application/brave.exe",
}
_MAC = {
    "edge": "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "chrome": "Google Chrome.app/Contents/MacOS/Google Chrome",
    "chromium": "Chromium.app/Contents/MacOS/Chromium",
    "brave": "Brave Browser.app/Contents/MacOS/Brave Browser",
}
_COMMANDS = {
    "edge": ("microsoft-edge", "microsoft-edge-stable"),
    "chrome": ("google-chrome", "google-chrome-stable"),
    "chromium": ("chromium", "chromium-browser"),
    "brave": ("brave-browser", "brave"),
}
NO_BROWSER = (
    "No Chromium-family browser found. Install Chrome, Chromium, Edge or Brave, "
    "use a build with a bundled browser, or pass --browser /path/to/executable."
)


def find_system(kind: str) -> str | None:
    if kind not in _WINDOWS:
        return None
    if sys.platform == "win32":
        bases = [
            os.environ.get("PROGRAMFILES", r"C:\Program Files"),
            os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        for base in bases:
            if base and (Path(base) / _WINDOWS[kind]).is_file():
                return str(Path(base) / _WINDOWS[kind])
    elif sys.platform == "darwin":
        for base in (Path("/Applications"), Path.home() / "Applications"):
            candidate = base / _MAC[kind]
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
    else:
        for command in _COMMANDS[kind]:
            if found := shutil.which(command):
                return found
    return None


def _search(root: str | Path) -> str | None:
    root = Path(root)
    if not root.is_dir():
        return None
    names = (
        ("chrome-headless-shell.exe", "headless_shell.exe", "chrome.exe")
        if sys.platform == "win32"
        else ("chrome-headless-shell", "headless_shell", "Chromium", "chrome")
    )
    for name in names:
        for path in sorted(root.rglob(name), reverse=True):
            if path.is_file() and (sys.platform == "win32" or os.access(path, os.X_OK)):
                return str(path)
    return None


def playwright_cache() -> Path:
    override = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if override and override != "0":
        return Path(override).expanduser()
    if sys.platform == "win32":
        return (
            Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local") / "ms-playwright"
        )
    if sys.platform == "darwin":
        return Path.home() / "Library/Caches/ms-playwright"
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "ms-playwright"


def find_bundled() -> str | None:
    found = _search(Path(resource_root()) / "browser")
    if found or is_frozen():
        return found
    return _search(playwright_cache())


def describe(browser: Browser, label: str) -> str:
    return f"{label} ({browser.version})" if browser.version != "unknown" else label


def _qualified(
    path: str, label: str, cancel_check: Callable[[], None] | None = None
) -> tuple[Browser, str]:
    from .browser_qualification import qualify

    if cancel_check:
        cancel_check()
    browser = Browser(path)
    try:
        receipt = qualify(browser, cancel_check=cancel_check)
    except Exception:
        browser.set_cancel_callback(lambda: True)
        with contextlib.suppress(Exception):
            browser.close()
        raise
    browser.qualification_receipt = receipt.as_dict()
    return browser, f"{label} ({receipt.browser_version})"


def launch(
    choice: str = "auto", *, cancel_check: Callable[[], None] | None = None
) -> tuple[Browser, str]:
    if cancel_check:
        cancel_check()
    if choice not in CHOICES:
        path = Path(choice).expanduser()
        if not path.is_file():
            raise RuntimeError(f"Browser executable not found: {choice}")
        return _qualified(str(path.resolve()), path.name, cancel_check)
    if choice in ("auto", "bundled"):
        if path := find_bundled():
            try:
                browser = Browser(path)
            except SandboxUnavailableError as exc:
                if choice == "bundled":
                    raise
                return _launch_after_sandbox_refusal(exc, cancel_check)
            return browser, describe(browser, "bundled Chromium")
        if choice == "bundled":
            raise RuntimeError(
                "No bundled browser found. Use --browser auto or install a Chromium engine."
            )
    return _launch_system(choice, cancel_check)


def _launch_after_sandbox_refusal(
    refusal: SandboxUnavailableError, cancel_check: Callable[[], None] | None = None
) -> tuple[Browser, str]:
    """Use an installed, qualified browser when the system denies the bundled one's sandbox.

    Distribution and vendor Chrome/Chromium packages carry their own permission
    for the sandbox, so they can render where the bundled engine cannot. They
    must pass the same rendering check as any installed engine. Without one,
    the clear sandbox error stands; the sandbox is never turned off.
    """
    try:
        browser, description = _launch_system("auto", cancel_check)
    except (CancelledError, ResourceLimitError):
        raise
    except RuntimeError:
        raise refusal from None
    browser.system_browser_failures = [
        "bundled: Chromium's sandbox is unavailable on this system",
        *(browser.system_browser_failures or []),
    ]
    return browser, f"{description} (bundled Chromium's sandbox unavailable)"


def _launch_system(
    choice: str, cancel_check: Callable[[], None] | None = None
) -> tuple[Browser, str]:
    order = (
        ("edge", "chrome", "chromium", "brave")
        if sys.platform == "win32"
        else ("chrome", "chromium", "edge", "brave")
    )
    failures = []
    for kind in order if choice in ("auto", "system") else (choice,):
        if path := find_system(kind):
            try:
                browser, description = _qualified(path, f"qualified {kind}", cancel_check)
                browser.system_browser_failures = failures
                return browser, description
            except (CancelledError, ResourceLimitError):
                raise
            except Exception as exc:
                failures.append(f"{kind}: {exc}")
    if choice == "system" and (path := find_bundled()):
        browser, description = _qualified(
            path, "bundled Chromium (installed engine unavailable)", cancel_check
        )
        browser.system_browser_failures = failures
        return browser, description
    if failures:
        raise RuntimeError(
            "No installed browser passed the rendering check. Use the bundled browser or "
            "update your installed browser. " + "; ".join(failures)
        )
    raise RuntimeError(NO_BROWSER)
