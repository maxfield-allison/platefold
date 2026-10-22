"""Browser tests for the built UI; native operations are a controlled adapter.

Real worker/process/PDF integration is covered separately in test_worker.py.
"""

import functools
import http.server
import json
import re
import threading
from pathlib import Path

import pytest

from platefold.browser import find_bundled
from platefold.worker import catalog

pytestmark = pytest.mark.integration
DIST = Path(__file__).resolve().parents[1] / "desktop" / "dist"


def _pseudo_text(value: str) -> str:
    """Deterministic expansion test data; placeholders stay machine-stable."""
    accents = str.maketrans(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz",
        "ÅƁÇĐĖƑƓĦĪĴĶĿḾŃØƤɊŔŞŦŮṼŴẊẎŽåƀçđėƒɠħīĵķŀḿńøƥɋŕşŧŭṽŵẋẏž",
    )
    parts = re.split(r"(\{[A-Za-z][A-Za-z0-9_]*\})", value)
    expanded = "".join(part if part.startswith("{") else part.translate(accents) for part in parts)
    return f"⟦!! {expanded} {'~' * max(2, len(value) // 4)} !!⟧"


def _pseudo_catalog() -> dict:
    path = Path(__file__).resolve().parents[1] / "platefold" / "locales" / "en.json"
    source = json.loads(path.read_text(encoding="utf-8"))
    messages = {}
    for message_id, entry in source["messages"].items():
        messages[message_id] = (
            _pseudo_text(entry)
            if isinstance(entry, str)
            else {category: _pseudo_text(text) for category, text in entry.items()}
        )
    return {
        **source,
        "locale": "ar-XB",
        "name": "Mechanical RTL test",
        "direction": "rtl",
        "translation_method": "mechanical-test",
        "messages": messages,
    }


# Replaces the Tauri bridge: native commands are recorded, and tests drive
# worker events and update results through the window.__* hooks.
NATIVE_STUB = """
const callbacks = new Map(), listeners = new Map(); let id = 0;
window.__calls = [];
window.__updateStatus = {
    preferences: {enabled: false, channel: 'stable'}, version: '0.3.0-dev.6',
    configured: true, installable: true, distribution: 'nsis', phase: 'idle',
    pending_version: null,
    guidance: 'PlateFold installs only after each approval.', warning: null
};
window.__updateRelease = null;
window.__emitWorker = payload => callbacks.get(listeners.get('worker-event'))({payload});
window.__exitWorker = payload => callbacks.get(listeners.get('worker-exit'))({payload});
window.__emitNative = event => callbacks.get(listeners.get(event))({payload: null});
window.__TAURI_INTERNALS__ = {
    transformCallback: callback => { callbacks.set(++id, callback); return id; },
    invoke: async (command, args) => {
        window.__calls.push({command, args});
        if (command === 'update_status') return window.__updateStatus;
        if (command === 'update_preferences') {
            if (window.__updateSaveError) throw 'Save failed';
            window.__updateStatus.preferences = args.preferences;
            return window.__updateStatus;
        }
        if (command === 'check_updates') {
            if (window.__updatePending) return new Promise(resolve => window.__resolveUpdate = resolve);
            if (window.__updateError) throw window.__updateError;
            return window.__updateRelease;
        }
        if (command === 'download_update') {
            if (window.__downloadPending) return new Promise((resolve, reject) => {
                window.__resolveDownload = resolve; window.__rejectDownload = reject;
            });
            if (window.__downloadError) throw window.__downloadError;
            window.__updateStatus.phase = 'verified';
            return {version: '0.3.0', size: 19, sha256: 'a'.repeat(64)};
        }
        if (command === 'cancel_update') {
            if (window.__rejectDownload) {
                window.__rejectDownload('Update download cancelled; no partial bytes were kept');
                window.__rejectDownload = null;
            }
            window.__updateStatus.phase = 'idle';
        }
        if (command === 'install_update') {
            if (window.__installPending) return new Promise(resolve => window.__resolveInstall = resolve);
            if (window.__installError) {
                // What the native side records for a failure it cannot retry.
                if (window.__installFailureStatus) Object.assign(window.__updateStatus, window.__installFailureStatus);
                throw window.__installError;
            }
            window.__updateStatus.phase = 'installed';
            // What the native side records after a success, such as a folder left behind.
            if (window.__installSuccessStatus) Object.assign(window.__updateStatus, window.__installSuccessStatus);
            // Native code says whether it restarts now; by default it does not.
            return {...window.__updateStatus, restarting: !!window.__installRestarting};
        }
        if (command === 'restart_update') return;
        if (command === 'plugin:event|listen') { listeners.set(args.event, args.handler); return args.handler; }
        if (command === 'choose_path') return args.kind === 'output' ? '/selected/output' : '/selected/notes.md';
        if (command === 'send_worker' && args.message.command === 'catalog') {
            queueMicrotask(() => window.__emitWorker({schema_version:1,event:'catalog',id:'catalog',catalog:window.__catalog}));
        }
    }
};
"""


@pytest.fixture
def ui(missing_prerequisite):
    if not (DIST / "index.html").is_file():
        missing_prerequisite("Build desktop/dist with npm ci and npm run build first")
    from playwright.sync_api import expect, sync_playwright

    class Handler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Handler, directory=str(DIST))
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=find_bundled(), chromium_sandbox=True)
        page = browser.new_page(viewport={"width": 1280, "height": 1000})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("window.__catalog = " + json.dumps(catalog()) + ";")
        page.add_init_script(NATIVE_STUB)
        try:
            page.goto(f"http://127.0.0.1:{server.server_port}")
            expect(page.locator("#theme-name")).to_have_text("bristol")
            yield page
            assert not errors, errors
        finally:
            browser.close()
            server.shutdown()
            thread.join()


@pytest.mark.parametrize("exporting", [False, True])
def test_worker_exit_disables_export_and_clears_busy_controls(ui, exporting):
    from playwright.sync_api import expect

    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    if exporting:
        ui.get_by_role("button", name="Export PDFs").click()
    ui.evaluate("window.__exitWorker('Worker stopped. Reopen the app to reconnect.')")
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_disabled()
    expect(ui.get_by_role("button", name="Cancel export")).not_to_be_visible()
    expect(ui.get_by_role("status")).to_contain_text("Reopen the app")


def test_paper_controls_preserve_online_default_and_send_print_choices(ui):
    from playwright.sync_api import expect

    ui.add_init_script('Object.defineProperty(navigator, "language", {get: () => "en-GB"})')
    ui.reload()
    expect(ui.get_by_label("Output mode", exact=True)).to_have_value("online")
    expect(ui.locator("#paper-label")).to_have_text("LETTER")
    ui.get_by_label("Output mode", exact=True).select_option("print")
    expect(ui.locator("#paper-label")).to_have_text("A4")
    # Overlap only means something for tiled print.
    expect(ui.locator("#advanced-tile_overlap")).to_be_disabled()
    ui.get_by_label("Paper", exact=True).select_option("letter")
    ui.get_by_label("Output mode", exact=True).select_option("tiled")
    expect(ui.get_by_label("Paper", exact=True)).to_have_value("letter")
    ui.get_by_label("Page margins", exact=True).select_option("bottom-right")
    ui.get_by_label("Orientation", exact=True).select_option("landscape")
    expect(ui.locator("#margin-note")).to_contain_text("taping")
    ui.locator("#advanced-layout summary").click()
    overlap = ui.get_by_label("Tile overlap (mm)")
    expect(overlap).to_be_enabled()
    overlap.fill("6")
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.get_by_role("button", name="Export PDFs").click()
    request = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'export').at(-1).args.message"
    )
    settings = request["settings"]
    assert {
        key: settings[key]
        for key in ("output_mode", "preset", "margin_mode", "orientation", "locale_hint")
    } == {
        "output_mode": "tiled",
        "preset": "letter",
        "margin_mode": "bottom-right",
        "orientation": "landscape",
        "locale_hint": "en-GB",
    }
    assert settings["tile_overlap"] == 6


