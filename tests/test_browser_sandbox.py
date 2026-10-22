"""A denied Chromium sandbox is reported clearly and never worked around."""

import json
import os
import sys

import pytest

from platefold import browser, cdp, cli
from platefold.i18n import translator
from platefold.worker import DesktopWorker, recovery_id

CHROMIUM_REFUSAL = (
    "[0925/184329.787148:FATAL:content/browser/zygote_host/zygote_host_impl_linux.cc:128] "
    "No usable sandbox! If you are running on Ubuntu 23.10+ or another Linux distro that has "
    "disabled unprivileged user namespaces with AppArmor, see https://example.invalid. If you "
    "want to live dangerously and need an immediate workaround, you can try using --no-sandbox."
)


def fake_browser(directory, stderr, code):
    """A browser executable that writes ``stderr`` and exits with ``code``.

    The body is Python, so the same fake works everywhere; Windows starts it
    through a .cmd launcher because it ignores #! lines.
    """
    script = directory / "fake-browser.py"
    script.write_text(
        f"import sys\nsys.stderr.write({stderr + chr(10)!r})\nsys.exit({code})\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = directory / "chrome.cmd"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        launcher = directory / "chrome-headless-shell"
        launcher.write_text(
            f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n", encoding="utf-8"
        )
        launcher.chmod(0o755)
    return launcher


@pytest.fixture
def refusing_browser(tmp_path):
    """An executable that fails the way Chromium does when AppArmor denies its sandbox."""
    return fake_browser(tmp_path, CHROMIUM_REFUSAL, 133)


def test_sandbox_refusal_is_named_without_relaying_no_sandbox_advice(refusing_browser):
    with pytest.raises(cdp.SandboxUnavailableError) as raised:
        cdp.Browser(str(refusing_browser))
    error = raised.value
    assert error.code == "browser_sandbox_unavailable"
    assert "--no-sandbox" not in str(error)
    assert "LINUX-INSTALL.md" in str(error)
    assert "No usable sandbox!" in error.details
    assert isinstance(error, cdp.CDPError)


def test_other_startup_failures_keep_the_generic_error(tmp_path):
    script = fake_browser(tmp_path, "something else", 1)
    with pytest.raises(cdp.CDPError) as raised:
        cdp.Browser(str(script))
    assert not isinstance(raised.value, cdp.SandboxUnavailableError)
    assert "something else" in str(raised.value)


class Installed:
    def __init__(self):
        self.system_browser_failures = ["edge: not qualified"]


def test_auto_falls_back_to_a_qualified_installed_browser(monkeypatch, refusing_browser):
    monkeypatch.setattr(browser, "find_bundled", lambda: str(refusing_browser))
    installed = Installed()
    monkeypatch.setattr(
        browser, "_launch_system", lambda choice, _check=None: (installed, "qualified chrome (1)")
    )
    selected, description = browser.launch("auto")
    assert selected is installed
    assert description == "qualified chrome (1) (bundled Chromium's sandbox unavailable)"
    assert selected.system_browser_failures[0].startswith("bundled:")
    assert "edge: not qualified" in selected.system_browser_failures


def test_auto_without_an_installed_browser_reports_the_sandbox(monkeypatch, refusing_browser):
    monkeypatch.setattr(browser, "find_bundled", lambda: str(refusing_browser))

    def none_installed(choice, _check=None):
        raise RuntimeError(browser.NO_BROWSER)

    monkeypatch.setattr(browser, "_launch_system", none_installed)
    with pytest.raises(cdp.SandboxUnavailableError):
        browser.launch("auto")


def test_explicit_bundled_choice_does_not_switch_engines(monkeypatch, refusing_browser):
    monkeypatch.setattr(browser, "find_bundled", lambda: str(refusing_browser))
    monkeypatch.setattr(browser, "_launch_system", lambda *_: pytest.fail("engine switched"))
    with pytest.raises(cdp.SandboxUnavailableError):
        browser.launch("bundled")


def test_desktop_and_cli_show_the_supported_routes(monkeypatch, capsys):
    error = cdp.SandboxUnavailableError(CHROMIUM_REFUSAL)
    assert recovery_id(error) == "help.browser_sandbox"
    payload = DesktopWorker.error_payload(error, {"ui_locale": "en"})
    assert payload["code"] == "browser_sandbox_unavailable"
    assert payload["message"] == translator("en").text("error.browser_sandbox_unavailable")
    assert "--no-sandbox" not in payload["message"] + payload["help"]
    assert "AppArmor" in payload["message"]

    def refuse(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(cli, "_run", refuse)
    source = os.path.abspath(__file__)
    assert cli.main([source, "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["error"]["code"] == "browser_sandbox_unavailable"
    assert "LINUX-INSTALL.md" in result["error"]["message"]


def test_catalog_message_matches_the_exception_text():
    assert translator("en").text("error.browser_sandbox_unavailable") == str(
        cdp.SandboxUnavailableError()
    )
