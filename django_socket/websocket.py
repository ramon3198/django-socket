"""The WebSocket object every handler receives."""

from __future__ import annotations

import asyncio
import json
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs


class WebSocketDisconnect(Exception):
    """The connection is gone."""

    def __init__(self, code: int = 1000, reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"WebSocket closed (code={code})")


class WebSocketClosed(Exception):
    """Something tried to use a socket the server has already closed."""


class RateLimited(Exception):
    """The client is sending faster than allowed."""

    def __init__(self, retry_after: float, code: int):
        self.retry_after = retry_after
        self.code = code
        super().__init__(f"Rate limit exceeded; retry in {retry_after:.1f}s")


class InvalidJSON(ValueError):
    """
    The client sent something that is not valid JSON.

    It is a ValueError, so a plain `except ValueError` still catches it. It
    exists as its own type so the dispatcher can tell it apart from a server
    failure: the client's fault closes with 4400, not with a 1011 that would
    also fill your log with tracebacks that are not yours.
    """

    def __init__(self, raw, reason=""):
        self.raw = raw
        sample = str(raw)
        if len(sample) > 80:
            sample = sample[:77] + "..."
        super().__init__(
            f"Invalid JSON from the client: {sample!r}"
            + (f" ({reason})" if reason else "")
        )


_encoder = None


def _get_encoder():
    """
    DjangoJSONEncoder, imported late so importing this module never touches
    settings.

    It matters because it is the one that knows Django's types, and above all
    because of dates. `str(aware)` gives "2026-08-26 19:43:30.251057+00:00";
    the encoder gives "2026-08-26T19:43:30.251Z". Two differences that count:

    * ISO-8601 is the only format the ECMAScript spec requires `Date` to
      parse. Anything else is each engine's fallback -- V8 is lenient and
      accepts it, others historically are not.
    * `str()` emits microseconds (6 digits) and `Date` only understands
      milliseconds; the encoder truncates to 3, which is what JS can represent.

    On the way it also renders `Decimal` as a string so precision is not lost,
    and serializes `UUID` and lazy translation strings on its own.
    """
    global _encoder
    if _encoder is None:
        from django.core.serializers.json import DjangoJSONEncoder

        class SocketJSONEncoder(DjangoJSONEncoder):
            def default(self, o):
                try:
                    return super().default(o)
                except TypeError:
                    raise TypeError(
                        f"Cannot send a {type(o).__name__} over the socket. "
                        f"The usual Django types (datetime, date, time, "
                        f"timedelta, Decimal, UUID, lazy strings) go through "
                        f"on their own; convert the rest yourself: a model to "
                        f"a dict, a QuerySet to a list. For the old permissive "
                        f"behaviour: sock.send_json(value, default=str)."
                    ) from None

        _encoder = SocketJSONEncoder
    return _encoder


class Message:
    """An incoming message. Use `.text`, `.bytes` or `.json()`."""

    __slots__ = ("text", "bytes")

    def __init__(self, text: str | None = None, data: bytes | None = None):
        self.text = text
        self.bytes = data

    def json(self, **kwargs) -> Any:
        """Parse the message as JSON. Raises `InvalidJSON` if it is not."""
        raw = self.text
        if raw is None:
            if self.bytes is None:
                raise InvalidJSON("", "empty message")
            try:
                raw = self.bytes.decode()
            except UnicodeDecodeError as exc:
                raise InvalidJSON(self.bytes, "not valid UTF-8") from exc
        try:
            return json.loads(raw, **kwargs)
        except ValueError as exc:
            raise InvalidJSON(raw, str(exc)) from None

    @property
    def is_text(self) -> bool:
        return self.text is not None

    def __eq__(self, other) -> bool:
        """Allows `if msg == "ping"` without reaching for .text."""
        if isinstance(other, str):
            return self.text == other
        if isinstance(other, (bytes, bytearray)):
            return self.bytes == bytes(other)
        if isinstance(other, Message):
            return self.text == other.text and self.bytes == other.bytes
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.text, self.bytes))

    def __str__(self) -> str:
        return self.text if self.text is not None else repr(self.bytes)

    def __repr__(self) -> str:
        preview = str(self)
        if len(preview) > 40:
            preview = preview[:37] + "..."
        return f"<Message {preview!r}>"


CONNECTING, OPEN, CLOSED = "connecting", "open", "closed"


