"""Automated accessibility checks for the built interface in Chromium.

Each interface state is reached with the native adapter (or the web API)
replaced by a controlled stub, then checked under the display preferences a
browser can emulate: forced colours, reduced motion, increased contrast, and
zoom at the smallest window the desktop app allows. The checks cover axe-core
rules, whether every control can be reached, seen and pressed, clipped or
overlapping text, sideways scrolling, a visible focus indicator at every
keyboard stop, and motion.

This is mechanical evidence. It does not replace listening with a screen
reader or testing the packaged app under the operating system's own settings.
"""

from __future__ import annotations

import functools
import http.server
import io
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from test_desktop_ui import DIST, NATIVE_STUB, USER_THEME_PATH

from platefold.browser import find_bundled
from platefold.contrast import relative_luminance
from platefold.worker import catalog

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[1]
AXE = ROOT / "desktop" / "node_modules" / "axe-core" / "axe.min.js"
PROBE = Path(__file__).with_name("ui_probe.js")
AXE_TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]

# The desktop window's minimum is 640 x 420 CSS pixels (tauri.conf.json).
# Browser zoom divides the CSS viewport; operating-system scaling only raises
# the device pixel ratio.
SIZES = {
    "minimum": (640, 420, 1),
    "os-scale-200": (640, 420, 2),
    "zoom-200": (320, 210, 2),
    "zoom-400": (160, 105, 4),
    "reflow-320": (320, 256, 4),
}
MODES = {
    "light": {},
    "dark": {"appearance": "dark"},
    "forced-colors": {"forced_colors": "active"},
    "forced-colors-dark": {"forced_colors": "active", "color_scheme": "dark"},
    "reduced-motion": {"reduced_motion": "reduce"},
    "contrast-more": {"contrast": "more"},
    "contrast-more-dark": {"contrast": "more", "appearance": "dark"},
}


@pytest.fixture(scope="module")
def browser(missing_prerequisite):
    if not (DIST / "index.html").is_file():
        missing_prerequisite("Build desktop/dist with npm ci and npm run build first")
    if not AXE.is_file():
        missing_prerequisite("Install desktop dev dependencies with npm ci first")
    from playwright.sync_api import sync_playwright

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(DIST))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with sync_playwright() as playwright:
            chromium = playwright.chromium.launch(
                executable_path=find_bundled(), chromium_sandbox=True
            )
            chromium.origin = f"http://127.0.0.1:{server.server_port}"
            try:
                yield chromium
            finally:
                chromium.close()
    finally:
        server.shutdown()
        thread.join()


@dataclass
class Session:
    page: object
    size: tuple[int, int, int]
    mode: str
    errors: list

    def evaluate(self, script, arg=None):
        return self.page.evaluate(script, arg)


def open_ui(browser, size="minimum", mode="light", transport="native") -> Session:
    from playwright.sync_api import expect

    width, height, scale = SIZES[size]
    options = MODES[mode]
    context = browser.new_context(
        viewport={"width": width, "height": height},
        device_scale_factor=scale,
        reduced_motion=options.get("reduced_motion", "no-preference"),
        forced_colors=options.get("forced_colors", "none"),
        color_scheme=options.get("color_scheme", "light"),
        contrast=options.get("contrast", "no-preference"),
    )
    page = context.new_page()
    errors: list = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.add_init_script(PROBE.read_text(encoding="utf-8"))
    if transport == "native":
        page.add_init_script("window.__catalog = " + json.dumps(catalog()) + ";")
        page.add_init_script(NATIVE_STUB)
    else:
        stub_web_api(page)
    page.goto(browser.origin)
    if transport == "native":
        expect(page.locator("#theme-name")).to_have_text("bristol")
    else:
        expect(page.locator("#connect-server")).to_be_visible()
    if options.get("appearance") == "dark":
        page.locator("#appearance").evaluate("el => el.click()")
        expect(page.locator("#app")).to_have_attribute("data-appearance", "dark")
    return Session(page, SIZES[size], mode, errors)


def web_capabilities() -> dict:
    return {
        **catalog(),
        "schema_version": 1,
        "browser_adapter": True,
        "mounts": ["manuals"],
        "limits": {
            "upload_bytes": 50 * 1048576,
            "output_bytes": 200 * 1048576,
            "retention_seconds": 3600,
        },
        "pdf_engine": {"discovered": True},
        "storage_ready": True,
    }


