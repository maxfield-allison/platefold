"""Load the served browser interface in the image's own Chromium and fail unless it starts.

The container suite pipes this file to ``python -`` inside the running server
container, so the browser is the image's bundled Chromium, launched the way the
server launches it, under the server container's own confinement. It needs
nothing from the host and nothing outside the image.

The interface counts as started when loading it raised no uncaught exception
and no console error, and the page shows text from the English interface
catalog the image ships. The text check stops a blank page from passing: a
bundle built without its catalogs throws while loading and renders nothing.

Usage: python - ORIGIN < tools/container_ui_check.py
"""

from __future__ import annotations

import contextlib
import json
import sys
from importlib.resources import files

from platefold.browser import launch
from platefold.cdp import TimeoutError_

# Before a credential is entered, the page shows its server connection panel.
# Each element's text comes straight from one catalog message.
EXPECTED = {"#connect-server": "web.connect", "#connection-note": "web.connection_intro"}
CONSOLE_ERRORS = {"error", "assert"}
LOAD_TIMEOUT = 30.0
RENDER_TIMEOUT = 10.0
# Loading work the page schedules for after its load event, such as fonts,
# still reports errors; give it this long before the page is read.
SETTLE_MS = 500


def expected_text() -> dict[str, str]:
    catalog = json.loads(files("platefold").joinpath("locales/en.json").read_text("utf-8"))
    return {selector: catalog["messages"][key] for selector, key in EXPECTED.items()}


def _first_line(value: object) -> str:
    return str(value).strip().split("\n", 1)[0]


def page_problems(events: list[dict], session: str) -> list[str]:
    """The page's uncaught exceptions and console errors, one readable line each."""
    found = []
    for event in events:
        if event.get("sessionId") != session:
            continue
        method, params = event.get("method"), event.get("params") or {}
        if method == "Runtime.exceptionThrown":
            details = params.get("exceptionDetails") or {}
            thrown = (details.get("exception") or {}).get("description") or details.get("text")
            found.append("uncaught exception: " + _first_line(thrown))
        elif method == "Runtime.consoleAPICalled" and params.get("type") in CONSOLE_ERRORS:
            args = params.get("args") or []
            text = " ".join(
                _first_line(arg.get("value", arg.get("description", ""))) for arg in args
            )
            found.append(f"console.{params['type']}: {text}")
        elif method == "Log.entryAdded" and (params.get("entry") or {}).get("level") == "error":
            entry = params["entry"]
            where = f" ({entry['url']})" if entry.get("url") else ""
            found.append(f"console error [{entry.get('source')}]: {entry.get('text')}{where}")
    return found


def missing_text(shown: dict[str, str | None], expected: dict[str, str]) -> list[str]:
    return [
        f"{selector} shows {shown.get(selector)!r}, expected {text!r}"
        for selector, text in expected.items()
        if (shown.get(selector) or "").strip() != text
    ]


def check(origin: str) -> dict:
    expected = expected_text()
    events: list[dict] = []
    browser, engine = launch("bundled")
    browser.set_event_listener(events.append)
    try:
        page = browser.new_page()
        # A new page refuses every network request, which is right for the
        # documents the server renders. This page is the served interface.
        page._send("Network.setBlockedURLs", {"urls": []})
        page._send("Runtime.enable")
        page._send("Log.enable")
        session = page._session
        navigation = page._send("Page.navigate", {"url": origin + "/"})
        if navigation.get("errorText"):
            raise SystemExit(f"The interface did not load: {navigation['errorText']}")
        page.wait_for_function("document.readyState === 'complete'", LOAD_TIMEOUT)
        page.evaluate(
            f"document.fonts.ready.then(() => new Promise(done => setTimeout(done, {SETTLE_MS})))"
        )
        if not page_problems(events, session):
            # The panel renders synchronously while the entry module runs, so
            # a missing panel after a clean load is itself the failure.
            with contextlib.suppress(TimeoutError_):
                page.wait_for_function(
                    " && ".join(
                        f"document.querySelector({json.dumps(s)})?.textContent.trim() === "
                        f"{json.dumps(t)}"
                        for s, t in expected.items()
                    ),
                    RENDER_TIMEOUT,
                )
        shown = page.evaluate(
            "(selectors) => Object.fromEntries(selectors.map("
            "s => [s, document.querySelector(s)?.textContent ?? null]))",
            list(expected),
        )
        lang = page.evaluate("document.documentElement.lang")
        failures = page_problems(events, session) + missing_text(shown, expected)
        if lang != "en":
            failures.append(f"document language is {lang!r}, expected 'en'")
        if failures:
            raise SystemExit("The browser interface did not start:\n  " + "\n  ".join(failures))
        return {"engine": engine, "url": origin + "/", "lang": lang, "catalog_text": shown}
    finally:
        browser.close()


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        raise SystemExit("usage: python - ORIGIN < container_ui_check.py")
    print(json.dumps(check(argv[1].rstrip("/")), indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