class WebSocket:
    """
    A wrapper over the ASGI (receive, send) pair.

    You don't need to call `accept()`: the handshake completes on its own the
    first time you send, receive or iterate. Call it by hand only when you need
    to set a subprotocol or headers, and call `close()` up front to reject.
    """

    def __init__(self, scope, receive, send, *, layer=None):
        self.scope = scope
        self._receive = receive
        self._send = send
        self._layer = layer
        self._state = CONNECTING
        self._groups: set[str] = set()
        self.group: str | None = None   # default target for broadcast()
        self._rate = None                           # rate-limit bucket
        self._outbox: asyncio.Queue | None = None   # fan-out only
        self._outbox_policy: str = "close"
        self._writer: asyncio.Task | None = None
        self.close_code: int | None = None
        # Filled in by the dispatcher before the handler runs.
        self.user = None
        self.session = None
        self.path_params: dict[str, Any] = {}

    # ------------------------------------------------------------------- data

    @property
    def path(self) -> str:
        return self.scope.get("path", "")

    @property
    def headers(self) -> dict[str, str]:
        if not hasattr(self, "_headers"):
            self._headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in self.scope.get("headers", [])
            }
        return self._headers

    @property
    def query_params(self) -> dict[str, str]:
        """Only the first value of each key; use `query_lists` if they repeat."""
        if not hasattr(self, "_qp"):
            raw = self.scope.get("query_string", b"").decode("utf-8", "replace")
            self._ql = parse_qs(raw, keep_blank_values=True)
            self._qp = {k: v[0] for k, v in self._ql.items()}
        return self._qp

    @property
    def query_lists(self) -> dict[str, list[str]]:
        self.query_params  # noqa: B018 - forces the parse
        return self._ql

    @property
    def cookies(self) -> dict[str, str]:
        if not hasattr(self, "_cookies"):
            jar = SimpleCookie()
            jar.load(self.headers.get("cookie", ""))
            self._cookies = {k: v.value for k, v in jar.items()}
        return self._cookies

    @property
    def subprotocols(self) -> list[str]:
        return self.scope.get("subprotocols", [])

    @property
    def client(self) -> tuple[str, int] | None:
        c = self.scope.get("client")
        return tuple(c) if c else None

    @property
    def connected(self) -> bool:
        return self._state == OPEN

    @property
    def groups(self) -> frozenset[str]:
        """Which groups you are a member of right now (empty after disconnect).

        Different from `sock.group`, which is the default target for
        broadcast() and keeps pointing at the same place after disconnection.
        """
        return frozenset(self._groups)

    def __repr__(self) -> str:
        return f"<WebSocket {self.path} {self._state}>"

    # -------------------------------------------------------------- handshake

    async def accept(self, subprotocol: str | None = None, headers=None) -> None:
        if self._state != CONNECTING:
            return
        msg = {"type": "websocket.accept", "subprotocol": subprotocol}
        if headers:
            msg["headers"] = [
                (
                    k.encode() if isinstance(k, str) else k,
                    v.encode() if isinstance(v, str) else v,
                )
                for k, v in headers
            ]
        await self._send(msg)
        self._state = OPEN

    async def _ensure_open(self) -> None:
        if self._state == CONNECTING:
            await self.accept()
        elif self._state == CLOSED:
            raise WebSocketClosed("The socket is already closed.")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """
        Close, delivering `code` and `reason` to the client.

        If the handshake has not completed yet, it completes first: closing
        without accepting makes the server answer an HTTP 403 and the browser
        gets an `onclose` with code 1006 and no reason. Accepting and closing
        right after means the client's JS receives your code as-is and can tell
        "needs login" from "no such room". Use `deny()` if you would rather
        kill the handshake instead.
        """
        if self._state == CLOSED:
            return
        await self._flush_outbox()
        await self._leave_all()
        try:
            if self._state == CONNECTING:
                await self.accept()
            await self._send(
                {"type": "websocket.close", "code": code, "reason": reason}
            )
        except Exception:
            pass  # the client is already gone
        self._state = CLOSED
        self.close_code = code

    async def deny(self, code: int = 403) -> None:
        """
        Reject the handshake without accepting it: the client sees an HTTP 403
        and a WebSocket never comes into being. Safer when the connection
        should not have been attempted at all (a disallowed origin).
        """
        if self._state != CONNECTING:
            return await self.close(1008, "Denied")
        try:
            await self._send({"type": "websocket.close", "code": code})
        except Exception:
            pass
        self._state = CLOSED
        self.close_code = code

    # ------------------------------------------------------------------ input

    async def receive(self) -> Message:
        """
        The next message. Raises `WebSocketDisconnect` once there is no
        connection left, whatever the cause.

        That "whatever the cause" matters: the socket can also close without
        the client leaving -- because we evicted it for not consuming, or
        because its writer died. That used to surface as `WebSocketClosed`,
        which is not a `WebSocketDisconnect`, so it blew up the handler's
        `async for` instead of ending it: a traceback in the log, a 1011 close,
        and the cleanup code after the loop never running. It showed up at 6000
        connections, which is exactly when evictions start.
        """
        if self._state == CLOSED:
            raise WebSocketDisconnect(
                self.close_code if self.close_code is not None else 1006,
                "the socket was already closed",
            )
        await self._ensure_open()
        event = await self._receive()
        if event["type"] == "websocket.receive" and self._rate is not None:
            if not self._rate.consume():
                from .ratelimit import CLOSE_RATE_LIMIT

                raise RateLimited(self._rate.retry_after, CLOSE_RATE_LIMIT)
        if event["type"] == "websocket.disconnect":
            self._state = CLOSED
            self.close_code = event.get("code", 1005)
            await self._leave_all()
            raise WebSocketDisconnect(self.close_code, event.get("reason", ""))
        return Message(text=event.get("text"), data=event.get("bytes"))

    async def receive_text(self) -> str:
        msg = await self.receive()
        if msg.text is None:
            raise TypeError("Expected a text frame, got a binary one.")
        return msg.text

    async def receive_bytes(self) -> bytes:
        msg = await self.receive()
        if msg.bytes is None:
            raise TypeError("Expected a binary frame, got a text one.")
        return msg.bytes

    async def receive_json(self, **kwargs) -> Any:
        return (await self.receive()).json(**kwargs)

    def __aiter__(self):
        return self

    async def __anext__(self) -> Message:
        try:
            return await self.receive()
        except WebSocketDisconnect:
            raise StopAsyncIteration

    async def iter_text(self):
        async for msg in self:
            if msg.text is not None:
                yield msg.text

    async def iter_json(self):
        async for msg in self:
            yield msg.json()

    # ----------------------------------------------------------------- output

    async def send(self, data: Any) -> None:
        """str -> text, bytes -> binary, anything else -> JSON."""
        if isinstance(data, str):
            await self.send_text(data)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            await self.send_bytes(bytes(data))
        else:
            await self.send_json(data)

    async def send_text(self, text: str) -> None:
        await self._ensure_open()
        await self._send({"type": "websocket.send", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        await self._ensure_open()
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: Any, **kwargs) -> None:
        """
        Serialize with Django's encoder: `datetime` in ISO-8601, `Decimal` as a
        string, `UUID` and lazy strings too.

        An object it cannot serialize raises a TypeError that names it and says
        what to do, instead of shipping `"User object (3)"` to the browser and
        letting you find out in production.
        """
        if "default" not in kwargs:
            kwargs.setdefault("cls", _get_encoder())
        await self.send_text(json.dumps(data, **kwargs))

    # ----------------------------------------------------------------- groups

    async def join(self, *groups: str) -> None:
        """The first group you join becomes the default broadcast target."""
        for g in groups:
            await self._layer.add(g, self)
            self._groups.add(g)
            if self.group is None:
                self.group = g

    async def leave(self, *groups: str) -> None:
        for g in groups:
            await self._layer.discard(g, self)
            self._groups.discard(g)
            if self.group == g:
                self.group = next(iter(self._groups), None)

    async def broadcast(
        self, data: Any, *, to: str | None = None, exclude_self: bool = False
    ) -> None:
        """
        Send to every member of a group.

            await sock.broadcast(data)                  # to the default group
            await sock.broadcast(data, to="other:group")
            await sock.broadcast(data, exclude_self=True)
        """
        target = to or self.group
        if target is None:
            raise ValueError(
                "sock.broadcast(data) with no default group. Join one with "
                "await sock.join('my:group'), declare it on the route with "
                "@ws(..., group='my:{param}'), or name the target with "
                "sock.broadcast(data, to='my:group')."
            )
        await self._layer.send(target, data, exclude=self if exclude_self else None)

    # ---------------------------------------------------------------- fan-out

    async def enqueue(self, data: Any) -> bool:
        """
        Queue a broadcast message. `False` when this client is so far behind
        that it has to be evicted.

        It does not wait for the write, and that is the whole point.
        `sock.send()` does wait -- blocking there is healthy: if the client
        cannot keep up with you, your handler slows down. But waiting inside a
        broadcast is ruinous: a single client that stops reading hangs the
        broadcaster forever, and with it that handler's read loop and cleanup.
        Measured: uvicorn applies backpressure at around 24 MB, and past that
        `send()` never returns.
        """
        if self._state == CLOSED:
            return False

        if self._outbox is None:
            maximum, self._outbox_policy = _config_outbox()
            self._outbox = asyncio.Queue(maxsize=maximum)
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._drain_loop())

        try:
            self._outbox.put_nowait(data)
            return True
        except asyncio.QueueFull:
            if self._outbox_policy == "drop_oldest":
                # For streams that tolerate gaps (cursor positions, telemetry):
                # better to lose the oldest value than to evict the client.
                try:
                    self._outbox.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._outbox.put_nowait(data)
                return True
            return False

    async def _drain_loop(self) -> None:
        """Write the outbox in order. If the socket dies, leave its groups."""
        try:
            while True:
                data = await self._outbox.get()
                try:
                    await self.send(data)
                except Exception:
                    self._outbox.task_done()
                    self._state = CLOSED
                    await self._leave_all()
                    return
                self._outbox.task_done()
        except asyncio.CancelledError:
            raise

    def evict(self, code: int = 1013, reason: str = "Client too slow") -> None:
        """
        Throw out a client that is not consuming, without waiting for it.

        `await close()` is not an option: writing towards it is blocked, which
        is precisely why we are evicting it. The writer is cancelled and the
        close goes out in the background with a deadline.

        That client's handler stays parked in `receive()` until the ASGI server
        delivers its `websocket.disconnect`; with uvicorn that arrives within
        ping_interval + ping_timeout at the latest (40 s by default). Meanwhile
        it consumes nothing and is already out of every group.
        """
        if self._state == CLOSED:
            return
        self._state = CLOSED
        self.close_code = code
        if self._writer is not None:
            self._writer.cancel()
        asyncio.get_running_loop().create_task(self._close_later(code, reason))

    async def _close_later(self, code: int, reason: str) -> None:
        try:
            await asyncio.wait_for(
                self._send(
                    {"type": "websocket.close", "code": code, "reason": reason}
                ),
                timeout=5,
            )
        except Exception:
            pass

    async def drain(self, timeout: float = 1.0) -> None:
        """
        Wait for everything queued for fan-out to go out.

        Not needed in production: the writer runs on its own and `broadcast`
        deliberately waits for nobody. In a test it is, so you can assert on
        what has actually arrived instead of sleeping blindly and hoping.
        """
        if self._outbox is None:
            return
        try:
            await asyncio.wait_for(self._outbox.join(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _flush_outbox(self, deadline: float = 1.0) -> None:
        """Give the writer one last chance before closing."""
        if self._outbox is None or self._writer is None or self._writer.done():
            return
        try:
            await asyncio.wait_for(self._outbox.join(), timeout=deadline)
        except Exception:
            pass

    def _stop_writer(self) -> None:
        """
        Cancel the writer task. Idempotent.

        It lives in the common cleanup and not only in `close()`, because the
        usual path is the other one: the client disconnects, `receive()` marks
        the socket closed, and then `close()` returns early without cancelling
        anything. That was one orphaned task per connection that ended.
        """
        if self._writer is None or self._writer.done():
            return
        # Don't cancel yourself: _drain_loop() comes through here when it fails.
        try:
            if asyncio.current_task() is self._writer:
                return
        except RuntimeError:
            pass
        self._writer.cancel()

    async def _leave_all(self) -> None:
        """
        Take the socket out of its groups, but do NOT clear `self.group`.

        `groups` is which groups you are a member of; `group` is where
        broadcast() points by default. On disconnect you stop being a member,
        but the target is still valid -- otherwise the most common pattern
        there is

            async for msg in sock:
                ...
            await sock.broadcast({"kind": "leave"})   # <- no group left

        would lose its target on the exact line where it is needed.
        """
        self._stop_writer()
        if self._groups and self._layer is not None:
            for g in list(self._groups):
                await self._layer.discard(g, self)
            self._groups.clear()


def _config_outbox() -> tuple[int, str]:
    """
    Size of the fan-out outbox and what to do when it fills up.

    The 256 is not a round number picked by eye: measured, one process
    publishes ~1,500 broadcasts/s against Redis, so an outbox of 64 would fill
    in 42 ms at full rate -- less than a mobile network hiccup or a GC pause,
    and you would be evicting healthy clients. At 256 the margin rises to
    ~170 ms in the worst case, and to tens of seconds at normal chat rates.

    Only stuck clients pay the memory cost: one that keeps up has an empty
    outbox. It is `stuck x 256 x message_size`.
    """
    from django.conf import settings

    conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
    return int(conf.get("SEND_QUEUE_MAX", 256)), conf.get("SEND_QUEUE_FULL", "close")
