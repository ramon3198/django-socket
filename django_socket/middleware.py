"""Middleware: wraps every connection, for whatever has to happen on all of them.

Tracing, metrics, error reporting, connection limits. A middleware is
`async def (sock, next_)`, and `next_()` runs the rest of the chain and finally
your handler:

    # myapp/ws.py
    import time, logging

    log = logging.getLogger("myapp.sockets")

    async def measure(sock, next_):
        start = time.monotonic()
        try:
            await next_()
        finally:
            log.info("%s took %.1fs", sock.path, time.monotonic() - start)

    # settings.py
    DJANGO_SOCKET = {"MIDDLEWARE": ["myapp.ws.measure"]}

They are applied in order: the first in the list is the outermost, the same as
Django's own MIDDLEWARE.

To reject a connection, close it and don't call `next_()`:

    async def paid_only(sock, next_):
        if not await is_paid(sock.user):
            await sock.close(4403, "Plan required")
            return
        await next_()
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger("django_socket")

NextHandler = Callable[[], Awaitable[None]]
Middleware = Callable[..., Awaitable[None]]

_chain: list[Middleware] | None = None


def get_middleware() -> list[Middleware]:
    """Read and cache the list from settings. Resolved once, not per connection."""
    global _chain
    if _chain is None:
        from django.conf import settings
        from django.utils.module_loading import import_string

        conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
        _chain = [
            import_string(m) if isinstance(m, str) else m
            for m in conf.get("MIDDLEWARE", [])
        ]
        if _chain:
            logger.debug(
                "django_socket: %d middleware(s): %s",
                len(_chain),
                ", ".join(getattr(m, "__name__", str(m)) for m in _chain),
            )
    return _chain


def clear_cache() -> None:
    """Tests only: force MIDDLEWARE to be read from settings again."""
    global _chain
    _chain = None


async def apply(sock, final_handler: NextHandler) -> None:
    """
    Run the chain and, at the end, the handler.

    Built from the inside out so the first entry in the list ends up outermost,
    which is what people expect after reading a Django MIDDLEWARE setting.
    """
    chain = get_middleware()
    if not chain:
        await final_handler()
        return

    next_ = final_handler
    for mw in reversed(chain):
        next_ = _wrap(mw, sock, next_)
    await next_()


def _wrap(mw: Middleware, sock, next_: NextHandler) -> NextHandler:
    async def call() -> None:
        await mw(sock, next_)

    call.__name__ = getattr(mw, "__name__", "middleware")
    return call


# ---------------------------------------------------------- useful middleware


def max_connections_per_user(limit: int = 5, code: int = 4429):
    """
    Cut off a user who opens more than `limit` sockets at once.

        DJANGO_SOCKET = {
            "MIDDLEWARE": [max_connections_per_user(10)],
        }

    Counted per process. With several workers the real ceiling is
    `limit x workers`; a global cap would need counters in Redis, and that is
    only worth it if you genuinely need that precision.

    Behind a reverse proxy, anonymous visitors all share the proxy's IP and
    therefore one bucket. Until `X-Forwarded-For` support lands, raise the
    limit or key it off something you control.
    """
    from collections import defaultdict

    open_count: dict[object, int] = defaultdict(int)

    async def middleware(sock, next_):
        user = getattr(sock, "user", None)
        is_auth = getattr(user, "is_authenticated", False)
        key = getattr(user, "pk", None) if is_auth else None
        if key is None:
            key = (sock.client or ("?", 0))[0]      # anonymous, by IP

        if open_count[key] >= limit:
            logger.warning(
                "django_socket: %r went over %d simultaneous connections on %s",
                key, limit, sock.path,
            )
            await sock.close(code, "Too many connections")
            return

        open_count[key] += 1
        try:
            await next_()
        finally:
            open_count[key] -= 1
            if open_count[key] <= 0:
                open_count.pop(key, None)

    middleware.__name__ = "max_connections_per_user"
    return middleware


def log_connections(
    level: int = logging.INFO, logger_name: str = "django_socket.access"
):
    """One line per connection: path, user, duration and how it ended."""
    import time

    log = logging.getLogger(logger_name)

    async def middleware(sock, next_):
        start = time.monotonic()
        try:
            await next_()
        finally:
            log.log(
                level,
                "%s user=%s dur=%.2fs code=%s",
                sock.path,
                getattr(sock.user, "pk", None) or "anon",
                time.monotonic() - start,
                sock.close_code,
            )

    middleware.__name__ = "log_connections"
    return middleware


# --------------------------------------------------------- 0.2.x compatibility
# These names shipped in 0.2.x and were documented in the README. They keep
# working so a 0.2.x project does not break on upgrade, and go away at 1.0.
max_conexiones_por_usuario = max_connections_per_user
registrar = log_connections