def test_mechanical_pseudolocale_expands_and_keeps_document_locale_separate(ui):
    from playwright.sync_api import expect

    ui.add_init_script(
        "window.__PLATEFOLD_I18N_TEST__ = " + json.dumps(_pseudo_catalog(), ensure_ascii=False)
    )
    ui.add_init_script('Object.defineProperty(navigator, "language", {get: () => "en-GB"})')
    ui.reload()

    expect(ui.locator("html")).to_have_attribute("lang", "ar-XB")
    expect(ui.locator("html")).to_have_attribute("dir", "rtl")
    expect(ui.locator(".tagline")).to_contain_text("⟦!!")
    expect(ui.get_by_role("button", name=re.compile("Çħøøşė ƒīŀė"))).to_be_visible()
    expect(ui.locator("#delete-summary")).to_contain_text("⟦!!")
    expect(ui.locator("#delete-summary code")).to_have_count(1)
    assert ui.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")

    expect(ui.locator("#document-language-help")).to_contain_text("⟦!!")
    expect(ui.locator("#document-language")).to_have_attribute("placeholder", re.compile("⟦!!"))
    ui.locator("#document-language").fill("fr-CA")
    ui.locator("#choose-file").click()
    ui.locator("#choose-output").click()
    ui.locator("#export").click()
    request = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'export').at(-1).args.message"
    )
    assert request["ui_locale"] == "ar-XB"
    assert request["settings"]["locale_hint"] == "en-GB"
    assert request["settings"]["document_language"] == "fr-CA"


def test_catalog_messages_are_never_treated_as_html(ui):
    from playwright.sync_api import expect

    catalog = _pseudo_catalog()
    payload = '<img id="i18n-injection" src=x onerror="window.__i18nInjected=true">'
    catalog["messages"]["app.tagline"] = payload
    catalog["messages"]["theme.dark_advisory"] = payload
    ui.add_init_script(
        "window.__PLATEFOLD_I18N_TEST__ = " + json.dumps(catalog, ensure_ascii=False)
    )
    ui.reload()

    expect(ui.locator(".tagline")).to_have_text(payload)
    expect(ui.locator("#mode-advisory")).to_have_text(payload)
    expect(ui.locator("#i18n-injection")).to_have_count(0)
    assert ui.evaluate("window.__i18nInjected") is None


def test_advanced_layout_overrides_preview_export_and_reset(ui):
    from playwright.sync_api import expect

    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    expect(ui.locator("#advanced-layout")).not_to_have_attribute("open", "")
    ui.get_by_text("Advanced layout", exact=True).click()
    ui.get_by_label("Sheet width (mm)", exact=True).fill("230.5")
    ui.get_by_label("Sheet height (mm)", exact=True).fill("340.25")
    ui.get_by_label("Minimum plate scale (0-1)", exact=True).fill("0.95")
    ui.get_by_label("Body text size (pt)", exact=True).fill("12")
    ui.get_by_label("Image resolution (DPI)", exact=True).fill("240")
    ui.get_by_label("Use exact margins on every page").check()
    for edge, value in zip(["Top", "Right", "Bottom", "Left"], [3, 17, 21, 7], strict=True):
        ui.get_by_label(f"{edge} margin (mm)", exact=True).fill(str(value))
    ui.get_by_label("Paper", exact=True).select_option("a3")
    ui.get_by_label("Page margins", exact=True).select_option("none")
    expect(ui.locator("#margin-note")).to_contain_text("Exact margins from Advanced layout")
    expect(ui.get_by_label("Sheet width (mm)", exact=True)).to_have_value("230.5")
    expect(ui.locator("#paper-label")).to_have_text("CUSTOM SHEET")
    ui.get_by_label("Output mode", exact=True).select_option("tiled")
    expect(ui.get_by_label("Minimum plate scale (0-1)", exact=True)).to_be_disabled()
    ui.wait_for_function(
        "window.__calls.some(c => c.args?.message?.command === 'preview' && c.args.message.settings?.output_mode === 'tiled')"
    )
    preview = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message.settings"
    )
    assert preview["page_width"] == 230.5 and preview["page_height"] == 340.25
    assert preview["margin_edges"] == [3, 17, 21, 7]
    assert preview["body_size_pt"] == 12 and preview["image_dpi"] == 240
    assert "plate_fit" not in preview
    ui.get_by_role("button", name="Export PDFs").click()
    exported = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'export').at(-1).args.message.settings"
    )
    assert exported == preview
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'error',id:'export',message:'Test export stopped'})"
    )
    ui.get_by_role("button", name="Close results").click()
    ui.get_by_label("Output mode", exact=True).select_option("online")
    expect(ui.get_by_label("Minimum plate scale (0-1)", exact=True)).to_have_value("0.95")
    ui.get_by_role("button", name="Reset advanced overrides").click()
    expect(ui.get_by_label("Sheet width (mm)", exact=True)).to_have_value("")
    expect(ui.get_by_label("Use exact margins on every page")).not_to_be_checked()
    expect(ui.locator("#paper-label")).to_have_text("A3")
    ui.get_by_role("button", name="Export PDFs").click()
    reset = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'export').at(-1).args.message.settings"
    )
    assert reset["preset"] == "a3"
    assert not (
        {"page_width", "page_height", "margin_edges", "body_size_pt", "image_dpi", "plate_fit"}
        & reset.keys()
    )


def test_invalid_advanced_values_block_export_until_corrected(ui):
    from playwright.sync_api import expect

    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.get_by_text("Advanced layout", exact=True).click()
    ui.get_by_label("Minimum inline scale (0-1)", exact=True).fill("2")
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_disabled()
    expect(ui.locator("#preview-status")).to_contain_text("Check Advanced layout")
    ui.get_by_label("Minimum inline scale (0-1)", exact=True).fill("0.9")
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_enabled()
    ui.get_by_label("Use exact margins on every page").check()
    ui.get_by_label("Top margin (mm)", exact=True).fill("")
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_disabled()
    ui.get_by_role("button", name="Reset advanced overrides").click()
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_enabled()


def test_large_report_error_keeps_output_location_and_allows_another_export(ui):
    from playwright.sync_api import expect

    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.get_by_role("button", name="Export PDFs").click()
    ui.evaluate("""window.__emitWorker({schema_version:1,event:'error',id:'export',
        code:'response_too_large',message:'Full report: /selected/output/_export-report.json'})""")
    expect(ui.get_by_role("status")).to_contain_text("report is too large")
    expect(ui.locator("#results-dialog")).to_be_visible()
    expect(ui.locator("#error")).to_contain_text("/selected/output/_export-report.json")
    ui.get_by_role("button", name="Close results").click()
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_enabled()
    expect(ui.get_by_role("button", name="Cancel export")).not_to_be_visible()


def test_theme_preview_and_application_appearance_are_independent(ui):
    from playwright.sync_api import expect

    expect(ui.get_by_role("button", name="Export PDFs")).to_be_disabled()
    ui.get_by_label("Font set", exact=True).select_option("georgia")
    assert "Georgia" in ui.locator("#paper").evaluate("el => getComputedStyle(el).fontFamily")
    ui.locator("#choose-theme").click()
    ui.get_by_role("button", name="midnight", exact=True).click()
    expect(ui.locator("#theme-dialog")).not_to_be_visible()
    expect(ui.locator("#choose-theme")).to_be_focused()
    assert ui.locator("#app").get_attribute("data-appearance") is None
    expected = ui.evaluate("window.__catalog.themes.find(t => t.name === 'midnight').colors.page")
    assert ui.locator("#paper").evaluate("el => el.style.getPropertyValue('--page')") == expected
    ui.get_by_role("button", name="Dark interface").click()
    assert ui.locator("#app").get_attribute("data-appearance") == "dark"
    ui.get_by_text("Customise palette", exact=True).click()
    ui.get_by_label("accent colour", exact=True).fill("#123456")
    assert ui.locator("#paper").evaluate("el => el.style.getPropertyValue('--accent')") == "#123456"
    ui.get_by_role("button", name="Revert").click()
    assert ui.locator("#paper").evaluate("el => el.style.getPropertyValue('--accent')") != "#123456"
    ui.get_by_role("button", name="Save as a copy").click()
    expect(ui.get_by_label("Theme name")).to_be_focused()
    ui.get_by_role("button", name="Cancel", exact=True).click()
    expect(ui.locator("#save-dialog")).not_to_be_visible()


def test_export_flow_busy_cancel_and_safe_result_text(ui):
    from playwright.sync_api import expect

    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.get_by_role("button", name="Export PDFs").click()
    expect(ui.get_by_role("button", name="Choose file", exact=True)).to_be_disabled()
    request = ui.evaluate(
        "window.__calls.filter(c => c.command === 'send_worker').at(-1).args.message"
    )
    assert request["command"] == "export" and request["settings"]["font_set"] == "sora"
    ui.get_by_role("button", name="Cancel export").click()
    assert ui.evaluate("window.__calls.at(-1).args.message.command") == "cancel"
    ui.evaluate("""window.__emitWorker({schema_version:1,event:'result',id:'export',result:{
        status:'cancelled',total_documents:2,output_dir:'/selected/output',report:{engine:'test Chromium',documents:[{
        file:'<img src=x onerror=alert(1)>.md',pages:2,figures:0,missing_images:['absent.png'],diagram_errors:[],warnings:[]}]}}})""")
    expect(ui.get_by_role("status")).to_contain_text("Cancelled")
    expect(ui.locator("#results img")).to_have_count(0)
    expect(ui.locator("#results")).to_contain_text("Missing image: absent.png")
    ui.get_by_role("button", name="View results").click()
    ui.get_by_role("button", name="Open PDF for").click()
    assert {"command": "open_result", "args": {"index": 0}} in ui.evaluate("window.__calls")
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_enabled()