def stub_web_api(page) -> None:
    capabilities = json.dumps(web_capabilities())

    def handle(route):
        path = route.request.url.split("/api/v1", 1)[1]
        if path == "/capabilities":
            route.fulfill(status=200, content_type="application/json", body=capabilities)
        elif path == "/jobs" and route.request.method == "POST":
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps(
                    {"error": {"code": "busy", "message": "The server is busy. " * 12}}
                ),
            )
        else:
            route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps({"error": {"code": "not_found", "message": "Missing"}}),
            )

    page.route("**/api/v1/**", handle)


# -- interface states ---------------------------------------------------------


def emit(page, payload: str) -> None:
    page.evaluate(f"window.__emitWorker({payload})")


def last_preview_id(page) -> str:
    page.wait_for_function("window.__calls.some(c => c.args?.message?.command === 'preview')")
    return page.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message.id"
    )


def choose_paths(page) -> None:
    page.locator("#choose-file").evaluate("el => el.click()")
    page.locator("#choose-output").evaluate("el => el.click()")


def show_preview(page) -> None:
    """Narrow windows show one pane at a time; switch to the preview."""
    tab = page.locator("#preview-tab")
    if tab.is_visible():
        tab.evaluate("el => el.click()")


def state_idle(page) -> None:
    pass


def state_details_open(page) -> None:
    for summary in page.locator("summary").all():
        if summary.is_visible() and not summary.evaluate("el => el.parentElement.open"):
            summary.evaluate("el => el.click()")


def state_tiled_details(page) -> None:
    """Tiled mode shows its own advanced fields, such as tile overlap."""
    from playwright.sync_api import expect

    page.locator("#output-mode").select_option("tiled")
    state_details_open(page)
    field = page.locator("#advanced-tile_overlap")
    expect(field).to_be_visible()
    field.fill("80")  # above the 50 mm maximum: the invalid state is checked too


def state_sandbox_error(page) -> None:
    """The Linux sandbox failure carries the longest built-in help text."""
    from playwright.sync_api import expect

    choose_paths(page)
    page.locator("#export").evaluate("el => el.click()")
    emit(
        page,
        """{schema_version:1,event:'error',id:'export',code:'browser_sandbox_unavailable',
        message_id:'error.browser_sandbox_unavailable',message:'sandbox unavailable',
        help_id:'help.browser_sandbox',help:'install the profile'}""",
    )
    expect(page.locator("#results-dialog")).to_be_visible()
    expect(page.locator("#error")).to_contain_text("LINUX-INSTALL.md")


def state_generating(page) -> None:
    from playwright.sync_api import expect

    choose_paths(page)
    show_preview(page)
    expect(page.locator("#preview-spinner")).to_be_visible()


def state_preview_warnings(page) -> None:
    from playwright.sync_api import expect

    choose_paths(page)
    preview_id = last_preview_id(page)
    page.evaluate(
        """id => {
        const canvas = document.createElement('canvas'); canvas.width = 600; canvas.height = 900;
        const g = canvas.getContext('2d'); g.fillStyle = '#fff'; g.fillRect(0, 0, 600, 900);
        g.fillStyle = '#123'; g.fillRect(40, 40, 400, 30);
        window.__emitWorker({schema_version:1,event:'preview',id,preview:{
            file:'field-notes-with-a-long-name.md',pages:null,width:1,height:1,
            text:'Field notes',image:canvas.toDataURL('image/png').split(',')[1],
            warnings:['DOCUMENT_LANGUAGE: Technical language warning',
                      'Missing image: figures/' + 'very-long-path/'.repeat(8) + 'plot.png']}});
    }""",
        preview_id,
    )
    show_preview(page)
    expect(page.locator("#preview-warning-toggle")).to_be_visible()
    page.locator("#preview-warning-toggle").evaluate("el => el.click()")
    expect(page.locator("#preview-warnings")).to_be_visible()


def state_settings_after_preview(page) -> None:
    state_preview_warnings(page)
    tab = page.locator("#settings-tab")
    if tab.is_visible():
        tab.evaluate("el => el.click()")


def state_exporting(page) -> None:
    from playwright.sync_api import expect

    choose_paths(page)
    page.locator("#export").evaluate("el => el.click()")
    emit(page, "{schema_version:1,event:'progress',file:'chapter-one.md',done:1,total:4}")
    emit(page, "{schema_version:1,event:'activity',id:'export',phase:'print pdf'}")
    expect(page.locator("#cancel")).to_be_visible()


