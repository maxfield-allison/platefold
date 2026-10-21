"""A small Chrome DevTools Protocol client.

This replaces Playwright. Not because Playwright is bad — it is excellent —
but because its Python package drives the browser through a JavaScript driver,
which means shipping an entire 88 MB Node.js runtime to relay the handful of
commands this tool actually sends:

    Target.createTarget      open a page
    Target.attachToTarget    get a session for it
    Page.setDocumentContent  load our HTML
    Runtime.evaluate         measure diagrams, await fonts
    Emulation.setEmulatedMedia   switch to print
    Page.printToPDF          the actual output

That is the whole surface. Talking to the browser directly costs a few hundred
lines and a websocket, and takes more than half the weight out of the build.

The classes below deliberately mirror the small slice of Playwright's API the
pipeline was already written against, so the rest of the codebase did not have
to change shape when this landed.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import websocket  # websocket-client: a few hundred KB, no Node required

from .cancellation import CancelledError
from .limits import ResourceLimitError

# Flags that make a browser suitable for unattended rendering: no profile
# migration prompts, no background chatter, no GPU, no scrollbars bleeding
# into the printed output.
LAUNCH_FLAGS = [
    "--headless=new",
    "--disable-gpu",
    "--hide-scrollbars",
    "--mute-audio",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-extensions",
    "--disable-background-networking",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-sync",
    "--disable-default-apps",
    "--disable-component-update",
    "--disable-client-side-phishing-detection",
    "--metrics-recording-only",
    "--no-service-autorun",
    "--password-store=basic",
]

STARTUP_TIMEOUT = 30.0
COMMAND_TIMEOUT = 120.0
# Loading, measuring and printing a document grow with its size. A 100,000-cell
# table (about 2 MiB of HTML) prints with tags in 25 to 45 seconds on two
# cores, so allow at least three times that rate on top of the base timeout.
# Cancel and the export time limit still interrupt these waits.
CONTENT_SECONDS_PER_MIB = 60.0
CANCEL_POLL_INTERVAL = 0.1
# Closing never waits on a browser that has stopped answering: each teardown
# command gets this long.
CLOSE_COMMAND_TIMEOUT = 2.0
# How long a browser gets to exit by itself after Browser.close before it is
# killed. Its profile is a temporary directory that is deleted next, and every
# PDF has already been written, so nothing depends on an orderly shutdown.
# Chromium on Linux exits well inside this; on macOS it takes seconds, and
# several more if asked with SIGTERM instead.
CLOSE_GRACE_SECONDS = 1.0


class CDPError(RuntimeError):
    """The browser rejected a command."""


SANDBOX_UNAVAILABLE_MESSAGE = (
    "Chromium could not start its security sandbox, so Plate|Fold did not render. "
    "On Ubuntu 23.10 and later, AppArmor lets only applications covered by a profile "
    "create the user namespaces the sandbox needs. Install the Plate|Fold .deb package, "
    "which adds that profile (see LINUX-INSTALL.md), or install Google Chrome or Chromium "
    "and choose it as the browser. Plate|Fold never turns the sandbox off."
)


class SandboxUnavailableError(CDPError):
    """Chromium refused to start because the system denied its sandbox."""

    code = "browser_sandbox_unavailable"

    def __init__(self, details: str = ""):
        super().__init__(SANDBOX_UNAVAILABLE_MESSAGE)
        self.details = details


def _sandbox_unavailable(startup_log: str) -> bool:
    """Whether Chromium's startup log reports that no sandbox could be created."""
    return "No usable sandbox!" in startup_log


class TimeoutError_(RuntimeError):
    """The browser did not respond, or a condition never became true."""


def _make_user_data_dir(executable: str) -> str:
    """Create a browser profile somewhere both Plate|Fold and Chromium can see.

    Strictly confined Chromium snaps receive a private view of ``/tmp``.  A
    profile created there works inside Chromium, but the parent process cannot
    read its ``DevToolsActivePort`` file and mistakes a running browser for a
    startup timeout.  The snap's common user-data directory is shared with its
    launcher and remains private to the current user.
    """
    path = Path(executable)
    if path.parent == Path("/snap/bin"):
        parent = Path.home() / "snap" / path.name / "common"
        parent.mkdir(parents=True, exist_ok=True)
        return tempfile.mkdtemp(prefix="platefold-profile-", dir=parent)
    return tempfile.mkdtemp(prefix="platefold-profile-")


