"""The container suite's browser interface check, without Docker or a browser."""

import ast
import json
from pathlib import Path

import pytest

from tools import container_release, container_ui_check
from tools import test_container as suite

ROOT = Path(__file__).resolve().parents[1]
SESSION = "page-session"


def event(method, session=SESSION, **params):
    return {"method": method, "sessionId": session, "params": params}


def test_a_catalog_that_never_loaded_is_reported_as_an_uncaught_exception():
    thrown = event(
        "Runtime.exceptionThrown",
        exceptionDetails={
            "text": "Uncaught",
            "exception": {
                "description": "Error: The English interface catalog is unavailable\n"
                "    at http://127.0.0.1:8787/assets/index.js:1:1"
            },
        },
    )
    assert container_ui_check.page_problems([thrown], SESSION) == [
        "uncaught exception: Error: The English interface catalog is unavailable"
    ]


def test_console_errors_and_browser_logged_errors_fail_the_check():
    events = [
        event("Runtime.consoleAPICalled", type="error", args=[{"value": "boom"}]),
        event("Runtime.consoleAPICalled", type="assert", args=[{"description": "Error: x"}]),
        event(
            "Log.entryAdded",
            entry={
                "level": "error",
                "source": "network",
                "text": "Failed to load resource: 404",
                "url": "http://127.0.0.1:8787/assets/app.css",
            },
        ),
    ]
    assert container_ui_check.page_problems(events, SESSION) == [
        "console.error: boom",
        "console.assert: Error: x",
        "console error [network]: Failed to load resource: 404 "
        "(http://127.0.0.1:8787/assets/app.css)",
    ]


def test_ordinary_logging_and_other_targets_do_not_fail_the_check():
    events = [
        event("Runtime.consoleAPICalled", type="log", args=[{"value": "hello"}]),
        event("Runtime.consoleAPICalled", type="warning", args=[{"value": "careful"}]),
        event("Log.entryAdded", entry={"level": "warning", "source": "other", "text": "w"}),
        event("Runtime.executionContextCreated", context={}),
        event("Runtime.exceptionThrown", session="another-page", exceptionDetails={}),
        {"method": "Target.targetCreated", "params": {}},
    ]
    assert container_ui_check.page_problems(events, SESSION) == []


def test_the_expected_text_is_the_shipped_english_catalog():
    catalog = json.loads((ROOT / "platefold/locales/en.json").read_text("utf-8"))["messages"]
    expected = container_ui_check.expected_text()
    assert expected == {
        selector: catalog[key] for selector, key in container_ui_check.EXPECTED.items()
    }
    assert all(expected.values())


def test_a_blank_page_fails_the_text_check():
    expected = container_ui_check.expected_text()
    shown = dict.fromkeys(expected)
    assert len(container_ui_check.missing_text(shown, expected)) == len(expected)
    assert container_ui_check.missing_text(expected, expected) == []


def test_the_expected_elements_render_catalog_text():
    # The check reads these ids; the web controls must still create them from
    # the catalog messages it expects.
    source = (ROOT / "desktop/src/web-controls.ts").read_text("utf-8")
    for selector, key in container_ui_check.EXPECTED.items():
        element = selector.removeprefix("#")
        assert f'id="{element}"' in source, element
        start = source.index(f'id="{element}"')
        assert f't("{key}")' in source[start : source.index("</", start)], key


def test_the_suite_runs_the_check_inside_the_server_container(monkeypatch, tmp_path):
    calls = []

    def fake_docker(*args, input=None):
        calls.append((args, input))
        return '{"lang": "en"}'

    monkeypatch.setattr(suite, "docker", fake_docker)
    suite.browser_interface("platefold-test-server", tmp_path)

    [(args, script)] = calls
    assert args == ("exec", "-i", "platefold-test-server", "python", "-", suite.SERVER_ORIGIN)
    assert script == (ROOT / "tools/container_ui_check.py").read_text()
    assert json.loads((tmp_path / "browser-interface.json").read_text()) == {"lang": "en"}


def test_a_failed_check_fails_the_suite(monkeypatch, tmp_path):
    def failing_docker(*args, input=None):
        raise RuntimeError("docker exec failed: The browser interface did not start")

    monkeypatch.setattr(suite, "docker", failing_docker)
    with pytest.raises(RuntimeError, match="did not start"):
        suite.browser_interface("platefold-test-server", tmp_path)
    assert not (tmp_path / "browser-interface.json").exists()


def test_the_suite_receipt_passes_exactly_the_release_runtime_checks():
    tree = ast.parse((ROOT / "tools/test_container.py").read_text())
    [receipt] = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "receipt" for t in node.targets)
    ]
    passed = {
        key.value
        for key, value in zip(receipt.keys, receipt.values, strict=True)
        if isinstance(value, ast.Constant) and value.value == "passed"
    }
    assert passed == set(container_release.RUNTIME_CHECKS)
