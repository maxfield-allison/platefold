"""Exercise a built Windows app through its validated path-entry controls.

Requires development Playwright and an interactive Windows session. The temporary
debugging endpoint belongs only to the launched test process. No debug flags ship
in the application. Inputs and output must be explicit synthetic test paths.
"""

import argparse
import json
import os
import socket
import subprocess
import time
import urllib.request
from pathlib import Path


def process_inventory():
    script = "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,Name | ConvertTo-Json -Compress"
    processes = json.loads(
        subprocess.check_output(["powershell", "-NoProfile", "-Command", script], text=True)
    )
    return [processes] if isinstance(processes, dict) else processes


def descendants(parent_pid):
    by_parent = {}
    for process in process_inventory():
        by_parent.setdefault(process["ParentProcessId"], []).append(process)
    found = []
    pending = [parent_pid]
    while pending:
        children = by_parent.get(pending.pop(), [])
        found.extend(children)
        pending.extend(child["ProcessId"] for child in children)
    return found


def theme_journey(page, output, user_data, phase):
    """Exercise disk-backed themes across two separate native app launches."""
    from playwright.sync_api import expect

    name = "windows-package-proof"
    file = user_data / "roaming/platefold/themes" / f"{name}.json"
    page.locator("#choose-theme").click()
    page.get_by_role("button", name="bristol", exact=True).click()
    expect(page.locator("#overwrite")).to_be_disabled()
    expect(page.locator("#delete-theme")).to_be_disabled()
    if phase == "create":
        assert not file.exists(), "Use fresh qualification user data"
        page.locator("#choose-theme").click()
        page.get_by_role("button", name="midnight", exact=True).click()
        expect(page.locator("#mode-advisory")).to_be_visible()
        page.screenshot(path=str(output / "dark-print-advisory.png"))
        page.locator("#choose-theme").click()
        page.get_by_role("button", name="bristol", exact=True).click()
        page.locator("#save-copy").click()
        page.get_by_label("Theme name").fill(name)
        page.locator("#confirm-copy").click()
        expect(page.locator("#theme-name")).to_have_text(name)
        page.get_by_text("Customise palette", exact=True).click()
        page.get_by_label("accent hex", exact=True).fill("#123456")
        page.locator("#overwrite").click()
        expect(page.locator("#status")).to_contain_text(f"Saved edits to {name}")
        assert json.loads(file.read_text())["colors"]["accent"] == "#123456"
        page.get_by_label("accent hex", exact=True).fill("#654321")
        page.locator("#revert").click()
        expect(page.get_by_label("accent hex", exact=True)).to_have_value("#123456")
    else:
        assert json.loads(file.read_text())["colors"]["accent"] == "#123456"
        page.locator("#choose-theme").click()
        page.get_by_role("button", name=name, exact=True).click()
        page.get_by_text("Customise palette", exact=True).click()
        expect(page.get_by_label("accent hex", exact=True)).to_have_value("#123456")
        page.locator("#delete-theme").click()
        expect(page.locator("#delete-dialog")).to_contain_text(name)
        expect(page.locator("#delete-dialog")).to_contain_text(str(file))
        page.screenshot(path=str(output / "theme-delete-confirmation.png"))
        page.locator("#dismiss-delete").click()
        assert file.exists()
        page.locator("#delete-theme").click()
        page.locator("#confirm-delete").click()
        expect(page.locator("#delete-dialog")).not_to_be_visible()
        assert not file.exists()
    page.screenshot(path=str(output / f"theme-{phase}.png"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--user-data", type=Path, help="Isolated state shared only by qualification runs"
    )
    parser.add_argument("--theme-phase", choices=("create", "delete"))
    args = parser.parse_args()
    if os.name != "nt":
        parser.error("This qualification tool requires Windows")
    executable = args.executable.resolve(strict=True)
    source = args.source.resolve(strict=True)
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve()
    user_data = (args.user_data or output / "user-data").resolve()
    (user_data / "roaming").mkdir(parents=True, exist_ok=True)
    (user_data / "local").mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import expect, sync_playwright

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env["APPDATA"] = str(user_data / "roaming")
    env["LOCALAPPDATA"] = str(user_data / "local")
    # Keep the debugging endpoint independent of an already running installed app.
    env["WEBVIEW2_USER_DATA_FOLDER"] = str(output / "webview-profile")
    env["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = f"--remote-debugging-port={port}"
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    process = subprocess.Popen([str(executable)], env=env, startupinfo=startup)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=1):
                    break
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("Native WebView2 did not become ready") from None
                time.sleep(0.2)
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = browser.contexts[0].pages[0]
            expect(page.locator("#status")).to_contain_text("Ready", timeout=20000)
            page.get_by_text("Enter source path", exact=True).click()
            page.get_by_label("Source path", exact=True).fill(str(source))
            page.locator("#use-source").click()
            expect(page.locator("#source-path")).to_contain_text(source.name, timeout=20000)
            expect(page.locator("#preview-status")).to_contain_text("Generating preview")
            page.screenshot(path=str(output / "preview-generating.png"), animations="disabled")
            expect(page.locator("#preview-status")).to_contain_text("Preview ready", timeout=60000)
            expect(page.locator("#preview-caption")).to_contain_text(source.name)
            original_preview = page.locator("#preview-image").get_attribute("src")
            page.get_by_label("Font set", exact=True).select_option("georgia")
            expect(page.locator("#preview-status")).to_contain_text("Generating preview")
            expect(page.locator("#preview-status")).to_contain_text("Preview ready", timeout=60000)
            assert page.locator("#preview-image").get_attribute("src") != original_preview
            page.screenshot(path=str(output / "preview-georgia.png"), animations="disabled")
            page.get_by_label("Font set", exact=True).select_option("sora")
            expect(page.locator("#preview-status")).to_contain_text("Preview ready", timeout=60000)
            page.get_by_text("Enter output path", exact=True).click()
            page.get_by_label("Output folder path", exact=True).fill(str(output))
            page.locator("#use-output").click()
            expect(page.locator("#output-path")).to_contain_text(output.name, timeout=20000)
            page.get_by_role("button", name="Export PDFs").click()
            expect(page.locator("#status")).to_contain_text("Export complete", timeout=60000)
            report = json.loads((output / "_export-report.json").read_text())
            assert report["documents"] and "bundled Chromium" in report["engine"], report
            assert report["settings"]["font_set"] == "sora", report
            assert page.evaluate(
                "document.documentElement.scrollHeight <= innerHeight && document.documentElement.scrollWidth <= innerWidth"
            ), "The native window must not have an outer scrollbar"
            expect(page.get_by_role("button", name="Export PDFs")).to_be_in_viewport()
            page.screenshot(path=str(output / "desktop.png"), full_page=True)
            page.get_by_role("button", name="Dark interface").click()
            page.screenshot(path=str(output / "desktop-dark.png"), animations="disabled")
            page.get_by_role("button", name="View results").click()
            expect(page.get_by_role("button", name="Open PDF for")).to_have_count(1)
            page.screenshot(path=str(output / "desktop-results.png"), animations="disabled")
            page.get_by_role("button", name="Close results").click()
            page.get_by_role("button", name="Choose theme:").click()
            page.screenshot(path=str(output / "desktop-themes.png"), animations="disabled")
            page.keyboard.press("Escape")
            expect(page.get_by_role("button", name="Choose theme:")).to_be_focused()
            if args.theme_phase:
                theme_journey(page, output, user_data, args.theme_phase)
            print(
                json.dumps(
                    {
                        "status": "passed",
                        "engine": report["engine"],
                        "documents": report["documents"],
                    }
                )
            )
            # Interrupt a second real job through the ordinary window-close path,
            # then prove every worker/browser descendant disappears.
            interruption = output / "interruption-source"
            interruption.mkdir(exist_ok=True)
            for number in range(100):
                (interruption / f"{number:03}.md").write_text(
                    "# Lifecycle test\n\n" + "Body text.\n\n" * 100
                )
            page.get_by_label("Source path", exact=True).fill(str(interruption))
            page.locator("#use-source").click()
            expect(page.locator("#source-path")).to_contain_text(interruption.name)
            page.get_by_role("button", name="Export PDFs").click()
            expect(page.locator("#status")).to_contain_text("Exporting", timeout=20000)
            owned = descendants(process.pid)
            worker = next(child for child in owned if child["Name"] == "platefold.exe")
            worker_tree = descendants(worker["ProcessId"])
            assert any("chrome" in child["Name"].lower() for child in worker_tree), worker_tree
            # Keep this CDP connection alive through the entire native flow.
            started = time.monotonic()
            script = f"if (-not (Get-Process -Id {process.pid}).CloseMainWindow()) {{ exit 1 }}"
            subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True)
            process.wait(timeout=5)
            assert time.monotonic() - started < 3, "Window close exceeded the three-second gate"
            owned_pids = {child["ProcessId"] for child in owned}
            deadline = time.monotonic() + 5
            while owned_pids & {child["ProcessId"] for child in process_inventory()}:
                if time.monotonic() > deadline:
                    raise AssertionError("A worker or PDF browser survived its desktop host")
                time.sleep(0.2)
            print(
                f"Window-close lifecycle: {len(owned)} descendants exited in "
                f"{time.monotonic() - started:.2f}s"
            )
            (output / "NATIVE-CHECK.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "executable": str(executable),
                        "engine": report["engine"],
                        "theme_phase": args.theme_phase,
                        "descendants": owned,
                        "cleanup_seconds": time.monotonic() - started,
                        "host_close_limit_seconds": 3,
                        "cleanup_limit_seconds": 5,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)


if __name__ == "__main__":
    main()