@pytest.mark.parametrize("size", [(1280, 860), (960, 700)])
def test_export_controls_fit_without_horizontal_scrolling(ui, size):
    from playwright.sync_api import expect

    ui.set_viewport_size({"width": size[0], "height": size[1]})
    assert ui.evaluate("document.documentElement.scrollWidth <= innerWidth")
    for label in ("Paper", "Font set"):
        bounds = ui.get_by_label(label, exact=True).bounding_box()
        assert bounds and bounds["y"] + bounds["height"] < size[1]
    expect(ui.get_by_label("Source path", exact=True)).not_to_be_visible()
    ui.get_by_text("Enter source path", exact=True).click()
    expect(ui.get_by_label("Source path", exact=True)).to_be_visible()
    ui.get_by_text("Enter source path", exact=True).click()
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    button = ui.get_by_role("button", name="Export PDFs")
    expect(button).to_be_enabled()
    bounds = button.bounding_box()
    assert bounds and bounds["y"] + bounds["height"] <= size[1]


@pytest.mark.parametrize("size", [(1280, 860), (1280, 640), (880, 640), (820, 470), (800, 500)])
@pytest.mark.parametrize("dark", [False, True])
def test_independent_panes_and_modal_results_keep_workspace_stationary(ui, size, dark):
    from playwright.sync_api import expect

    ui.set_viewport_size({"width": size[0], "height": size[1]})
    if dark:
        ui.get_by_role("button", name="Dark interface").click()
    for summary in ("Enter source path", "Enter output path", "Customise palette"):
        ui.get_by_text(summary, exact=True).click()
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    source = ui.locator(".source-bar").bounding_box()
    actions = ui.locator(".job-panel").bounding_box()
    preview = ui.locator("#preview-scroll")
    settings = ui.locator("#settings-pane")
    assert settings.evaluate("el => getComputedStyle(el).colorScheme") == (
        "dark" if dark else "light"
    )
    settings.evaluate("el => el.scrollTo(0, el.scrollHeight)")
    expect(ui.get_by_role("button", name="Save as a copy")).to_be_in_viewport()
    assert preview.evaluate("el => el.scrollTop") == 0
    assert ui.locator(".source-bar").bounding_box() == source
    assert ui.locator(".job-panel").bounding_box() == actions
    if size[0] <= 900:
        ui.get_by_role("tab", name="Settings", exact=True).focus()
        ui.get_by_role("tab", name="Settings", exact=True).press("ArrowRight")
        expect(ui.get_by_role("tab", name="Preview", exact=True)).to_be_focused()
        expect(settings).not_to_be_visible()
    preview.evaluate("el => el.scrollTo(0, el.scrollHeight)")
    expect(ui.locator(".preview-note")).to_be_in_viewport()
    assert ui.locator(".source-bar").bounding_box() == source
    assert ui.locator(".job-panel").bounding_box() == actions
    assert ui.evaluate("""() => [...document.querySelectorAll('*')].filter(el =>
        ['auto','scroll'].includes(getComputedStyle(el).overflowY) &&
        el.clientHeight > 0 && el.scrollHeight > el.clientHeight + 1
    ).every(el => el.classList.contains('pane-scroll'))""")
    if size[0] <= 900:
        ui.get_by_role("tab", name="Settings", exact=True).click()
    settings.evaluate("el => el.scrollTo(0, 0)")
    ui.locator("#choose-theme").click()
    expect(ui.locator("#theme-dialog")).to_be_visible()
    ui.get_by_role("button", name="Dark", exact=True).click()
    expect(ui.get_by_role("button", name="bristol", exact=True)).not_to_be_visible()
    ui.get_by_role("button", name="midnight", exact=True).click()
    expect(ui.locator("#choose-theme")).to_be_focused()
    ui.locator("#choose-theme").click()
    ui.keyboard.press("Escape")
    expect(ui.locator("#theme-dialog")).not_to_be_visible()
    expect(ui.locator("#choose-theme")).to_be_focused()
    ui.evaluate("""window.__emitWorker({schema_version:1,event:'result',id:'export',result:{
        status:'completed_with_warnings',total_documents:25,output_dir:'/selected/output',
        report:{engine:'test Chromium',documents:Array.from({length:25},(_,i)=>({
            file:`${i}-${'long-name-'.repeat(30)}.md`,pages:2,figures:0,
            missing_images:[],diagram_errors:[],warnings:['A warning. '.repeat(30)]
        }))}}})""")
    expect(ui.locator("#results-dialog")).not_to_be_visible()
    ui.get_by_role("button", name="View results").click()
    scroller = ui.get_by_role("region", name="Result details")
    assert scroller.evaluate("el => el.scrollWidth <= el.clientWidth")
    scroller.focus()
    scroller.press("End")
    expect(ui.get_by_role("button", name="Open PDF for").last).to_be_in_viewport()
    expect(ui.get_by_role("button", name="Close results")).to_be_in_viewport()
    ui.keyboard.press("Escape")
    expect(ui.get_by_role("button", name="View results")).to_be_focused()
    assert ui.locator(".source-bar").bounding_box() == source
    assert ui.evaluate("""() => {
        const root = document.documentElement;
        return root.scrollHeight <= innerHeight && root.scrollWidth <= innerWidth;
    }""")
    control = ui.get_by_role("button", name="Export PDFs")
    bounds = control.bounding_box()
    assert bounds and 0 <= bounds["y"] < bounds["y"] + bounds["height"] < size[1]
    control.click()
    expect(ui.get_by_role("button", name="Cancel export")).to_be_in_viewport()


def test_large_theme_catalog_and_errors_stay_inside_dialogs(ui):
    from playwright.sync_api import expect

    ui.set_viewport_size({"width": 800, "height": 500})
    ui.evaluate("""() => {
        const catalog = structuredClone(window.__catalog);
        catalog.themes.push(...Array.from({length:80}, (_,i) => ({
            ...catalog.themes[0], name:`custom-${i}`, builtin:false
        })));
        window.__emitWorker({schema_version:1,event:'catalog',catalog});
    }""")
    ui.locator("#choose-theme").click()
    scroller = ui.locator("#theme-dialog .dialog-scroll")
    scroller.focus()
    scroller.press("End")
    expect(ui.get_by_role("button", name="custom-79", exact=True)).to_be_in_viewport()
    expect(ui.get_by_role("button", name="Close theme picker")).to_be_in_viewport()
    ui.get_by_role("button", name="custom-79", exact=True).click()
    expect(ui.locator("#choose-theme")).to_be_focused()
    ui.evaluate("""window.__emitWorker({schema_version:1,event:'error',id:'export',
        message:'Could not export this document. '.repeat(100)})""")
    expect(ui.locator("#results-dialog")).to_be_visible()
    expect(ui.get_by_role("alert")).to_contain_text("Could not export")

    expect(ui.get_by_role("button", name="Close results")).to_be_in_viewport()
    assert ui.locator("#results-dialog .dialog-scroll").evaluate(
        "el => el.scrollWidth <= el.clientWidth"
    )
    ui.get_by_role("button", name="Close results").click()
    ui.get_by_role("button", name="View results").click()
    expect(ui.get_by_role("alert")).to_contain_text("Could not export")


