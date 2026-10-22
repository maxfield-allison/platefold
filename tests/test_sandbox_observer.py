"""The renderer sandbox observer against Chromium's start-up windows.

A renderer forked from Chromium's zygote shows the zygote's command line until
it sets its own, and a renderer applies its seccomp filter a moment after it
starts. The observer must neither file such a renderer away as "not a renderer"
nor judge it before it has settled, and it must measure age on a clock that
agrees with the kernel's process start times.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import psutil
import pytest

from platefold.server_sandbox import SETTLE_SECONDS, SandboxObserver, boot_clock, started

ZYGOTE = ["/opt/chromium/chrome", "--type=zygote", "--no-zygote-sandbox"]
RENDERER = ["/opt/chromium/chrome", "--type=renderer", "--renderer-client-id=5"]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="observes the Linux sandbox")


@contextmanager
def sleeper():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        yield process.pid
    finally:
        process.kill()
        process.wait()


class Scripted:
    """A real, young process whose command line the test controls."""

    def __init__(self, pid: int, command: list[str]):
        self.pid = pid
        self.command = command

    def cmdline(self) -> list[str]:
        return self.command


def test_start_times_are_on_the_boot_clock():
    before = boot_clock()
    with sleeper() as pid:
        after = boot_clock()
        # One clock tick (normally 10 ms) of resolution either side.
        assert before - 0.02 <= started(pid) <= after + 0.02


def test_a_young_process_showing_the_zygote_command_line_is_looked_at_again():
    with sleeper() as pid:
        observer = SandboxObserver()
        forked = Scripted(pid, list(ZYGOTE))
        observer.observe([forked], now=started(pid) + 0.05)
        assert pid not in observer.others
        # The renderer sets its own command line; the next poll sees it.
        forked.command = list(RENDERER)
        observer.observe([forked], now=started(pid) + 0.15)
        assert observer.renderers == {pid: "unknown"}  # Young and not yet filtered.
        observer.observe([forked], now=started(pid) + SETTLE_SECONDS + 0.1)
        assert observer.state == "disabled"  # A plain sleeper has no sandbox.


@pytest.mark.parametrize("command", [[], ZYGOTE])
def test_a_settled_non_renderer_is_filed_away(command):
    with sleeper() as pid:
        observer = SandboxObserver()
        observer.observe([Scripted(pid, command)], now=started(pid) + SETTLE_SECONDS)
        assert pid in observer.others and observer.state == "unknown"


@pytest.mark.parametrize("flag", ["--no-sandbox", "--disable-seccomp-filter-sandbox"])
def test_a_sandbox_disabling_flag_is_evidence_at_any_age(flag):
    with sleeper() as pid:
        observer = SandboxObserver()
        observer.observe([Scripted(pid, [*RENDERER, flag])], now=started(pid))
        assert observer.state == "disabled"


# Real Chromium.


def chromium() -> str:
    from platefold.browser import find_bundled

    executable = find_bundled()
    if not executable:
        pytest.skip("bundled Chromium is unavailable")
    return executable


@contextmanager
def browser(executable: str, flags: list[str] | None = None):
    from platefold.cdp import Browser, SandboxUnavailableError

    try:
        instance = Browser(executable, extra_flags=flags)
    except SandboxUnavailableError:
        pytest.skip("Chromium's sandbox is unavailable on this host")
    try:
        yield instance
    finally:
        instance.close()


def open_pages(instance, count: int) -> list:
    pages = []
    for index in range(count):
        page = instance.new_page(viewport={"width": 800, "height": 600})
        page.set_content(f"<h1>Page {index}</h1>" + "<p>Text.</p>" * 200)
        pages.append(page)
    return pages


def live_renderers() -> list[psutil.Process]:
    found = []
    for child in psutil.Process().children(recursive=True):
        try:
            if "--type=renderer" in child.cmdline():
                found.append(child)
        except psutil.Error:
            pass
    return found


@contextmanager
def guardian_polls(observer: SandboxObserver):
    """Observe this process's tree at the export guardian's 0.1 s cadence."""
    stop = threading.Event()

    def run():
        while not stop.is_set():
            observer.observe(psutil.Process().children(recursive=True))
            time.sleep(0.1)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join()


def settle(observer: SandboxObserver, renderers: list[psutil.Process]) -> None:
    deadline = time.monotonic() + 10 + SETTLE_SECONDS
    while time.monotonic() < deadline:
        observer.observe(psutil.Process().children(recursive=True))
        if all(observer.renderers.get(r.pid) in {"enabled", "disabled"} for r in renderers):
            return
        time.sleep(0.1)
    raise AssertionError(f"renderers not classified: {observer.renderers}")


@pytest.mark.integration
def test_real_sandboxed_renderers_are_reported_enabled():
    with browser(chromium()) as instance:
        observer = SandboxObserver()
        with guardian_polls(observer):
            open_pages(instance, 3)
        renderers = live_renderers()
        assert renderers
        settle(observer, renderers)
        assert not {r.pid for r in renderers} & observer.others
        assert observer.state == "enabled"


@pytest.mark.integration
def test_a_real_no_sandbox_renderer_is_reported_at_once():
    with browser(chromium(), ["--no-sandbox"]) as instance:
        open_pages(instance, 1)
        observer = SandboxObserver()
        # One look, before any renderer can have settled: the flag decides.
        observer.observe(psutil.Process().children(recursive=True))
        assert observer.state == "disabled"


def capped_scope_available() -> bool:
    if not shutil.which("systemd-run"):
        return False
    probe = subprocess.run(
        ["systemd-run", "--user", "--scope", "-q", "-p", "CPUQuota=50%", "true"],
        capture_output=True,
        check=False,
    )
    return probe.returncode == 0


@pytest.mark.integration
def test_renderers_forked_under_cpu_contention_are_not_filed_away(tmp_path):
    """Chromium alone runs at a 10% CPU cap, so its renderers linger in the
    zygote's command line while the observer polls at full speed."""
    if not capped_scope_available():
        pytest.skip("systemd-run --user scopes are unavailable")
    wrapper = Path(tmp_path, "capped-chromium")
    wrapper.write_text(
        f'#!/bin/sh\nexec systemd-run --user --scope -q -p CPUQuota=10% "{chromium()}" "$@"\n'
    )
    wrapper.chmod(0o700)
    with browser(str(wrapper)) as instance:
        observer = SandboxObserver()
        with guardian_polls(observer):
            open_pages(instance, 8)
        renderers = live_renderers()
        assert len(renderers) >= 8
        settle(observer, renderers)
        assert not {r.pid for r in renderers} & observer.others
        assert observer.state == "enabled"