def state_results(page) -> None:
    from playwright.sync_api import expect

    emit(
        page,
        """{schema_version:1,event:'result',id:'export',result:{
        status:'completed_with_warnings',total_documents:3,output_dir:'/selected/output',
        elapsed_seconds:12.5,report:{engine:'fixture Chromium',documents:[
          {file:'notes.md',pages:4,figures:2,missing_images:['absent.png'],diagram_errors:[],
           warnings:['DOCUMENT_LANGUAGE: Technical language warning']},
          {file:'reports/' + 'quarterly-'.repeat(12) + 'summary.docx',pages:12,figures:0,
           missing_images:[],diagram_errors:['Diagram 3 failed: parse error on line 2'],warnings:[]},
          {file:'table.csv',pages:1,figures:0,missing_images:[],diagram_errors:[],warnings:[]}]}}}""",
    )
    page.locator("#view-results").evaluate("el => el.click()")
    expect(page.locator("#results-dialog")).to_be_visible()
    first = page.locator("#results .warning-disclosure summary").first
    if first.count():
        first.evaluate("el => el.click()")


def state_error(page) -> None:
    from playwright.sync_api import expect

    choose_paths(page)
    page.locator("#export").evaluate("el => el.click()")
    emit(
        page,
        """{schema_version:1,event:'error',id:'export',code:'response_too_large',
        message:'Could not export this document. '.repeat(20) +
        'Full report: /selected/output/' + 'nested/'.repeat(10) + '_export-report.json'}""",
    )
    expect(page.locator("#results-dialog")).to_be_visible()


def state_theme_dialog(page) -> None:
    from playwright.sync_api import expect

    page.locator("#choose-theme").evaluate("el => el.click()")
    expect(page.locator("#theme-dialog")).to_be_visible()


def state_save_dialog(page) -> None:
    from playwright.sync_api import expect

    page.locator("#save-copy").evaluate("el => el.click()")
    expect(page.locator("#save-dialog")).to_be_visible()
    page.locator("#copy-name").fill("Not A Valid Name")
    page.locator("#confirm-copy").evaluate("el => el.click()")


def state_delete_dialog(page) -> None:
    from playwright.sync_api import expect

    page.evaluate(
        """path => {
        const catalog = structuredClone(window.__catalog);
        const base = catalog.themes.find(t => t.name === 'midnight');
        catalog.themes.push({...base, name:'night-reading', builtin:false, kind:'user', path,
            file:'night-reading.json', description:'Saved from the app', contrast_failures:[]});
        window.__emitWorker({schema_version:1,event:'catalog',id:'catalog',catalog});
    }""",
        USER_THEME_PATH,
    )
    page.locator("#choose-theme").evaluate("el => el.click()")
    page.get_by_role("button", name="night-reading", exact=True).evaluate("el => el.click()")
    page.locator("#delete-theme").evaluate("el => el.click()")
    expect(page.locator("#delete-dialog")).to_be_visible()
    emit(page, "{schema_version:1,event:'error',id:'delete',message:'Permission denied'}")


def state_updates(page) -> None:
    from playwright.sync_api import expect

    page.evaluate(
        """window.__updateRelease = {version: '1.0.1', notes: 'Fixes. '.repeat(40),
        artifact: {url: 'https://example.invalid/' + 'path/'.repeat(10) + 'app.exe',
        sha256: 'a'.repeat(64), size: 19, signature: 'fixture'}}"""
    )
    page.get_by_role("button", name="Updates", exact=True).evaluate("el => el.click()")
    dialog = page.locator("#updates-dialog")
    expect(dialog).to_be_visible()
    dialog.locator("#updates-enabled").check(force=True)
    dialog.locator("#updates-check").evaluate("el => el.click()")
    dialog.locator("#updates-download").evaluate("el => el.click()")
    expect(dialog.locator("#updates-install")).to_be_visible()


def state_web_disconnected(page) -> None:
    pass


def state_web_connected(page) -> None:
    from playwright.sync_api import expect

    page.locator("#api-credential").fill("x" * 40)
    page.locator("#connect-server").evaluate("el => el.click()")
    expect(page.locator("#connection-note")).to_contain_text("Connected")
    page.locator("#upload-file").set_input_files(
        files=[{"name": "notes.md", "mimeType": "text/markdown", "buffer": b"# Notes\n"}]
    )


def state_web_error(page) -> None:
    from playwright.sync_api import expect

    state_web_connected(page)
    page.locator("#export").evaluate("el => el.click()")
    expect(page.locator("#results-dialog")).to_be_visible()