def test_preview_generating_stale_replies_settings_and_retry(ui):
    from playwright.sync_api import expect

    ui.clock.install()
    ui.set_viewport_size({"width": 1280, "height": 640})
    ui.get_by_role("button", name="Choose file", exact=True).click()
    expect(ui.locator("#preview-status")).to_contain_text("Generating preview")
    expect(ui.locator("#preview-spinner")).to_be_visible()
    assert ui.locator("#preview-spinner").evaluate("el => el.getBoundingClientRect().width") >= 80
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_disabled()
    ui.wait_for_function("window.__calls.some(c => c.args?.message?.command === 'preview')")
    old = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message.id"
    )
    ui.get_by_label("Font set", exact=True).select_option("georgia")
    ui.get_by_label("Paper", exact=True).select_option("a4")
    ui.get_by_label("Contents page", exact=True).uncheck()
    ui.get_by_label("PDF bookmarks", exact=True).uncheck()
    ui.get_by_label("First CSV/TSV row is a header", exact=True).uncheck()
    ui.get_by_text("Customise palette", exact=True).click()
    ui.get_by_label("accent colour", exact=True).fill("#123456")
    ui.evaluate(
        "id => window.__emitWorker({schema_version:1,event:'preview_error',id,message:'stale error'})",
        old,
    )
    expect(ui.locator("#preview-status")).to_contain_text("Generating preview")
    # Finish the final settings debounce before choosing a reply ID. An earlier
    # request can already differ from `old` while a newer revision is pending.
    ui.clock.run_for(600)
    ui.wait_for_function(
        "id => window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message.id !== id",
        arg=old,
    )
    request = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message"
    )
    assert request["settings"]["font_set"] == "georgia"
    assert request["settings"]["preset"] == "a4"
    assert request["settings"]["colors"]["accent"] == "#123456"
    assert not request["settings"]["toc"] and not request["settings"]["bookmarks"]
    assert request["settings"]["csv_header"] is False
    ui.evaluate(
        "id => window.__emitWorker({schema_version:1,event:'preview_error',id,message:'Cannot render fixture'})",
        request["id"],
    )
    expect(ui.locator("#preview-status")).to_contain_text("Cannot render fixture")
    expect(ui.locator("#preview-status")).to_contain_text("How to fix: Retry the action")
    expect(ui.locator("#preview-spinner")).not_to_be_visible()
    expect(ui.locator("#results-dialog")).not_to_be_visible()
    ui.get_by_role("button", name="Refresh preview").click()
    expect(ui.locator("#preview-status")).to_contain_text("Generating preview")
    ui.wait_for_function(
        "id => window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message.id !== id",
        arg=request["id"],
    )
    ui.evaluate("""() => {
        const id=window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message.id;
        const canvas = document.createElement('canvas'); canvas.width=600; canvas.height=900;
        window.__emitWorker({schema_version:1,event:'preview',id,preview:{
            file:'notes.md',pages:null,width:1,height:1,text:'Real source text',warnings:[],
            image:canvas.toDataURL('image/png').split(',')[1]
        }});
    }""")
    expect(ui.locator("#preview-spinner")).not_to_be_visible()
    expect(ui.locator("#preview-status")).to_have_text("Preview ready.")
    expect(ui.locator("#preview-caption")).to_contain_text("notes.md - page 1")
    expect(ui.locator("#paper")).not_to_be_visible()
    ui.get_by_label("Font set", exact=True).select_option("sora")
    expect(ui.locator("#preview-status")).to_contain_text("Previous image is out of date")
    expect(ui.locator("#document-preview")).to_have_attribute("data-stale", "true")
    pane = ui.locator("#preview-scroll")
    toolbar = ui.locator(".preview-toolbar").bounding_box()
    pane.focus()
    # End, not Control+End: macOS binds only the plain key to the end of a pane.
    pane.press("End")
    ui.wait_for_function("document.querySelector('#preview-scroll').scrollTop > 0")
    assert ui.locator(".preview-toolbar").bounding_box() == toolbar
    scroll_box = pane.bounding_box()
    assert scroll_box["y"] >= toolbar["y"] + toolbar["height"]
    expect(ui.locator("#preview-label")).to_be_in_viewport()
    expect(ui.locator("#preview-status")).to_be_in_viewport()
    expect(ui.locator("#preview-spinner")).to_be_in_viewport()


def test_token_editor_groups_hex_entry_and_live_contrast(ui):
    from playwright.sync_api import expect

    from platefold.contrast import RULES, contrast_ratio
    from platefold.theme import THEMES

    bristol = THEMES["bristol"]
    scored = sum(rule.kind != "decorative" for rule in RULES)
    ui.get_by_text("Customise palette", exact=True).click()
    tokens = ui.locator("#tokens")
    expect(tokens.locator(".eyebrow")).to_have_text(["STRUCTURE", "DIAGRAMS"])
    expect(
        ui.get_by_role("note").filter(has_text="re-renders every cached diagram")
    ).to_be_visible()
    rows = tokens.locator(".token").evaluate_all("els => els.map(e => e.dataset.token)")
    assert rows[:3] == ["page", "ink", "ink_strong"] and rows[-1] == "diagram_note_stroke"
    pill = tokens.locator('[data-token="ink_soft"] .contrast')
    worst = min(
        contrast_ratio(bristol.ink_soft, bristol.page),
        contrast_ratio(bristol.ink_soft, bristol.surface_alt),
    )
    expect(pill).to_have_text(f"AA {worst:.2f}")
    expect(pill).to_have_attribute("data-verdict", "pass")
    expect(tokens.locator('[data-token="rule"] .contrast')).to_have_text("decorative")
    expect(tokens.locator('[data-token="page"] .contrast')).to_have_attribute(
        "data-verdict", "pass"
    )
    expect(ui.locator("#contrast-summary")).to_have_text(
        f"All {scored} scored pairings meet WCAG 2.1 AA."
    )
    expect(ui.get_by_role("button", name="Revert")).to_be_disabled()
    hex_entry = ui.get_by_label("ink soft hex", exact=True)
    hex_entry.fill("#CCCCCC")
    expect(pill).to_have_attribute("data-verdict", "fail")
    expect(pill).to_contain_text("below AA")
    expect(ui.locator("#contrast-summary")).to_contain_text("ink_soft on page")
    expect(ui.locator("#contrast-summary")).to_contain_text("needs 4.5:1")
    assert ui.get_by_label("ink soft colour", exact=True).input_value() == "#cccccc"
    assert (
        ui.locator("#paper").evaluate("el => el.style.getPropertyValue('--ink-soft')") == "#cccccc"
    )
    expect(ui.get_by_role("button", name="Revert")).to_be_enabled()
    hex_entry.fill("nope")
    expect(hex_entry).to_have_attribute("aria-invalid", "true")
    assert (
        ui.locator("#paper").evaluate("el => el.style.getPropertyValue('--ink-soft')") == "#cccccc"
    )
    hex_entry.press("Tab")
    assert hex_entry.input_value() == "#cccccc"
    expect(hex_entry).not_to_have_attribute("aria-invalid", "true")
    ui.get_by_role("button", name="Revert").click()
    expect(pill).to_have_attribute("data-verdict", "pass")
    assert hex_entry.input_value() == bristol.ink_soft
    expect(ui.get_by_role("button", name="Revert")).to_be_disabled()
    expect(ui.locator("#contrast-summary")).to_contain_text("meet WCAG 2.1 AA")


def test_builtin_gating_and_dark_page_advisory(ui):
    from playwright.sync_api import expect

    expect(ui.locator("#theme-kind")).to_have_text("BUILT IN · EDITS NEED A COPY")
    expect(ui.get_by_role("button", name="Overwrite")).to_be_disabled()
    expect(ui.get_by_role("button", name="Delete", exact=True)).to_be_disabled()
    expect(ui.get_by_role("button", name="Save as a copy")).to_be_enabled()
    expect(ui.locator("#mode-advisory")).not_to_be_visible()
    ui.locator("#choose-theme").click()
    expect(ui.get_by_role("button", name="midnight", exact=True)).to_contain_text("DARK · SCREEN")
    expect(ui.get_by_role("button", name="bristol", exact=True)).not_to_contain_text("DARK")
    ui.get_by_role("button", name="midnight", exact=True).click()
    expect(ui.locator("#mode-advisory")).to_be_visible()
    expect(ui.locator("#mode-advisory")).to_contain_text("Choose a light theme for printing")
    expect(ui.get_by_role("button", name="Overwrite")).to_be_disabled()
    ui.locator("#choose-theme").click()
    ui.get_by_role("button", name="bristol", exact=True).click()
    expect(ui.locator("#mode-advisory")).not_to_be_visible()
    ui.get_by_text("Customise palette", exact=True).click()
    ui.get_by_label("page hex", exact=True).fill("#101010")
    expect(ui.locator("#mode-advisory")).to_be_visible()
    expect(ui.get_by_role("button", name="Overwrite")).to_be_disabled()
    expect(ui.get_by_role("button", name="Revert")).to_be_enabled()


USER_THEME_PATH = "/home/reader/.config/platefold/themes/night-reading.json"


def catalog_with_user_theme(ui, accent=None):
    return ui.evaluate(
        """accent => {
        const catalog = structuredClone(window.__catalog);
        const base = catalog.themes.find(t => t.name === 'midnight');
        catalog.themes.push({...base, name:'night-reading', builtin:false, kind:'user',
            path:'%s', file:'night-reading.json',
            description:'Saved from the app', contrast_failures:[],
            colors: accent ? {...base.colors, accent} : base.colors});
        return catalog;
    }"""
        % USER_THEME_PATH,
        accent,
    )


