"""Routing JSON messages by type.

Most apps that speak JSON over a socket send `{"type": "something", ...}` and
end up with a long if/elif inside the loop. This turns that into named
functions without hiding the flow:

    from django_socket import Events, ws

    chat = Events()

    @chat.on("message")
    async def message(sock, data):
        await sock.broadcast({"type": "message", "text": data["text"]})

    @chat.on("typing")
    async def typing(sock):             # if you don't use the data, don't ask
        await sock.broadcast({"type": "typing"}, exclude_self=True)

    @ws("chat/<str:room>/", group="room:{room}")
    async def handler(sock, room):
        await chat.run(sock)

It is entirely optional: `async for msg in sock` is still there and you never
need to know this exists.
"""

from __future__ import annotations

import inspect
import logging

from .websocket import InvalidJSON

logger = logging.getLogger("django_socket")

ANY = "*"


class Events:
    """
    Dispatches JSON messages on one field (`"type"` by default).

    `key`     -- the field that decides the type. Use `Events(key="action")` if
                 your protocol calls it something else.
    `strict`  -- what to do with a type nobody handles. Ignored by default (and
                 noted in the log); with `strict=True` it closes with 4400.
    """

    def __init__(self, key: str = "type", strict: bool = False):
        self.key = key
        self.strict = strict
        self._handlers: dict[str, tuple] = {}

    def on(self, *types: str):
        """
        Register a handler for one or more types.

            @chat.on("message")
            @chat.on("join", "leave")       # several at once
            @chat.on("*")                   # whatever matched nothing else

        The handler may ask for the data or not:

            async def handler(sock, data)  -> data = the message minus `key`
            async def handler(sock)        -> knowing it arrived is enough
        """
        if not types:
            raise TypeError("@on() needs at least one type: @on('message')")

        def decorator(fn):
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(
                    f"@on expects 'async def', and {fn.__name__} is a plain "
                    f"function."
                )
            wants_data = _wants_data(fn)
            for kind in types:
                if kind in self._handlers:
                    previous = self._handlers[kind][0].__name__
                    raise ValueError(
                        f"Type {kind!r} is already handled by {previous}."
                    )
                self._handlers[kind] = (fn, wants_data)
            return fn

        return decorator

    @property
    def types(self) -> list[str]:
        return sorted(self._handlers)

    async def run(self, sock) -> None:
        """Consume messages until the client closes."""
        async for msg in sock:
            await self.handle(sock, msg.json())

    async def handle(self, sock, data) -> None:
        """Dispatch an already-parsed message. Handy for testing one handler."""
        if not isinstance(data, dict):
            raise InvalidJSON(data, f"expected an object with {self.key!r}")

        kind = data.get(self.key)
        entry = self._handlers.get(kind) or self._handlers.get(ANY)

        if entry is None:
            if self.strict:
                raise InvalidJSON(
                    data, f"unknown type {kind!r}; registered: {self.types}"
                )
            # WARNING on purpose: it is nearly always a typo in the type name,
            # and at DEBUG nobody would ever see it. If your protocol really
            # does send types you want to ignore, register an empty @on("*").
            logger.warning(
                "django_socket: nothing handles %s=%r on %s (registered: %s)",
                self.key, kind, sock.path, self.types or "none",
            )
            return

        handler, wants_data = entry
        if wants_data:
            await handler(sock, {k: v for k, v in data.items() if k != self.key})
        else:
            await handler(sock)


def _wants_data(fn) -> bool:
    """
    Whether the handler asks for the payload as well as the socket.

    Resolved once at registration time, not on every message.
    """
    params = [
        p for p in inspect.signature(fn).parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if len(params) == 1:
        return False
    if len(params) == 2:
        return True
    raise TypeError(
        f"{fn.__name__} must accept (sock) or (sock, data), not "
        f"{len(params)} positional parameters."
    )