def _read_devtools_endpoint(user_data_dir: str, process: subprocess.Popen) -> str:
    """Wait for the browser to publish the port it actually bound.

    Chromium writes DevToolsActivePort once the debugging endpoint is live:
    the port on the first line, the browser websocket path on the second.
    Reading it is more reliable than assuming the port we asked for was free.
    """
    marker = os.path.join(user_data_dir, "DevToolsActivePort")
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise CDPError(
                "The browser exited immediately (code %s). It may be a version "
                "that does not support headless rendering." % process.returncode
            )
        try:
            with open(marker, encoding="utf-8") as fh:
                lines = fh.read().split("\n")
            if len(lines) >= 2 and lines[0].strip():
                return "ws://127.0.0.1:%s%s" % (lines[0].strip(), lines[1].strip())
        except OSError:
            pass
        time.sleep(0.02)
    raise TimeoutError_("The browser did not open a debugging endpoint in time.")


class Connection:
    """Request/response over one websocket, with session routing."""

    def __init__(self, url: str):
        self._ws = websocket.create_connection(
            url,
            timeout=COMMAND_TIMEOUT,
            max_size=None,  # printToPDF returns the whole document in one message
            suppress_origin=True,
            # The browser writes JSON; checking each frame's UTF-8 in pure
            # Python cost more than printing a large document.
            skip_utf8_validation=True,
            # TCP_NODELAY matters more than it looks. This is a strict
            # request/response protocol over loopback, so without it Nagle's
            # algorithm interacts with delayed ACKs and quietly adds tens of
            # milliseconds to every command - multiplied by several commands
            # per print pass and two passes per document.
            sockopt=((socket.IPPROTO_TCP, socket.TCP_NODELAY, 1),),
        )
        self._ws.settimeout(CANCEL_POLL_INTERVAL)
        self._next_id = 0
        self._should_cancel: Callable[[], bool] | None = None
        self._on_event: Callable[[dict], None] | None = None

    def set_cancel_callback(self, callback: Callable[[], bool] | None) -> None:
        self._should_cancel = callback

    def set_event_listener(self, listener: Callable[[dict], None] | None) -> None:
        """Pass unsolicited events to ``listener`` instead of dropping them.

        Events arrive only while a command waits for its reply, so a caller
        sends one more command to collect everything the browser raised
        before it. Rendering never sets a listener.
        """
        self._on_event = listener

    def send(
        self,
        method: str,
        params: dict | None = None,
        session: str | None = None,
        timeout: float | None = None,
    ) -> dict:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method}
        if params:
            message["params"] = params
        if session:
            message["sessionId"] = session
        if self._should_cancel and self._should_cancel():
            raise CancelledError("Export cancelled")
        self._ws.send(json.dumps(message))

        # Responses are interleaved with events; skip anything unsolicited
        # unless a listener asked for events.
        deadline = time.monotonic() + (COMMAND_TIMEOUT if timeout is None else timeout)
        while time.monotonic() < deadline:
            if self._should_cancel and self._should_cancel():
                raise CancelledError("Export cancelled")
            try:
                reply = json.loads(self._ws.recv())
            except websocket.WebSocketTimeoutException:
                continue
            if reply.get("id") != message["id"]:
                if "id" not in reply and self._on_event:
                    self._on_event(reply)
                continue
            if "error" in reply:
                err = reply["error"]
                raise CDPError("%s: %s" % (method, err.get("message", err)))
            return reply.get("result", {})
        raise TimeoutError_("No response to %s" % method)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._ws.close()