def test_user_theme_overwrite_and_named_delete_confirmation(ui):
    from playwright.sync_api import expect

    ui.evaluate(
        "catalog => window.__emitWorker({schema_version:1,event:'catalog',id:'catalog',catalog})",
        catalog_with_user_theme(ui),
    )
    ui.locator("#choose-theme").click()
    ui.get_by_role("button", name="night-reading", exact=True).click()
    expect(ui.locator("#theme-kind")).to_have_text("YOUR THEME · night-reading.json")
    delete = ui.get_by_role("button", name="Delete", exact=True)
    overwrite = ui.get_by_role("button", name="Overwrite")
    expect(delete).to_be_enabled()
    expect(overwrite).to_be_disabled()
    ui.get_by_text("Customise palette", exact=True).click()
    ui.get_by_label("accent hex", exact=True).fill("#123456")
    expect(overwrite).to_be_enabled()
    overwrite.click()
    request = ui.evaluate(
        "window.__calls.filter(c => c.command === 'send_worker').at(-1).args.message"
    )
    assert request["command"] == "overwrite" and request["name"] == "night-reading"
    assert request["colors"]["accent"] == "#123456"
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'error',id:'overwrite',message:'Disk is read-only'})"
    )
    expect(ui.locator("#theme-error")).to_contain_text("Disk is read-only")
    expect(ui.locator("#theme-error")).to_contain_text("How to fix: Retry the action")
    expect(ui.locator("#results-dialog")).not_to_be_visible()
    ui.evaluate(
        """catalog => window.__emitWorker({schema_version:1,event:'overwritten',id:'overwrite',
            name:'night-reading',path:'%s',catalog})"""
        % USER_THEME_PATH,
        catalog_with_user_theme(ui, "#123456"),
    )
    expect(ui.get_by_role("status")).to_contain_text(
        f"Saved edits to night-reading ({USER_THEME_PATH})"
    )
    expect(overwrite).to_be_disabled()
    expect(ui.locator("#theme-error")).not_to_be_visible()
    assert ui.get_by_label("accent hex", exact=True).input_value() == "#123456"

    delete.click()
    dialog = ui.locator("#delete-dialog")
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("heading")).to_have_text("Delete “night-reading”?")
    expect(dialog).to_contain_text(USER_THEME_PATH)
    expect(dialog).to_contain_text("Documents already exported keep their appearance")
    expect(dialog.get_by_role("button", name="Cancel", exact=True)).to_be_focused()
    ui.keyboard.press("Escape")
    expect(dialog).not_to_be_visible()
    expect(delete).to_be_focused()
    delete.click()
    dialog.get_by_role("button", name="Delete", exact=True).click()
    request = ui.evaluate(
        "window.__calls.filter(c => c.command === 'send_worker').at(-1).args.message"
    )
    assert request == {
        "schema_version": 1,
        "id": "delete",
        "command": "delete",
        "name": "night-reading",
        "ui_locale": "en",
    }
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'error',id:'delete',message:'Permission denied'})"
    )
    expect(dialog).to_be_visible()
    expect(dialog.get_by_role("alert")).to_contain_text("Permission denied")
    expect(dialog.get_by_role("alert")).to_contain_text("How to fix: Retry the action")
    ui.evaluate(
        """window.__emitWorker({schema_version:1,event:'deleted',id:'delete',name:'night-reading',
            path:'%s',catalog:window.__catalog})"""
        % USER_THEME_PATH
    )
    expect(dialog).not_to_be_visible()
    expect(ui.get_by_role("status")).to_contain_text(f"Deleted night-reading ({USER_THEME_PATH})")
    expect(ui.locator("#theme-name")).to_have_text("bristol")
    expect(ui.locator("#choose-theme")).to_be_focused()
    expect(delete).to_be_disabled()
    ui.locator("#choose-theme").click()
    expect(ui.get_by_role("button", name="night-reading", exact=True)).to_have_count(0)


def test_new_from_current_forks_the_selection_into_the_copy_dialog(ui):
    from playwright.sync_api import expect

    ui.locator("#choose-theme").click()
    ui.get_by_role("button", name="New from current").click()
    expect(ui.locator("#theme-dialog")).not_to_be_visible()
    expect(ui.locator("#save-dialog")).to_be_visible()
    expect(ui.get_by_label("Theme name")).to_be_focused()
    ui.get_by_label("Theme name").fill("my-fork")
    ui.get_by_role("button", name="Save copy").click()
    request = ui.evaluate(
        "window.__calls.filter(c => c.command === 'send_worker').at(-1).args.message"
    )
    assert request["command"] == "save_copy" and request["name"] == "my-fork"
    assert request["theme"] == "bristol" and request["colors"]["accent"]


def test_updates_are_explicit_off_by_default_and_render_signed_notes_as_text(ui):
    from playwright.sync_api import expect

    assert not ui.evaluate("window.__calls.some(c => c.command === 'check_updates')")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    expect(dialog.get_by_label("Release channel")).to_have_value("stable")
    check = dialog.get_by_role("button", name="Check for updates")
    expect(check).to_be_disabled()
    dialog.get_by_label("Allow manual update checks").check()
    assert not ui.evaluate("window.__calls.some(c => c.command === 'check_updates')")
    ui.evaluate("""window.__updateRelease = {version: '0.3.0', notes: '<img src=x onerror=alert(1)>',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19, signature: 'fixture'}}""")
    check.click()
    expect(dialog.locator("#updates-notes")).to_have_text("<img src=x onerror=alert(1)>")
    expect(dialog.locator("#updates-notes img")).to_have_count(0)
    expect(dialog.get_by_role("button", name="Download and verify")).to_be_visible()
    assert not ui.evaluate("window.__calls.some(c => c.command === 'download_update')")
    expect(dialog.get_by_label("Release artifact URL")).to_have_value(
        "https://example.invalid/app.exe"
    )
    dialog.get_by_label("Release channel").select_option("prerelease")
    expect(dialog.locator("#updates-release")).not_to_be_visible()
    dialog.get_by_label("Allow manual update checks").uncheck()
    expect(check).to_be_disabled()
    ui.get_by_role("button", name="Close updates").click()
    ui.get_by_role("button", name="Updates", exact=True).click()
    expect(dialog.get_by_label("Allow manual update checks")).not_to_be_checked()


def test_update_results_are_discarded_when_disabled_during_check(ui):
    from playwright.sync_api import expect

    ui.evaluate("window.__updatePending = true")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_label("Allow manual update checks").uncheck()
    ui.evaluate("window.__resolveUpdate({version:'9.9.9',notes:'stale',artifact:{}})")
    expect(dialog.get_by_role("status")).to_have_text("Update checks are disabled.")
    expect(dialog.locator("#updates-release")).not_to_be_visible()


def test_update_checks_defer_when_export_starts(ui):
    from playwright.sync_api import expect

    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.evaluate("window.__updatePending = true")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Close updates").click()
    ui.get_by_role("button", name="Export PDFs").click()
    ui.get_by_role("button", name="Updates", exact=True).click()
    ui.evaluate("window.__resolveUpdate({version:'9.9.9',notes:'stale',artifact:{}})")
    expect(dialog.get_by_role("button", name="Check for updates")).to_be_disabled()
    expect(dialog.get_by_role("status")).to_contain_text("An export started")
    expect(dialog.locator("#updates-release")).not_to_be_visible()
    assert not ui.evaluate("window.__calls.some(c => /download|install|restart/.test(c.command))")


def test_a_deb_installation_shows_the_release_page_and_never_a_download(ui):
    from playwright.sync_api import expect

    ui.evaluate("""Object.assign(window.__updateStatus, {installable: false, distribution: 'deb',
        guidance: 'Download the new .deb from the release page.'})""")
    ui.evaluate("""window.__updateRelease = {version: '1.0.1', notes: 'Fixes.', artifact: null,
        release_page: 'https://github.com/maxfield-allison/platefold/releases/tag/v1.0.1'}""")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    expect(dialog.locator("#updates-guidance")).to_have_text(
        "Download the new .deb from the release page."
    )
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    expect(dialog.get_by_label("Release page")).to_have_value(
        "https://github.com/maxfield-allison/platefold/releases/tag/v1.0.1"
    )
    expect(dialog.get_by_role("status")).to_contain_text("Download it from the release page")
    expect(dialog.get_by_role("button", name="Download and verify")).not_to_be_visible()
    expect(dialog.get_by_label("Release artifact URL")).not_to_be_visible()
    expect(dialog.get_by_label("Expected SHA-256")).not_to_be_visible()
    assert not ui.evaluate("window.__calls.some(c => /download|install/.test(c.command))")