NATIVE_STATES: dict[str, Callable] = {
    "idle": state_idle,
    "details-open": state_details_open,
    "tiled-details": state_tiled_details,
    "generating": state_generating,
    "preview-warnings": state_preview_warnings,
    "settings-after-preview": state_settings_after_preview,
    "exporting": state_exporting,
    "results": state_results,
    "error": state_error,
    "sandbox-error": state_sandbox_error,
    "theme-dialog": state_theme_dialog,
    "save-dialog": state_save_dialog,
    "delete-dialog": state_delete_dialog,
    "updates": state_updates,
}
WEB_STATES: dict[str, Callable] = {
    "web-disconnected": state_web_disconnected,
    "web-connected": state_web_connected,
    "web-error": state_web_error,
}
STATES = {**NATIVE_STATES, **WEB_STATES}


def reach_state(browser, state, size="minimum", mode="light") -> Session:
    transport = "web" if state in WEB_STATES else "native"
    session = open_ui(browser, size, mode, transport)
    STATES[state](session.page)
    session.page.wait_for_timeout(50)
    return session


def close(session: Session) -> None:
    errors = session.errors
    session.page.context.close()
    assert not errors, errors


# -- checks --------------------------------------------------------------------


def axe(session: Session, rules: list[str] | None = None) -> list[str]:
    page = session.page
    page.add_script_tag(content=AXE.read_text(encoding="utf-8"))
    options = (
        {"runOnly": {"type": "rule", "values": rules}}
        if rules
        else {"runOnly": {"type": "tag", "values": AXE_TAGS}}
    )
    violations = page.evaluate(
        """options => axe.run(document, options).then(result => result.violations.map(v =>
            `${v.id} (${v.impact}): ${v.help} — ` +
            v.nodes.slice(0, 6).map(n => n.target.join(' ') +
                (n.failureSummary ? ' [' + n.failureSummary.replace(/\\s+/g, ' ') + ']' : '')
            ).join('; ')))""",
        options,
    )
    return violations


def layout_problems(session: Session, *, allow_ellipsis: bool) -> list[str]:
    return session.evaluate(
        """allowEllipsis => [
            ...window.__probe.controlProblems(),
            ...window.__probe.horizontalScroll(),
            ...window.__probe.clippedText({allowEllipsis}),
            ...window.__probe.overlaps(),
        ]""",
        allow_ellipsis,
    )


def _pixels(png: bytes):
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    return image, image.load()


@functools.lru_cache(maxsize=65536)
def _luminance(pixel) -> float:
    return relative_luminance("#%02x%02x%02x" % pixel)


def _strong_changes(before: bytes, after: bytes) -> int:
    """Pixels that differ between two captures with at least 3:1 contrast."""
    from PIL import ImageChops

    a, pa = _pixels(before)
    b, pb = _pixels(after)
    box = ImageChops.difference(a, b).getbbox()
    if not box:
        return 0
    count = 0
    for x in range(box[0], box[2]):
        for y in range(box[1], box[3]):
            if pa[x, y] != pb[x, y]:
                low, high = sorted((_luminance(pa[x, y]), _luminance(pb[x, y])))
                if (high + 0.05) / (low + 0.05) >= 3:
                    count += 1
    return count


def focus_indicator(session: Session, info: dict) -> str | None:
    """Compare the focused control with the same pixels after blur.

    The indicator must change at least a one-CSS-pixel ring's worth of pixels
    around the control, with 3:1 contrast between the focused and unfocused
    colour of each counted pixel.
    """
    page = session.page
    width, height, scale = session.size
    pad = 6
    rect = info["rect"]
    left = max(0, rect["x"] - pad)
    top = max(0, rect["y"] - pad)
    right = min(width, rect["x"] + rect["width"] + pad)
    bottom = min(height, rect["y"] + rect["height"] + pad)
    if right - left < 1 or bottom - top < 1:
        return f"{info['name']} is focused outside the window"
    clip = {"x": left, "y": top, "width": right - left, "height": bottom - top}
    focused = page.screenshot(clip=clip, animations="disabled", caret="hide")
    page.evaluate("document.activeElement.blur()")
    plain = page.screenshot(clip=clip, animations="disabled", caret="hide")
    changed = _strong_changes(focused, plain)
    visible_w = min(rect["width"], right - left)
    visible_h = min(rect["height"], bottom - top)
    ring = 2 * (visible_w + visible_h) * scale * scale
    if changed < ring:
        return f"{info['name']}: focus indicator changes {changed} pixels at 3:1, needs {int(ring)}"
    return None