class Page:
    """One browser tab, exposing the operations the pipeline needs."""

    # Wait for the document to finish parsing *and* for its fonts to load,
    # inside the browser, so readiness costs one round trip rather than a
    # polling loop plus a separate font check. Fonts matter as much as parsing
    # here: printing before they resolve would measure and break lines against
    # fallback metrics.
    _READY = """new Promise(resolve => {
        const done = () => document.fonts.ready.then(() => resolve(true), () => resolve(true));
        if (document.readyState === 'complete') return done();
        window.addEventListener('load', done, {once: true});
    })"""

    def __init__(self, conn: Connection, session: str, target_id: str):
        self._conn = conn
        self._session = session
        self._target_id = target_id
        self._frame_id: str | None = None
        self._timeout = COMMAND_TIMEOUT
        self._viewport: tuple[int, int] | None = None
        self._media: str | None = None
        # Page and Runtime are deliberately left disabled: nothing here
        # consumes their events, and enabling them makes the browser stream
        # lifecycle and console traffic that every command then has to skip.

    def _send(self, method: str, params: dict | None = None) -> dict:
        return self._conn.send(method, params, session=self._session, timeout=self._timeout)

    def _frame(self) -> str:
        """The main frame id, which survives setDocumentContent."""
        if self._frame_id is None:
            tree = self._send("Page.getFrameTree")
            frame_id = tree.get("frameTree", {}).get("frame", {}).get("id")
            if not isinstance(frame_id, str):
                raise CDPError("Page.getFrameTree returned no main frame id")
            self._frame_id = frame_id
        return self._frame_id

    def set_viewport(self, width: int, height: int) -> None:
        self._viewport = (width, height)
        self._send(
            "Emulation.setDeviceMetricsOverride",
            {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False},
        )

    def set_content(self, html: str, wait_until: str = "load") -> None:
        """Replace the document, then wait for parsing and scripts to settle.

        Every later command on this page waits in proportion to the document's
        size, since layout and printing scale with it.
        """
        frame = self._frame()
        self._timeout = COMMAND_TIMEOUT + len(html) / (1024 * 1024) * CONTENT_SECONDS_PER_MIB
        self._send("Page.setDocumentContent", {"frameId": frame, "html": html})
        if wait_until:
            self.evaluate(self._READY)

    def evaluate(self, expression: str, arg: Any = None) -> Any:
        """Evaluate a JS expression, awaiting it if it returns a promise.

        Call sites pass an arrow function plus an argument, matching how the
        pipeline was originally written; that becomes an immediately invoked
        expression here.
        """
        if arg is not None or expression.lstrip().startswith("("):
            expression = "(%s)(%s)" % (expression, json.dumps(arg))
        result = self._send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": True,
                "returnByValue": True,
                "userGesture": True,
            },
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"]
            message = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise CDPError(str(message).split("\n")[0])
        return result.get("result", {}).get("value")

    def wait_for_function(self, expression: str, timeout: float = 30.0) -> None:
        """Wait until a JS expression is truthy, polling inside the browser."""
        poller = """new Promise((resolve, reject) => {
            const deadline = Date.now() + %d;
            const check = () => {
                try { if (%s) return resolve(true); } catch (e) { return reject(e); }
                if (Date.now() > deadline) return reject(new Error('timeout'));
                setTimeout(check, 5);
            };
            check();
        })""" % (int(timeout * 1000), expression)
        try:
            self.evaluate(poller)
        except CDPError as exc:
            raise TimeoutError_("Timed out waiting for: %s (%s)" % (expression, exc)) from None

    def emulate_media(self, media: str | None = None) -> None:
        self._media = media or ""
        self._send("Emulation.setEmulatedMedia", {"media": self._media})

    def block_network(self) -> None:
        """Refuse every external request, even if a document navigates away."""
        self._send("Network.enable")
        self._send(
            "Network.setBlockedURLs",
            {"urls": ["http://*", "https://*", "file://*", "ftp://*", "ws://*", "wss://*"]},
        )

    def renew(self) -> None:
        """Continue in a fresh tab with the same settings, closing this one.

        A renderer keeps much of the memory a large document's layout took,
        so a long series of large documents is printed in fresh tabs.
        """
        target_id = self._conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self._conn.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )["sessionId"]
        old = self._target_id
        self._session, self._target_id, self._frame_id = session, target_id, None
        self._timeout = COMMAND_TIMEOUT
        self.block_network()
        if self._viewport is not None:
            self.set_viewport(*self._viewport)
        if self._media is not None:
            self.emulate_media(self._media)
        with contextlib.suppress(Exception):
            self._conn.send("Target.closeTarget", {"targetId": old})

    def pdf(
        self,
        path: str | None = None,
        prefer_css_page_size: bool = False,
        print_background: bool = False,
        display_header_footer: bool = False,
        header_template: str = "",
        footer_template: str = "",
        max_bytes: int | None = None,
        page_ranges: str | None = None,
    ) -> bytes:
        """Print to PDF through a bounded stream, optionally writing it to disk.

        When ``path`` is supplied the return value is empty; avoiding a second
        in-memory copy is the reason the renderer uses CDP's stream mode.
        """
        import base64

        parameters = {
            "preferCSSPageSize": prefer_css_page_size,
            "printBackground": print_background,
            "displayHeaderFooter": display_header_footer,
            "headerTemplate": header_template,
            "footerTemplate": footer_template,
            # Chromium halves the default margins when a header and footer
            # are shown unless told otherwise; the stylesheet owns margins.
            "marginTop": 0,
            "marginBottom": 0,
            "marginLeft": 0,
            "marginRight": 0,
            # Keep semantic structure in every delivered PDF. Bookmark rewriting
            # must clone the document catalog as well as its pages.
            "generateTaggedPDF": True,
            "generateDocumentOutline": False,
            # A normal data response puts the entire base64 PDF in one
            # websocket message. Stream it so the configured output limit
            # can be enforced before the process holds multiple copies.
            "transferMode": "ReturnAsStream",
        }
        if page_ranges is not None:
            parameters["pageRanges"] = page_ranges
        result = self._send("Page.printToPDF", parameters)
        stream = result.get("stream")
        if not isinstance(stream, str):
            raise CDPError("Page.printToPDF returned no stream")
        collected = bytearray() if path is None else None
        total = 0
        try:
            with contextlib.ExitStack() as stack:
                output = stack.enter_context(open(path, "wb")) if path else None
                while True:
                    reply = self._send("IO.read", {"handle": stream, "size": 1024 * 1024})
                    value = reply.get("data", "")
                    if not isinstance(value, str):
                        raise CDPError("IO.read returned invalid PDF data")
                    data = base64.b64decode(value) if reply.get("base64Encoded") else value.encode()
                    total += len(data)
                    if max_bytes is not None and total > max_bytes:
                        raise ResourceLimitError(f"Rendered PDF exceeds the {max_bytes}-byte limit")
                    if output is not None:
                        output.write(data)
                    elif collected is not None:
                        collected.extend(data)
                    if reply.get("eof"):
                        break
        finally:
            with contextlib.suppress(Exception):
                self._send("IO.close", {"handle": stream})
        return bytes(collected) if collected is not None else b""

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._conn.send(
                "Target.closeTarget", {"targetId": self._target_id}, timeout=CLOSE_COMMAND_TIMEOUT
            )


