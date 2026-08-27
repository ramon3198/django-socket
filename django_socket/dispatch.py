"""The core: handles the 'websocket' and 'lifespan' scopes.

It lives apart from `asgi.py` because two paths lead here: the patch on
`ASGIHandler` (the zero-configuration mode) and an explicit `ASGIApplication`.
Both share this code.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from asgiref.sync import ThreadSensitiveContext

from . import authentication, groups, routing
from . import middleware as mw
from .websocket import InvalidJSON, RateLimited, WebSocket, WebSocketDisconnect

logger = logging.getLogger("django_socket")

# Our own close codes (private range 4000-4999).
CLOSE_NO_ROUTE = 4404
CLOSE_BAD_DATA = 4400      # the client sent something unparseable
CLOSE_SERVER_ERROR = 1011

_layer_started = False


def _settings():
    from django.conf import settings

    return getattr(settings, "DJANGO_SOCKET", {}) or {}


async def _start_layer() -> None:
    global _layer_started
    if not _layer_started:
        await groups.get_layer().startup()
        _layer_started = True


async def _stop_layer() -> None:
    global _layer_started
    if _layer_started:
        await groups.get_layer().shutdown()
        _layer_started = False


# ---------------------------------------------------------------- lifespan


async def handle_lifespan(scope, receive, send) -> None:
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            try:
                await _start_layer()
            except Exception as exc:
                logger.exception("django_socket: startup failed")
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
                return
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            try:
                await _stop_layer()
            finally:
                await send({"type": "lifespan.shutdown.complete"})
            return


# --------------------------------------------------------------- websocket


async def handle_websocket(scope, receive, send) -> None:
    # The protocol's first event is always websocket.connect.
    event = await receive()
    if event["type"] != "websocket.connect":
        return

    await _start_layer()  # in case the server has no lifespan support
    sock = WebSocket(scope, receive, send, layer=groups.get_layer())

    if not origin_allowed(sock):
        logger.warning(
            "django_socket: rejected origin %r for %s",
            sock.headers.get("origin"),
            sock.path,
        )
        # Flat out: a foreign origin must not hold an open socket for an instant.
        await sock.deny()
        return

    match = routing.resolve(sock.path)
    if match is None:
        logger.warning(
            "django_socket: no route matches %s. Registered: %s",
            sock.path,
            ", ".join(f"/{r.route}" for r in routing.get_routes()) or "(none)",
        )
        await sock.close(CLOSE_NO_ROUTE, "No route")
        return

    route, kwargs = match
    sock.path_params = kwargs

    async with ThreadSensitiveContext():
        if route.auth is not False:
            await authentication.resolve(sock, route.auth)

        from . import ratelimit

        sock._rate = ratelimit.make_bucket(route.rate_limit, route.burst)

        async def run_handler():
            if route.group:
                # group="room:{room}" is filled from the route parameters.
                await sock.join(route.group.format(**kwargs))
            await route.handler(sock, **kwargs)

        try:
            # Middleware sits outside the handler but inside this try, so a
            # failure of its own is treated the same as one of the handler's.
            await mw.apply(sock, run_handler)
        except WebSocketDisconnect:
            pass  # the client left; normal exit
        except RateLimited as exc:
            logger.warning(
                "django_socket: %s is going too fast on %s (%s)",
                sock.client, sock.path, exc,
            )
            await sock.close(exc.code, f"Rate limit; retry in {exc.retry_after:.0f}s")
            return
        except InvalidJSON as exc:
            # The client's fault, not the server's: a warning and a code that
            # says so. No traceback and no 1011, which would suggest the bug
            # is yours every time someone sends garbage down the socket.
            logger.warning(
                "django_socket: %s on %s (client %s)", exc, sock.path, sock.client
            )
            await sock.close(CLOSE_BAD_DATA, "Invalid JSON")
            return
        except Exception:
            logger.exception(
                "django_socket: exception in the handler for %s", sock.path
            )
            await sock.close(CLOSE_SERVER_ERROR, "Internal error")
            return
        await sock.close()


# ------------------------------------------------------------------ origin


def origin_allowed(sock) -> bool:
    """
    WebSockets are not subject to the same-origin policy: without this check
    any website could open an authenticated socket against yours (cross-site
    WebSocket hijacking).

    A missing Origin is accepted: browsers always send it, so only native
    clients omit it. Make it strict with REQUIRE_ORIGIN.
    """
    from django.conf import settings

    conf = _settings()
    origin = sock.headers.get("origin")
    if origin is None:
        return not conf.get("REQUIRE_ORIGIN", False)

    allowed = conf.get("ALLOWED_ORIGINS")
    if allowed is not None:
        if "*" in allowed:
            return True
        return origin in allowed or _host_of(origin) in allowed

    # By default: ALLOWED_HOSTS + CSRF_TRUSTED_ORIGINS, like Django does.
    host = _host_of(origin)
    if not host:
        return False

    trusted = {
        _host_of(o) for o in getattr(settings, "CSRF_TRUSTED_ORIGINS", []) or []
    }
    if host in trusted:
        return True

    hosts = list(getattr(settings, "ALLOWED_HOSTS", []) or [])
    if settings.DEBUG and not hosts:
        hosts = ["localhost", "127.0.0.1", "[::1]"]
    return any(_host_matches(host, pattern) for pattern in hosts)


def _host_of(origin: str) -> str:
    """'https://example.com:8000' -> 'example.com'."""
    try:
        return (urlparse(origin).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.lower()
    if pattern == "*":
        return True
    if pattern.startswith("."):  # ".example.com" covers subdomains and the apex
        return host == pattern[1:] or host.endswith(pattern)
    if pattern.startswith("*."):
        return host == pattern[2:] or host.endswith(pattern[1:])
    return host == pattern