def test_update_download_install_and_restart_each_require_an_explicit_step(ui):
    from playwright.sync_api import expect

    ui.evaluate("""window.__updateRelease = {version: '0.3.0', notes: 'Release notes',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19,
        signature: 'fixture'}}""")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Download and verify").click()
    expect(dialog.get_by_role("status")).to_contain_text("Installation still needs your approval")
    install = dialog.get_by_role("button", name="Install verified update")
    expect(install).to_be_disabled()
    assert not ui.evaluate("window.__calls.some(c => c.command === 'install_update')")

    dialog.get_by_label("I approve replacing this installed copy of PlateFold").check()
    expect(install).to_be_enabled()
    expect(dialog.get_by_label("Restart PlateFold after installation")).not_to_be_checked()
    install.click()
    expect(dialog.get_by_role("status")).to_contain_text("Restart PlateFold when you are ready")
    install_call = ui.evaluate("window.__calls.filter(c => c.command === 'install_update').at(-1)")
    assert install_call["args"] == {"restart": False}
    restart = dialog.get_by_role("button", name="Restart PlateFold now")
    expect(restart).to_be_visible()
    restart.click()
    assert ui.evaluate("window.__calls.some(c => c.command === 'restart_update')")


def test_update_download_blocks_export_and_cancellation_discards_partial_bytes(ui):
    from playwright.sync_api import expect

    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.evaluate("""window.__updateRelease = {version: '0.3.0', notes: 'Release notes',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19,
        signature: 'fixture'}}; window.__downloadPending = true""")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Download and verify").click()
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_disabled()
    dialog.get_by_role("button", name="Cancel update", exact=True).click()
    expect(dialog.get_by_role("status")).to_contain_text("No partial bytes were kept")
    expect(ui.get_by_role("button", name="Export PDFs")).to_be_enabled()
    assert ui.evaluate("window.__calls.some(c => c.command === 'cancel_update')")


def test_simulated_install_failure_keeps_verified_update_available_for_retry(ui):
    from playwright.sync_api import expect

    ui.evaluate("""window.__updateRelease = {version: '0.3.0', notes: 'Release notes',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19,
        signature: 'fixture'}}; window.__installError = 'Synthetic installation failure'""")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Download and verify").click()
    dialog.get_by_label("I approve replacing this installed copy of PlateFold").check()
    install = dialog.get_by_role("button", name="Install verified update")
    install.click()
    expect(dialog.get_by_role("status")).to_contain_text("Synthetic installation failure")
    expect(install).to_be_enabled()
    ui.evaluate("window.__installError = null")
    install.click()
    expect(dialog.get_by_role("status")).to_contain_text("Update installed")


def test_an_installed_update_shows_a_folder_it_could_not_delete(ui):
    from playwright.sync_api import expect

    ui.evaluate("""window.__updateRelease = {version: '0.3.0', notes: 'Release notes',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19,
        signature: 'fixture'}};
        window.__installSuccessStatus = {warning: 'PlateFold could not delete the unused update folder at /apps/.PlateFold.AppImage.update-a1B2c3. You can delete that folder.'}""")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Download and verify").click()
    dialog.get_by_label("I approve replacing this installed copy of PlateFold").check()
    dialog.get_by_role("button", name="Install verified update").click()

    # The installation still reads as done, and the leftover folder is named after it.
    status = dialog.get_by_role("status")
    expect(status).to_contain_text("Restart PlateFold when you are ready")
    expect(status).to_contain_text("/apps/.PlateFold.AppImage.update-a1B2c3")
    expect(dialog.get_by_role("button", name="Restart PlateFold now")).to_be_visible()


def test_a_requested_restart_waits_while_a_leftover_folder_is_shown(ui):
    from playwright.sync_api import expect

    ui.evaluate("""window.__updateRelease = {version: '0.3.0', notes: 'Release notes',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19,
        signature: 'fixture'}};
        window.__installSuccessStatus = {warning: 'PlateFold could not delete the unused update folder at /apps/.PlateFold.AppImage.update-a1B2c3. You can delete that folder.'}""")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Download and verify").click()
    dialog.get_by_label("I approve replacing this installed copy of PlateFold").check()
    dialog.get_by_label("Restart PlateFold after installation").check()
    dialog.get_by_role("button", name="Install verified update").click()

    # Native code deferred the restart, so the dialog must not say it is restarting.
    status = dialog.get_by_role("status")
    expect(status).to_contain_text("Restart PlateFold when you are ready")
    expect(status).to_contain_text("/apps/.PlateFold.AppImage.update-a1B2c3")
    expect(status).not_to_contain_text("restarting PlateFold")
    expect(dialog.get_by_role("button", name="Restart PlateFold now")).to_be_visible()
    install_call = ui.evaluate("window.__calls.filter(c => c.command === 'install_update').at(-1)")
    assert install_call["args"] == {"restart": True}


def _install_verified_update(ui, *, restart):
    """Opens Updates, downloads the fixture release and installs it; returns the dialog."""
    ui.evaluate("""window.__updateRelease = {version: '0.3.0', notes: 'Release notes',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19,
        signature: 'fixture'}}""")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Download and verify").click()
    dialog.get_by_label("I approve replacing this installed copy of PlateFold").check()
    if restart:
        dialog.get_by_label("Restart PlateFold after installation").check()
    dialog.get_by_role("button", name="Install verified update").click()
    return dialog


def test_a_requested_restart_is_announced_only_when_native_code_restarts(ui):
    from playwright.sync_api import expect

    # A close during installation deferred the restart: no warning, restart checked.
    dialog = _install_verified_update(ui, restart=True)

    status = dialog.get_by_role("status")
    expect(status).to_contain_text("Restart PlateFold when you are ready")
    expect(status).not_to_contain_text("restarting PlateFold")


def test_a_native_restart_is_announced(ui):
    from playwright.sync_api import expect

    ui.evaluate("window.__installRestarting = true")
    dialog = _install_verified_update(ui, restart=True)

    expect(dialog.get_by_role("status")).to_contain_text("Update installed; restarting PlateFold.")


def test_changing_update_settings_keeps_an_installed_update_ready_to_restart(ui):
    from playwright.sync_api import expect

    ui.evaluate(
        """window.__installSuccessStatus = {warning: 'PlateFold could not delete the unused update folder at /apps/.PlateFold.AppImage.update-a1B2c3. You can delete that folder.'}"""
    )
    dialog = _install_verified_update(ui, restart=False)
    restart = dialog.get_by_role("button", name="Restart PlateFold now")
    expect(restart).to_be_visible()

    dialog.get_by_label("Release channel").select_option("prerelease")

    expect(dialog.get_by_role("status")).to_contain_text("/apps/.PlateFold.AppImage.update-a1B2c3")
    expect(restart).to_be_visible()
    expect(restart).to_be_enabled()
    restart.click()
    assert ui.evaluate("window.__calls.some(c => c.command === 'restart_update')")


def test_changing_settings_after_a_clean_install_still_asks_for_a_restart(ui):
    from playwright.sync_api import expect

    dialog = _install_verified_update(ui, restart=False)

    dialog.get_by_label("Release channel").select_option("prerelease")

    # Check stays disabled, so the dialog must not offer one.
    status = dialog.get_by_role("status")
    expect(status).to_have_text("Update installed. Restart PlateFold when you are ready.")
    expect(dialog.get_by_role("button", name="Check for updates")).to_be_disabled()
    expect(dialog.get_by_role("button", name="Restart PlateFold now")).to_be_enabled()


def test_an_installed_update_waiting_for_restart_cannot_be_checked_again(ui):
    from playwright.sync_api import expect

    dialog = _install_verified_update(ui, restart=False)

    # A check would reset the native state the Restart button relies on.
    expect(dialog.get_by_role("button", name="Check for updates")).to_be_disabled()
    restart = dialog.get_by_role("button", name="Restart PlateFold now")
    expect(restart).to_be_enabled()
    restart.click()
    assert ui.evaluate("window.__calls.some(c => c.command === 'restart_update')")
    # Only the check that found the update ran.
    assert ui.evaluate("window.__calls.filter(c => c.command === 'check_updates').length") == 1


