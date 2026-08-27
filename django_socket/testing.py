"""Test client: exercise your handlers without starting a server.

    from django_socket.testing import WebSocketClient

    async def test_chat_fans_out():
        async with WebSocketClient("/chat/general/") as a, \\
                   WebSocketClient("/chat/general/") as b:
            await b.send_json({"type": "message", "text": "hi"})
            assert (await a.receive_json())["text"] == "hi"

It speaks ASGI straight to the dispatcher, so it goes through the same path a
real connection does -- route, converters, origin validation, session, groups --
but with no sockets, no ports and in milliseconds.

Every `receive` has a timeout: a test waiting for something that never arrives
fails in one second instead of hanging.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .websocket import InvalidJSON, Message, WebSocketDisconnect

TIMEOUT = 1.0


class ReceiveTimeout(AssertionError):
    """Nothing arrived within the deadline.

    An AssertionError so it reads as a failing test, not as an error.
    """


class WebSocketClient:
    """
    A fake websocket client against the real dispatcher.

    `path`         the path, exactly as a browser would write it ("/chat/x/")
    `user`         a User: a session is created and `sock.user` sees it logged in
    `headers`      extra headers (`origin` is already set)
    `cookies`      dict of cookies; merged with the session from `user`
    `query`        "token=abc&n=3"
    `subprotocols` the ones a browser would offer
    """

    def __init__(
        self,
        path: str,
        *,
        user=None,
        headers: dict | None = None,
        cookies: dict | None = None,
        query: str = "",
        subprotocols=(),
        origin: str = "http://testserver",
    ):
        self.path = path
        self.user = user
        self._headers = dict(headers or {})
        self._cookies = dict(cookies or {})
        self._query = query
        self._subprotocols = list(subprotocols)
        if origin is not None:
            self._headers.setdefault("origin", origin)

        self._to_server: asyncio.Queue = asyncio.Queue()
        self._to_client: asyncio.Queue = asyncio.Queue()
        self._buffer: list[dict] = []      # what we already pulled off the queue
        self._task: asyncio.Task | None = None

        self.accepted = False
        self.subprotocol: str | None = None
        self.close_code: int | None = None
        self.close_reason: str = ""

    @property
    def connected(self) -> bool:
        """
        Whether you have a usable connection.

        Different from `accepted`, which says literally whether the server sent
        a `websocket.accept`. A coded rejection (4404, 4401...) looks like
        accept + close on the wire, because closing without accepting would
        leave the browser with a reasonless 1006. For "did it let me in?" use
        this one.
        """
        return self.accepted and self.close_code is None

    # ------------------------------------------------------------- life cycle

    async def connect(self, timeout: float = TIMEOUT) -> "WebSocketClient":
        """
        Open the connection and wait for the handshake response.

        It does not raise when the server rejects: look at `connected` and
        `close_code`, which is what you want to assert when testing a
        rejection.
        """
        if self.user is not None:
            self._cookies.setdefault(
                await _session_cookie_name(), await _make_session(self.user)
            )
        if self._cookies:
            self._headers["cookie"] = "; ".join(
                f"{k}={v}" for k, v in self._cookies.items()
            )

        from . import dispatch

        self._to_server.put_nowait({"type": "websocket.connect"})
        self._task = asyncio.create_task(
            dispatch.handle_websocket(self._scope(), self._receive, self._send)
        )

        event = await self._next_event(timeout, what="handshake response")
        if event["type"] == "websocket.accept":
            self.accepted = True
            self.subprotocol = event.get("subprotocol")
            await self._check_immediate_close()
        else:
            self._record_close(event)
        return self

    async def _check_immediate_close(self) -> None:
        """
        Let the handler make progress and record the close if one is already on
        its way, so `connected` tells the truth the moment connect() returns.

        Whatever comes off the queue goes into the buffer, so `receive()` still
        sees it in order: neither a message nor the close is lost.
        """
        for _ in range(3):
            await asyncio.sleep(0)
        while not self._to_client.empty():
            event = self._to_client.get_nowait()
            self._buffer.append(event)
            if event["type"] == "websocket.close":
                self._record_close(event)
                return

    async def disconnect(self, code: int = 1000) -> None:
        """The client leaves. Waits for the handler to finish its cleanup."""
        if self._task is None or self._task.done():
            return
        self._to_server.put_nowait({"type": "websocket.disconnect", "code": code})
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            self._task.cancel()
        self._drain_closes()

    async def __aenter__(self) -> "WebSocketClient":
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # ---------------------------------------------------------------- sending

    async def send(self, data: Any) -> None:
        """str -> text, bytes -> binary, anything else -> JSON."""
        if isinstance(data, str):
            await self.send_text(data)
        elif isinstance(data, (bytes, bytearray)):
            await self.send_bytes(bytes(data))
        else:
            await self.send_json(data)

    async def send_text(self, text: str) -> None:
        self._to_server.put_nowait({"type": "websocket.receive", "text": text})
        await asyncio.sleep(0)      # let the handler run

    async def send_bytes(self, data: bytes) -> None:
        self._to_server.put_nowait({"type": "websocket.receive", "bytes": data})
        await asyncio.sleep(0)

    async def send_json(self, data: Any) -> None:
        import json

        await self.send_text(json.dumps(data, default=str))

    # -------------------------------------------------------------- receiving

    async def receive(self, timeout: float = TIMEOUT) -> Message:
        """
        The next message. Raises `WebSocketDisconnect` if the server closes and
        `ReceiveTimeout` if nothing arrives.
        """
        event = await self._next_event(timeout, what="message")
        if event["type"] == "websocket.close":
            self._record_close(event)
            raise WebSocketDisconnect(self.close_code, self.close_reason)
        return Message(text=event.get("text"), data=event.get("bytes"))

    async def receive_text(self, timeout: float = TIMEOUT) -> str:
        msg = await self.receive(timeout)
        if msg.text is None:
            raise AssertionError("Expected text, got a binary frame.")
        return msg.text

    async def receive_bytes(self, timeout: float = TIMEOUT) -> bytes:
        msg = await self.receive(timeout)
        if msg.bytes is None:
            raise AssertionError("Expected binary, got a text frame.")
        return msg.bytes

    async def receive_json(self, timeout: float = TIMEOUT) -> Any:
        return (await self.receive(timeout)).json()

    async def receive_all(self, timeout: float = 0.1) -> list[Message]:
        """Everything pending right now. Handy after a broadcast."""
        messages = []
        while True:
            try:
                messages.append(await self.receive(timeout))
            except (ReceiveTimeout, WebSocketDisconnect):
                return messages

    async def receive_nothing(self, timeout: float = 0.1) -> bool:
        """True if nothing arrives. For asserting that a group is isolated."""
        try:
            await self._next_event(timeout, what="anything")
        except ReceiveTimeout:
            return True
        return False

    async def wait_closed(self, timeout: float = TIMEOUT) -> int:
        """Wait for the server to close and return the code."""
        if self.close_code is not None:
            return self.close_code
        try:
            while True:
                event = await self._next_event(timeout, what="close")
                if event["type"] == "websocket.close":
                    self._record_close(event)
                    return self.close_code
        except ReceiveTimeout:
            raise AssertionError(
                f"The server did not close within {timeout}s (still open)."
            ) from None

    # --------------------------------------------------------------- internals

    def _scope(self) -> dict:
        headers = [
            (k.lower().encode("latin-1"), str(v).encode("latin-1"))
            for k, v in self._headers.items()
            if v is not None
        ]
        return {
            "type": "websocket",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": self._query.encode(),
            "headers": headers,
            "subprotocols": self._subprotocols,
            "client": ("127.0.0.1", 54321),
            "server": ("testserver", 80),
            "scheme": "ws",
        }

    async def _receive(self) -> dict:
        return await self._to_server.get()

    async def _send(self, message: dict) -> None:
        await self._to_client.put(message)

    async def _next_event(self, timeout: float, what: str) -> dict:
        if self._buffer:
            return self._buffer.pop(0)
        try:
            return await asyncio.wait_for(self._to_client.get(), timeout=timeout)
        except asyncio.TimeoutError:
            self._raise_if_handler_died()
            raise ReceiveTimeout(
                f"No {what} arrived from {self.path} within {timeout}s."
            ) from None

    def _raise_if_handler_died(self) -> None:
        """If the handler really died, that error is more useful than a timeout."""
        if self._task is not None and self._task.done():
            exc = self._task.exception()
            if exc is not None:
                raise exc

    def _record_close(self, event: dict) -> None:
        self.close_code = event.get("code", 1000)
        self.close_reason = event.get("reason", "")

    def _drain_closes(self) -> None:
        while not self._to_client.empty():
            self._buffer.append(self._to_client.get_nowait())
        for event in self._buffer:
            if event["type"] == "websocket.close":
                self._record_close(event)


# -------------------------------------------------------------------- session


async def _session_cookie_name() -> str:
    from django.conf import settings

    return settings.SESSION_COOKIE_NAME


async def _make_session(user) -> str:
    """Leave an authenticated session in the DB, like a real login would."""
    from importlib import import_module

    from django.conf import settings
    from django.contrib.auth import (
        BACKEND_SESSION_KEY,
        HASH_SESSION_KEY,
        SESSION_KEY,
    )

    engine = import_module(settings.SESSION_ENGINE)
    session = engine.SessionStore()
    session[SESSION_KEY] = str(user.pk)
    session[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
    session[HASH_SESSION_KEY] = user.get_session_auth_hash()

    if hasattr(session, "acreate"):
        await session.acreate()
    else:  # pragma: no cover - Django < 5.0
        from asgiref.sync import sync_to_async

        await sync_to_async(session.create)()
    return session.session_key


__all__ = [
    "InvalidJSON",
    "ReceiveTimeout",
    "WebSocketClient",
    "WebSocketDisconnect",
]