def keyboard_walk(session: Session, limit: int = 90) -> tuple[list[str], list[str]]:
    """Tab through the current scope and check every stop."""
    page = session.page
    problems: list[str] = []
    stops: list[str] = []
    page.evaluate("document.activeElement && document.activeElement.blur()")
    first = None
    for _ in range(limit):
        page.keyboard.press("Tab")
        marker = page.evaluate(
            """() => {
            const el = document.activeElement;
            if (!el || el === document.body) return null;
            if (!el.dataset.walk) el.dataset.walk = String(Math.random()).slice(2);
            return el.dataset.walk;
        }"""
        )
        if marker is None:
            continue
        if marker == first:
            break
        first = first or marker
        info = page.evaluate("window.__probe.focusInfo()")
        stops.append(info["name"])
        if not info["focusVisible"]:
            problems.append(f"{info['name']} does not match :focus-visible")
        if info["visible"]["width"] < 1 or info["visible"]["height"] < 1:
            problems.append(f"{info['name']} is focused but not on screen")
            continue
        if info["coveredBy"]:
            problems.append(f"{info['name']} is focused under {info['coveredBy']}")
        problem = focus_indicator(session, info)
        if problem:
            problems.append(problem)
        # Blurring leaves the sequential focus starting point on this control.
    else:
        problems.append(f"focus did not cycle within {limit} stops")
    return stops, problems


# -- tests ----------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["light", "dark", "contrast-more", "contrast-more-dark"])
@pytest.mark.parametrize("state", list(STATES))
def test_axe_finds_no_violations(browser, state, mode):
    session = reach_state(browser, state, "minimum", mode)
    try:
        violations = axe(session)
        if mode.startswith("contrast-more"):
            violations += axe(session, ["color-contrast-enhanced"])
        assert not violations, "\n".join(violations)
    finally:
        close(session)


@pytest.mark.parametrize("state", list(STATES))
def test_axe_finds_no_violations_when_zoomed(browser, state):
    session = reach_state(browser, state, "zoom-200")
    try:
        violations = axe(session)
        assert not violations, "\n".join(violations)
    finally:
        close(session)


@pytest.mark.parametrize("size", ["minimum", "os-scale-200", "zoom-200", "zoom-400", "reflow-320"])
@pytest.mark.parametrize("state", list(STATES))
def test_every_control_is_reachable_unclipped_and_uncovered(browser, state, size):
    session = reach_state(browser, state, size)
    try:
        # At full size a path or status line may end in an ellipsis when its
        # complete text is also available; once zoomed, text must wrap.
        problems = layout_problems(session, allow_ellipsis=size in {"minimum", "os-scale-200"})
        assert not problems, "\n".join(problems)
    finally:
        close(session)


@pytest.mark.parametrize("mode", ["forced-colors", "forced-colors-dark", "contrast-more"])
@pytest.mark.parametrize("state", list(STATES))
def test_display_preferences_keep_layout_intact(browser, state, mode):
    session = reach_state(browser, state, "minimum", mode)
    try:
        problems = layout_problems(session, allow_ellipsis=True)
        assert not problems, "\n".join(problems)
    finally:
        close(session)


FOCUS_STATES = [
    "idle",
    "details-open",
    "tiled-details",
    "preview-warnings",
    "exporting",
    "results",
    "error",
    "theme-dialog",
    "save-dialog",
    "delete-dialog",
    "updates",
    "web-connected",
]


@pytest.mark.parametrize("mode", ["light", "dark", "forced-colors", "contrast-more"])
@pytest.mark.parametrize("state", FOCUS_STATES)
def test_every_keyboard_stop_shows_a_visible_focus_indicator(browser, state, mode):
    session = reach_state(browser, state, "minimum", mode)
    try:
        stops, problems = keyboard_walk(session)
        assert len(stops) >= 2, stops
        assert not problems, "\n".join(problems)
    finally:
        close(session)


def test_focus_walk_detects_a_missing_indicator(browser):
    # Guards the focus test against passing because it cannot see outlines.
    session = reach_state(browser, "idle")
    try:
        session.page.add_style_tag(
            content="*:focus-visible { outline: none !important; box-shadow: none !important; }"
        )
        _, problems = keyboard_walk(session, limit=6)
        assert any("focus indicator changes" in problem for problem in problems)
    finally:
        close(session)


