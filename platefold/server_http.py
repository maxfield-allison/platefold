"""uvicorn's h11 protocol with a deadline and a size limit for each request head.

uvicorn closes idle keep-alive connections, but it places no limit on how long
a client may take to send request headers, and any received byte cancels the
keep-alive timer. A few dozen connections that trickle partial headers can
therefore hold every concurrency slot indefinitely (slowloris). This protocol
gives each connection ``head_seconds`` from opening, and again from each
completed response, to deliver a complete request head. Once a head arrives,
the handler's own deadlines bound its body and response. A body the handler
never reads, such as one sent after an authentication failure, falls under
the next head deadline.

h11's ``max_incomplete_event_size`` only bounds a head that is still
incomplete in the receive buffer: a larger head that arrives in one read is
parsed in full. ``HeadLimitedConnection`` measures the complete head, from the
request line through the blank line that ends it, and refuses anything over
``head_bytes`` with 431 Request Header Fields Too Large.

The deadline shortens how long each connection can be held; it cannot stop a
client that keeps reopening connections. Put an exposed server behind a
buffering reverse proxy with per-client connection limits.
"""

from __future__ import annotations

import asyncio
import re

import h11
from uvicorn.protocols.http.h11_impl import STATUS_PHRASES, H11Protocol

HEAD_SECONDS = 10.0
HEAD_BYTES = 16 * 1024
# The line ending that closes a head, matched the way h11 finds it.
_HEAD_END = re.compile(b"\n\r?\n")


class HeadLimitedConnection(h11.Connection):
    """An h11 server connection that refuses request heads over ``head_bytes``."""

    def __init__(self, head_bytes: int, max_incomplete_event_size: int | None = None) -> None:
        if max_incomplete_event_size is None:
            super().__init__(h11.SERVER)
        else:
            super().__init__(h11.SERVER, max_incomplete_event_size=max_incomplete_event_size)
        self.head_bytes = head_bytes
        self.head_too_large = False
        # An upper bound on the unparsed bytes, so the buffer is only copied
        # and searched when it could hold an oversized head.
        self._unparsed = 0

    def receive_data(self, data: bytes) -> None:
        super().receive_data(data)
        self._unparsed += len(data)

    def next_event(self):
        if self.their_state is h11.IDLE and self._unparsed > self.head_bytes:
            buffered, _ = self.trailing_data
            self._unparsed = len(buffered)
            # A head fits only if its closing blank line ends within the limit.
            if len(buffered) > self.head_bytes and not _HEAD_END.search(
                buffered, 0, self.head_bytes
            ):
                self.head_too_large = True
                raise h11.RemoteProtocolError(
                    f"request head exceeds {self.head_bytes} bytes", error_status_hint=431
                )
        event = super().next_event()
        if isinstance(event, h11.Request):
            self._unparsed = len(self.trailing_data[0])
        return event


class DeadlineH11Protocol(H11Protocol):
    head_seconds = HEAD_SECONDS
    head_bytes = HEAD_BYTES
    _head_timer: asyncio.TimerHandle | None = None

    def __init__(self, config, server_state, app_state, _loop=None) -> None:
        super().__init__(config, server_state, app_state, _loop)
        self.conn = HeadLimitedConnection(self.head_bytes, config.h11_max_incomplete_event_size)

    def send_400_response(self, msg: str) -> None:
        conn = self.conn
        if not (isinstance(conn, HeadLimitedConnection) and conn.head_too_large):
            super().send_400_response(msg)
            return
        body = f"Request head exceeds {self.head_bytes // 1024} KiB".encode("ascii")
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"connection", b"close"),
        ]
        for event in (
            h11.Response(status_code=431, headers=headers, reason=STATUS_PHRASES[431]),
            h11.Data(data=body),
            h11.EndOfMessage(),
        ):
            self.transport.write(conn.send(event) or b"")
        self.transport.close()

    def connection_made(self, transport) -> None:  # type: ignore[override]
        super().connection_made(transport)
        self._arm_head_deadline()

    def connection_lost(self, exc: Exception | None) -> None:
        self._cancel_head_deadline()
        super().connection_lost(exc)

    def on_response_complete(self) -> None:
        super().on_response_complete()
        self._arm_head_deadline()

    def _arm_head_deadline(self) -> None:
        self._cancel_head_deadline()
        if not self.transport.is_closing():
            self._head_timer = self.loop.call_later(self.head_seconds, self._head_deadline)

    def _cancel_head_deadline(self) -> None:
        if self._head_timer is not None:
            self._head_timer.cancel()
            self._head_timer = None

    def _head_deadline(self) -> None:
        self._head_timer = None
        cycle = getattr(self, "cycle", None)
        if cycle is not None and not cycle.response_complete:
            return  # A request is in progress under its handler's deadlines.
        if not self.transport.is_closing():
            self.transport.close()
