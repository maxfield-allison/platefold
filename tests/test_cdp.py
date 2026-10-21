import json

from platefold.cdp import COMMAND_TIMEOUT, CONTENT_SECONDS_PER_MIB, Page


class RecordingConnection:
    def __init__(self):
        self.calls = []
        self.timeouts = []

    def send(self, method, params=None, session=None, timeout=None):
        self.calls.append((method, params, session))
        self.timeouts.append((method, timeout))
        if method == "Page.getFrameTree":
            return {"frameTree": {"frame": {"id": "frame"}}}
        if method == "Page.printToPDF":
            return {"stream": "preview-stream"}
        if method == "IO.read":
            return {"data": "", "eof": True}
        return {}


def test_first_page_pdf_uses_chromiums_page_range():
    connection = RecordingConnection()
    page = Page(connection, "session", "target")

    page.pdf(page_ranges="1")

    method, parameters, session = connection.calls[0]
    assert method == "Page.printToPDF"
    assert parameters["pageRanges"] == "1"
    assert session == "session"


class ClosingConnection:
    """A connection whose cancel callback has fired, as after a cancelled export."""

    def __init__(self):
        self.cancel = lambda: True
        self.sent = []
        self.closed = False

    def set_cancel_callback(self, callback):
        self.cancel = callback

    def send(self, method, params=None, session=None, timeout=None):
        if self.cancel and self.cancel():
            raise RuntimeError("Export cancelled")
        self.sent.append((method, timeout))
        return {}

    def close(self):
        self.closed = True


class SlowProcess:
    """A browser process that has not exited when its grace period ends."""

    def __init__(self):
        self.waits = []
        self.signals = []

    def poll(self):
        return None

    def wait(self, timeout=None):
        self.waits.append(timeout)
        if not self.signals:
            import subprocess

            raise subprocess.TimeoutExpired("browser", timeout)
        return -9

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")


def _browser(tmp_path, connection, process):
    from platefold.cdp import Browser

    browser = Browser.__new__(Browser)
    browser._conn = connection
    browser._process = process
    browser._pages = [Page(connection, "session", "target")]
    browser._user_data_dir = str(tmp_path / "profile")
    (tmp_path / "profile").mkdir()
    return browser


def test_a_cancelled_export_still_asks_the_browser_to_quit(tmp_path):
    from platefold.cdp import CLOSE_COMMAND_TIMEOUT

    connection = ClosingConnection()
    _browser(tmp_path, connection, SlowProcess()).close()

    assert connection.sent == [
        ("Target.closeTarget", CLOSE_COMMAND_TIMEOUT),
        ("Browser.close", CLOSE_COMMAND_TIMEOUT),
    ]
    assert connection.closed


def test_a_browser_slow_to_exit_is_killed_after_a_short_grace(tmp_path):
    from platefold.cdp import CLOSE_GRACE_SECONDS

    process = SlowProcess()
    _browser(tmp_path, ClosingConnection(), process).close()

    # No SIGTERM: Chromium on macOS takes seconds to honour it, and the
    # temporary profile needs no orderly shutdown.
    assert process.signals == ["kill"]
    assert process.waits[0] == CLOSE_GRACE_SECONDS
    assert not (tmp_path / "profile").exists()


def test_page_commands_wait_in_proportion_to_the_loaded_document():
    connection = RecordingConnection()
    page = Page(connection, "session", "target")
    page.emulate_media("print")

    page.set_content("x" * (2 * 1024 * 1024), wait_until="")
    page.pdf()

    scaled = COMMAND_TIMEOUT + 2 * CONTENT_SECONDS_PER_MIB
    waits = dict(connection.timeouts)
    assert waits["Emulation.setEmulatedMedia"] == COMMAND_TIMEOUT
    assert waits["Page.setDocumentContent"] == scaled
    assert waits["Page.printToPDF"] == scaled


class ScriptedSocket:
    """A DevTools websocket that answers with a fixed sequence of messages."""

    def __init__(self, messages):
        self.messages = [json.dumps(message) for message in messages]
        self.sent = []

    def settimeout(self, _timeout):
        pass

    def send(self, text):
        self.sent.append(json.loads(text))

    def recv(self):
        return self.messages.pop(0)


def _connection(monkeypatch, messages):
    from platefold import cdp

    socket = ScriptedSocket(messages)
    monkeypatch.setattr(cdp.websocket, "create_connection", lambda *a, **k: socket)
    return cdp.Connection("ws://127.0.0.1:1/devtools/browser/test")


EVENT = {"method": "Runtime.exceptionThrown", "sessionId": "page", "params": {}}
OTHER_REPLY = {"id": 99, "result": {"stale": True}}


def test_events_that_arrive_while_waiting_reach_a_listener(monkeypatch):
    connection = _connection(monkeypatch, [EVENT, OTHER_REPLY, {"id": 1, "result": {"ok": 1}}])
    events = []
    connection.set_event_listener(events.append)

    assert connection.send("Runtime.evaluate") == {"ok": 1}
    # Replies to other commands are not events.
    assert events == [EVENT]


def test_events_are_dropped_without_a_listener(monkeypatch):
    connection = _connection(monkeypatch, [EVENT, {"id": 1, "result": {"ok": 1}}])

    assert connection.send("Runtime.evaluate") == {"ok": 1}