class Browser:
    """A browser process plus its DevTools connection."""

    qualification_receipt: dict | None = None
    system_browser_failures: list[str] | None = None

    def __init__(self, executable: str, extra_flags: list[str] | None = None):
        self._user_data_dir = _make_user_data_dir(executable)
        args = [
            executable,
            *LAUNCH_FLAGS,
            *(extra_flags or []),
            # Port 0 lets the browser pick a free one and report it back via
            # DevToolsActivePort. Choosing a port ourselves means binding a
            # socket to test it, and on Windows the resulting TIME_WAIT state
            # can stop the browser binding the same port moments later.
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--user-data-dir=%s" % self._user_data_dir,
            "about:blank",
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        with tempfile.TemporaryFile() as startup_log:
            try:
                self._process = subprocess.Popen(
                    args,
                    stdout=subprocess.DEVNULL,
                    stderr=startup_log,
                    creationflags=creationflags,
                )
                endpoint = _read_devtools_endpoint(self._user_data_dir, self._process)
                self._conn = Connection(endpoint)
            except Exception as exc:
                startup_log.seek(0)
                details = startup_log.read().decode("utf-8", errors="replace")[-2000:].strip()
                self._terminate()
                if _sandbox_unavailable(details):
                    # Chromium's own text suggests --no-sandbox. Replace it with
                    # the supported routes rather than relaying that advice.
                    raise SandboxUnavailableError(details) from exc
                raise CDPError(f"{exc}\n{details}".strip()) from exc
        self._pages: list[Page] = []

    def set_cancel_callback(self, callback: Callable[[], bool] | None) -> None:
        """Make active DevTools waits interruptible by cooperative cancellation."""
        self._conn.set_cancel_callback(callback)

    def set_event_listener(self, listener: Callable[[dict], None] | None) -> None:
        """Receive DevTools events from every page; see Connection.set_event_listener."""
        self._conn.set_event_listener(listener)

    @property
    def version(self) -> str:
        try:
            return self._conn.send("Browser.getVersion").get("product", "unknown")
        except Exception:
            return "unknown"

    def new_page(self, viewport: dict | None = None) -> Page:
        target_id = self._conn.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self._conn.send(
            "Target.attachToTarget", {"targetId": target_id, "flatten": True}
        )["sessionId"]
        page = Page(self._conn, session, target_id)
        # Markdown, SVG and Mermaid are local rendering inputs. Block external
        # requests even if input navigates the document away from our CSP.
        page.block_network()
        if viewport:
            page.set_viewport(int(viewport["width"]), int(viewport["height"]))
        self._pages.append(page)
        return page

    def close(self) -> None:
        # Closing is not work a cancellation should interrupt. With the
        # callback still set, every command below would raise at once and the
        # browser would never be asked to quit.
        self._conn.set_cancel_callback(None)
        for page in self._pages:
            page.close()
        with contextlib.suppress(Exception):
            self._conn.send("Browser.close", timeout=CLOSE_COMMAND_TIMEOUT)
        self._conn.close()
        self._terminate()

    def _terminate(self) -> None:
        process = getattr(self, "_process", None)
        if process is not None and process.poll() is None:
            try:
                process.wait(timeout=CLOSE_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        shutil.rmtree(self._user_data_dir, ignore_errors=True)