def test_an_install_failure_that_cannot_be_retried_shows_the_recovery_warning(ui):
    from playwright.sync_api import expect

    ui.evaluate("""window.__updateRelease = {version: '0.3.0', notes: 'Release notes',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19,
        signature: 'fixture'}};
        window.__installError = 'The update could not be installed. The previous copy of PlateFold is kept at /backup/PlateFold.AppImage';
        window.__installFailureStatus = {phase: 'idle', pending_version: null,
            warning: 'Installation of 0.3.0 failed, and PlateFold could not put the previous copy back. It is kept at /backup/PlateFold.AppImage. Move it back to /apps/PlateFold.AppImage to use PlateFold again.'}""")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Download and verify").click()
    dialog.get_by_label("I approve replacing this installed copy of PlateFold").check()
    install = dialog.get_by_role("button", name="Install verified update")
    install.click()

    expect(dialog.get_by_role("status")).to_contain_text(
        "Move it back to /apps/PlateFold.AppImage to use PlateFold again"
    )
    expect(install).not_to_be_visible()
    assert ui.evaluate("window.__calls.filter(c => c.command === 'install_update').length") == 1
    # Reopening shows the native state, not the status cached before the failure.
    dialog.get_by_role("button", name="Close updates").click()
    ui.get_by_role("button", name="Updates", exact=True).click()
    expect(dialog.get_by_role("status")).to_contain_text("could not put the previous copy back")


def test_a_close_held_by_an_installation_is_announced_with_or_without_the_dialog(ui):
    from playwright.sync_api import expect

    pending = "Close requested. PlateFold is waiting for the update. After 60 seconds, it will close unless replacement or installer launch is still running."
    ui.evaluate("""window.__updateRelease = {version: '0.3.0', notes: 'Release notes',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19,
        signature: 'fixture'}}; window.__installPending = true""")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Download and verify").click()
    dialog.get_by_label("I approve replacing this installed copy of PlateFold").check()
    dialog.get_by_role("button", name="Install verified update").click()

    ui.evaluate("window.__emitNative('update-close-pending')")
    expect(dialog.get_by_role("status")).to_have_text(pending)
    expect(ui.locator("#status")).to_have_text(pending)

    # With the dialog closed, the main window's status still says why it stays open.
    dialog.get_by_role("button", name="Close updates").click()
    ui.evaluate("document.querySelector('#status').textContent = ''")
    ui.evaluate("window.__emitNative('update-close-pending')")
    expect(ui.locator("#status")).to_have_text(pending)
    ui.evaluate(
        "window.__updateStatus.phase = 'installed'; window.__resolveInstall({...window.__updateStatus, restarting: false})"
    )


def test_update_failures_keep_preferences_and_allow_retry(ui):
    from playwright.sync_api import expect

    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    ui.evaluate("window.__updateSaveError = true")
    dialog.get_by_label("Allow manual update checks").click()
    expect(dialog.get_by_label("Allow manual update checks")).not_to_be_checked()
    expect(dialog.get_by_role("status")).to_contain_text("Could not save")
    ui.evaluate(
        "window.__updateSaveError = false; window.__updateError = 'Update metadata signature verification failed'"
    )
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    expect(dialog.get_by_role("status")).to_contain_text("signature verification failed")
    expect(dialog.get_by_role("button", name="Check for updates")).to_be_enabled()
    expect(dialog.locator("#updates-release")).not_to_be_visible()
    ui.evaluate("window.__updateError = null")
    dialog.get_by_role("button", name="Check for updates").click()
    expect(dialog.get_by_role("status")).to_contain_text("current release")


def test_unconfigured_preview_explains_closed_feed_without_checking(ui):
    from playwright.sync_api import expect

    ui.evaluate("window.__updateStatus.configured = false")
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    expect(dialog.get_by_role("status")).to_contain_text("not configured")
    expect(dialog.get_by_role("button", name="Check for updates")).to_be_disabled()
    assert not ui.evaluate("window.__calls.some(c => c.command === 'check_updates')")


def test_installed_update_cannot_restart_during_export(ui):
    """A separately deferred restart must remain blocked while export is active."""
    from playwright.sync_api import expect

    ui.evaluate(
        """window.__updateRelease = {version: '0.3.0', notes: 'Synthetic release',
        artifact: {url: 'https://example.invalid/app.exe', sha256: 'a'.repeat(64), size: 19,
        signature: 'fixture'}}"""
    )
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.get_by_role("button", name="Updates", exact=True).click()
    dialog = ui.get_by_role("dialog", name="Updates", exact=True)
    dialog.get_by_label("Allow manual update checks").check()
    dialog.get_by_role("button", name="Check for updates").click()
    dialog.get_by_role("button", name="Download and verify").click()
    dialog.get_by_label("I approve replacing this installed copy of PlateFold").check()
    dialog.get_by_role("button", name="Install verified update").click()

    restart = dialog.get_by_role("button", name="Restart PlateFold now")
    expect(restart).to_be_visible()
    dialog.get_by_role("button", name="Close updates").click()
    ui.get_by_role("button", name="Export PDFs").click()
    ui.get_by_role("button", name="Updates", exact=True).click()

    expect(restart).to_be_disabled()
    assert not ui.evaluate("window.__calls.some(c => c.command === 'restart_update')")
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'result',id:'export',result:{status:'cancelled',total_documents:1,report:{documents:[],engine:'fixture'}}})"
    )
    expect(restart).to_be_enabled()
    restart.click()
    assert ui.evaluate("window.__calls.filter(c => c.command === 'restart_update').length") == 1


def test_accessibility_language_and_export_focus_recovery(ui):
    from playwright.sync_api import expect

    ui.get_by_role("button", name="Choose file", exact=True).click()
    expect(ui.get_by_role("button", name="Choose file", exact=True)).to_be_focused()
    ui.get_by_role("button", name="Choose output folder").click()
    expect(ui.get_by_role("button", name="Choose output folder")).to_be_focused()
    ui.get_by_label("Document language", exact=True).fill("fr-CA")
    ui.get_by_role("button", name="Export PDFs").click()
    cancel = ui.get_by_role("button", name="Cancel export")
    expect(cancel).to_be_focused()
    request = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'export').at(-1).args.message"
    )
    assert request["settings"]["document_language"] == "fr-CA"
    cancel.click()
    expect(cancel).to_be_focused()
    expect(cancel).to_have_attribute("aria-disabled", "true")
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'result',id:'export',result:{status:'cancelled',total_documents:1,report:{documents:[],engine:'fixture'}}})"
    )
    expect(ui.get_by_role("button", name="View results")).to_be_focused()
    ui.keyboard.press("Enter")
    expect(ui.get_by_role("dialog", name="Export results")).to_be_visible()
    ui.keyboard.press("Escape")
    expect(ui.get_by_role("button", name="View results")).to_be_focused()


def test_accessibility_progress_does_not_replace_every_announcement(ui):
    from playwright.sync_api import expect

    ui.clock.install()
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.get_by_role("button", name="Export PDFs").click()
    ui.clock.run_for(1100)
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'progress',file:'first.md',done:1,total:100})"
    )
    expect(ui.locator("#status")).to_contain_text("first.md")
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'progress',file:'second.md',done:2,total:100})"
    )
    expect(ui.locator("#status")).to_contain_text("first.md")
    expect(ui.get_by_role("progressbar")).to_have_attribute("value", "2")
    ui.clock.run_for(1100)
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'progress',file:'third.md',done:3,total:100})"
    )
    expect(ui.locator("#status")).to_contain_text("third.md")


def test_preview_warnings_are_compact_keyboard_disclosures(ui):
    from playwright.sync_api import expect

    ui.clock.install()
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.clock.run_for(600)
    emit_preview = """() => {
        const id = window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message.id;
        window.__emitWorker({schema_version:1,event:'preview',id,preview:{
            file:'library.csv',pages:null,width:1,height:1,text:'Table',image:'',
            warnings:['DOCUMENT_LANGUAGE: Technical language warning', 'Another warning. '.repeat(40)]
        }});
    }"""
    ui.evaluate(emit_preview)
    status = ui.locator("#preview-status")
    warnings = ui.locator("#preview-warnings")
    toggle = ui.get_by_role("button", name="Show warnings and how to fix them")
    expect(status).to_have_text("Preview ready.")
    expect(warnings).not_to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    assert status.evaluate("el => !['auto','scroll'].includes(getComputedStyle(el).overflowY)")
    toggle.focus()
    ui.keyboard.press("Enter")
    expect(warnings).to_be_visible()
    expect(warnings).to_contain_text("Document language")
    expect(warnings).to_contain_text("en")
    expect(warnings).not_to_contain_text("und (undetermined)")
    expect(toggle).to_have_count(0)
    expect(ui.get_by_role("button", name="Hide warnings")).to_have_attribute(
        "aria-expanded", "true"
    )
    ui.get_by_role("button", name="Refresh preview").click()
    ui.clock.run_for(600)
    ui.evaluate(emit_preview)
    expect(warnings).not_to_be_visible()
    expect(toggle).to_have_attribute("aria-expanded", "false")