@pytest.mark.parametrize(
    "state", ["idle", "details-open", "results", "theme-dialog", "web-connected"]
)
def test_keyboard_stops_stay_visible_when_zoomed(browser, state):
    session = reach_state(browser, state, "zoom-200")
    try:
        stops, problems = keyboard_walk(session)
        assert len(stops) >= 2, stops
        assert not problems, "\n".join(problems)
    finally:
        close(session)


@pytest.mark.parametrize("state", list(STATES))
def test_reduced_motion_stops_every_animation_and_transition(browser, state):
    session = reach_state(browser, state, "minimum", "reduced-motion")
    try:
        problems = session.evaluate("window.__probe.motion()")
        assert not problems, "\n".join(problems)
    finally:
        close(session)


def test_generating_preview_animates_without_a_motion_preference(browser):
    # Guards the reduced-motion test against passing because nothing moves.
    session = reach_state(browser, "generating", "minimum", "light")
    try:
        assert session.evaluate("window.__probe.motion()")
    finally:
        close(session)


def test_forced_colors_are_applied_and_indicators_stay_visible(browser):
    session = reach_state(browser, "generating", "minimum", "forced-colors")
    try:
        page = session.page
        assert page.evaluate("matchMedia('(forced-colors: active)').matches")
        spinner = page.locator("#preview-spinner")
        box = spinner.bounding_box()
        assert box
        image, pixels = _pixels(spinner.screenshot(animations="disabled"))
        colours = {pixels[x, y] for x in range(image.width) for y in range(image.height)}
        assert len(colours) > 1, "the preview progress bar is blank in forced colours"
    finally:
        close(session)


def test_forced_colors_keep_theme_swatches_and_pressed_state(browser):
    session = reach_state(browser, "theme-dialog", "minimum", "forced-colors")
    try:
        page = session.page
        swatch = page.locator("#themes .swatch").first
        image, pixels = _pixels(swatch.screenshot(animations="disabled"))
        colours = {pixels[x, y] for x in range(image.width) for y in range(image.height)}
        assert len(colours) > 2, "theme swatches lose their colours in forced colours"
        # Theme buttons have no border by design; forced colours must add one,
        # and the selected theme needs a heavier one.
        widths = page.evaluate(
            """() => [...document.querySelectorAll('#themes button')].filter(b => b.checkVisibility())
                .map(b => [b.getAttribute('aria-pressed'), parseFloat(getComputedStyle(b).borderTopWidth)])"""
        )
        assert widths and all(width >= 1 for _, width in widths)
        assert all(width >= 2 for pressed, width in widths if pressed == "true")
    finally:
        close(session)


@pytest.mark.parametrize("mode", ["light", "dark", "contrast-more", "contrast-more-dark"])
def test_form_field_boundaries_meet_non_text_contrast(browser, mode):
    """Text fields and selects are identified by their border (WCAG 1.4.11)."""
    session = reach_state(browser, "details-open", "minimum", mode)
    try:
        failures = session.evaluate(
            """() => {
            const parse = c => (c.match(/\\d+(\\.\\d+)?/g) || []).slice(0, 4).map(Number);
            const lum = ([r, g, b]) => [r, g, b].map(v => {
                v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
            }).reduce((s, v, i) => s + v * [0.2126, 0.7152, 0.0722][i], 0);
            const ratio = (a, b) => { const [x, y] = [lum(a), lum(b)].sort((m, n) => n - m);
                return (x + 0.05) / (y + 0.05); };
            const behind = el => { for (let n = el.parentElement; n; n = n.parentElement) {
                const c = parse(getComputedStyle(n).backgroundColor);
                if (c.length < 4 || c[3] > 0) return c; } return [255, 255, 255]; };
            const fields = [...document.querySelectorAll(
                'select, input[type=text], input:not([type]), input[type=number], input[type=password]')]
                .filter(el => el.getClientRects().length && !el.closest('dialog:not([open])'));
            return fields.flatMap(el => {
                const s = getComputedStyle(el);
                const border = parse(s.borderTopColor);
                const outside = behind(el);
                const fill = parse(s.backgroundColor);
                const inside = fill.length < 4 || fill[3] > 0 ? fill : outside;
                const best = Math.max(ratio(border, outside), ratio(inside, outside));
                return best >= 3 ? [] : [`${el.id || el.getAttribute('aria-label')}: border ${s.borderTopColor} is ${best.toFixed(2)}:1`];
            });
        }"""
        )
        assert not failures, "\n".join(failures)
    finally:
        close(session)