def test_result_warnings_are_collapsed_and_step_labels_are_unnumbered(ui):
    from playwright.sync_api import expect

    labels = ui.locator(".eyebrow").all_text_contents()
    assert {"SOURCE", "DESTINATION", "EXPORT"}.issubset(set(labels))
    assert not any(re.match(r"\s*\d+\s*/", label) for label in labels)
    ui.evaluate("""window.__emitWorker({schema_version:1,event:'result',id:'export',result:{
        status:'completed_with_warnings',total_documents:1,report:{engine:'fixture',documents:[{
            file:'library.csv',pages:92,figures:0,missing_images:[],diagram_errors:[],
            warnings:['DOCUMENT_LANGUAGE: Technical language warning']
        }]}}})""")
    ui.get_by_role("button", name="View results").click()
    disclosure = ui.locator("#results .warning-disclosure")
    expect(disclosure.locator(".warning")).not_to_be_visible()
    disclosure.locator("summary").focus()
    ui.keyboard.press("Enter")
    expect(disclosure.locator(".warning")).to_be_visible()
    expect(disclosure).to_contain_text("Document language")
    expect(disclosure).not_to_contain_text("DOCUMENT_LANGUAGE:")
    ui.keyboard.press("Enter")
    expect(disclosure.locator(".warning")).not_to_be_visible()


def test_export_announces_first_progress_immediately_and_active_stage(ui):
    from playwright.sync_api import expect

    ui.clock.install()
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.get_by_role("button", name="Export PDFs").click()
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'progress',file:'library.csv',done:0,total:1})"
    )
    # No one-second wait: the first event must replace the startup message.
    expect(ui.locator("#status")).to_contain_text("library.csv")
    assert ui.get_by_role("progressbar").get_attribute("value") is None
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'activity',id:'export',phase:'print pdf'})"
    )
    expect(ui.locator("#status")).to_contain_text("Rendering PDF pages")
    expect(ui.locator("#status")).to_contain_text("library.csv")
    stage = ui.locator("#status").inner_text()
    before = ui.locator("#export-timing").inner_text()
    ui.clock.run_for(2100)
    assert ui.locator("#export-timing").inner_text() != before
    expect(ui.locator("#export-timing")).to_contain_text("Elapsed")
    expect(ui.locator("#status")).to_have_text(stage)
    assert ui.locator("#export-timing").get_attribute("aria-live") not in {"polite", "assertive"}
    ui.get_by_role("button", name="Cancel export").click()
    cancelling = ui.locator("#status").inner_text()
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'activity',id:'export',phase:'validate pdf'})"
    )
    expect(ui.locator("#status")).to_have_text(cancelling)


@pytest.mark.parametrize("ending", ["result", "error", "disconnect"])
def test_export_timer_stops_when_work_ends(ui, ending):
    from playwright.sync_api import expect

    ui.clock.install()
    ui.clock.run_for(100)
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.get_by_role("button", name="Export PDFs").click()
    ui.clock.run_for(1100)
    if ending == "result":
        ui.evaluate("""window.__emitWorker({schema_version:1,event:'result',id:'export',result:{
            status:'completed',total_documents:1,elapsed_seconds:12,
            report:{documents:[],engine:'fixture'}}})""")
        expect(ui.locator("#export-timing")).to_have_text("Elapsed 0:12")
    elif ending == "error":
        ui.evaluate(
            "window.__emitWorker({schema_version:1,event:'error',id:'export',message:'Fixture failure'})"
        )
        expect(ui.locator("#export-timing")).not_to_be_visible()
    else:
        ui.evaluate("window.__exitWorker('Worker stopped. Reopen the app to reconnect.')")
        expect(ui.locator("#export-timing")).not_to_be_visible()
    stopped = ui.locator("#export-timing").inner_text()
    ui.clock.run_for(3100)
    expect(ui.locator("#export-timing")).to_have_text(stopped)
    expect(ui.get_by_role("button", name="Cancel export")).not_to_be_visible()


def test_export_estimate_reuses_same_settings_and_resets_for_changed_settings(ui):
    from playwright.sync_api import expect

    ui.clock.install()
    ui.clock.run_for(100)
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.get_by_role("button", name="Export PDFs").click()
    expect(ui.locator("#export-timing")).to_contain_text("Large tables can take a few minutes")
    complete = """window.__emitWorker({schema_version:1,event:'result',id:'export',result:{
        status:'completed',total_documents:1,elapsed_seconds:20,
        report:{documents:[],engine:'fixture'}}})"""
    ui.evaluate(complete)
    ui.get_by_role("button", name="Export PDFs").click()
    expect(ui.locator("#export-timing")).to_contain_text("About 0:20 remaining")
    ui.clock.run_for(5100)
    expect(ui.locator("#export-timing")).to_contain_text("About 0:15 remaining")
    ui.clock.run_for(16000)
    expect(ui.locator("#export-timing")).to_contain_text("Taking longer")
    ui.evaluate(complete)
    ui.get_by_label("Font set", exact=True).select_option("georgia")
    ui.get_by_role("button", name="Export PDFs").click()
    expect(ui.locator("#export-timing")).to_contain_text("Large tables can take a few minutes")


def test_installed_font_selection_source_override_and_missing_refresh(ui):
    from playwright.sync_api import expect

    ui.clock.install()
    ui.evaluate("""() => {
        window.__catalog.font_sets.push({name:'system:Test Font (Display)',label:'Test Font (Display)',body:'Arial',head:'Arial',mono:'monospace'});
        window.__emitWorker({schema_version:1,event:'catalog',catalog:window.__catalog});
    }""")
    expect(ui.locator('#font optgroup[label="Installed on this computer"]')).to_have_count(1)
    expect(ui.locator('#font option[value="system:Test Font (Display)"]')).to_have_text(
        "Test Font (Display)"
    )
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.locator("#choose-theme").click()
    ui.get_by_role("button", name="source", exact=True).click()
    expect(ui.locator("#font")).to_be_enabled()
    expect(ui.locator("#font")).to_have_value("__source__")
    ui.clock.run_for(600)
    automatic = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message"
    )
    assert "font_set" not in automatic["settings"]
    ui.locator("#font").select_option("system:Test Font (Display)")
    ui.clock.run_for(600)
    override = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message"
    )
    assert override["settings"]["theme"] == "source"
    assert override["settings"]["font_set"] == "system:Test Font (Display)"
    ui.evaluate(
        "window.__catalog.font_sets=window.__catalog.font_sets.filter(f => f.name !== 'system:Test Font (Display)')"
    )
    ui.get_by_role("button", name="Refresh fonts", exact=True).click()
    expect(ui.locator("#font")).to_have_value("system:Test Font (Display)")
    expect(ui.locator("#font option:checked")).to_contain_text("unavailable; fallback")


def test_renderer_choice_reaches_preview_and_export(ui):
    ui.clock.install()
    ui.get_by_role("button", name="Choose file", exact=True).click()
    ui.get_by_role("button", name="Choose output folder").click()
    ui.locator("#renderer-options summary").click()
    ui.get_by_label("PDF renderer", exact=True).select_option("system")
    ui.clock.run_for(600)
    preview = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'preview').at(-1).args.message"
    )
    assert preview["browser"] == "system"
    ui.get_by_role("button", name="Export PDFs").click()
    request = ui.evaluate(
        "window.__calls.filter(c => c.args?.message?.command === 'export').at(-1).args.message"
    )
    assert request["browser"] == "system"


def test_native_downloads_default_and_explicit_output_survive_source_changes(ui):
    from playwright.sync_api import expect

    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'ready',output_path:'D:/Téléchargements'})"
    )
    expect(ui.locator("#output-path")).to_have_text("D:/Téléchargements")
    expect(ui.locator("#output-entry")).to_have_value("D:/Téléchargements")
    ui.locator("#choose-file").click()
    expect(ui.locator("#export")).to_be_enabled()
    expect(ui.locator("#output-path")).to_have_text("D:/Téléchargements")
    ui.locator("#choose-output").click()
    ui.locator("#choose-folder").click()
    ui.evaluate(
        "window.__emitWorker({schema_version:1,event:'ready',output_path:'D:/Téléchargements'})"
    )
    expect(ui.locator("#output-path")).to_have_text("/selected/output")
    expect(ui.locator("#output-entry")).to_have_value("/selected/output")


def test_native_unavailable_downloads_keeps_folder_selection_available(ui):
    from playwright.sync_api import expect

    message = "Your Downloads folder is unavailable. Choose an output folder before exporting."
    ui.evaluate(
        "message => window.__emitWorker({schema_version:1,event:'ready',output_error:message})",
        message,
    )
    expect(ui.locator("#error")).to_contain_text(message)
    ui.locator("#dismiss-results").click()
    ui.locator("#choose-file").click()
    expect(ui.locator("#export")).to_be_disabled()
    ui.locator("#choose-output").click()
    expect(ui.locator("#export")).to_be_enabled()
