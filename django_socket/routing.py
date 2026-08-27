"""Route registry and resolution for WebSocket endpoints."""

from __future__ import annotations

import inspect
import string
from typing import Any, Callable, NamedTuple

from django.urls.resolvers import RoutePattern


class Route(NamedTuple):
    pattern: RoutePattern
    handler: Callable
    name: str
    auth: Any
    group: str | None
    route: str
    rate_limit: str | None
    burst: float | None


_routes: list[Route] = []


def ws(
    route: str,
    *,
    group: str | None = None,
    auth: Any = True,
    rate_limit: str | None = None,
    burst: float | None = None,
    name: str | None = None,
):
    """
    Register a WebSocket handler.

        @ws("chat/<str:room>/", group="room:{room}")
        async def chat(sock, room):
            async for msg in sock:
                await sock.broadcast(msg.text)

    `route` uses `django.urls.path` syntax and its converters (`<int:pk>`,
    `<slug:x>`, `<uuid:x>`...), so parameters reach the handler already
    converted.

    `group` is filled in from those same parameters: the socket joins on
    connect, leaves on disconnect, and `sock.broadcast(data)` goes there by
    default.

    `auth` decides how `sock.user` is resolved:

        auth=True                  the authenticators from settings (by
                                   default, the Django session)
        auth=False                 none; `sock.user` stays None
        auth="token"               token only
        auth=["session", "token"]  the first one that recognises anybody
        auth=my_function           async(sock) -> user | None

    See `django_socket.authentication`.

    `rate_limit` caps incoming messages per socket ("60/m", "10/s"). Going
    over closes with 4429. `burst` lets spikes through without raising the
    sustained rate. See `django_socket.ratelimit`.
    """

    def decorator(handler: Callable) -> Callable:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(
                f"@ws expects 'async def', and {handler.__name__} is a plain "
                f"function.\n"
                f"    async def {handler.__name__}(sock, ...):\n"
                f"A WebSocket lives on the event loop. For the ORM use its "
                f"async API (await Model.objects.aget(...)) or wrap the sync "
                f"call in asgiref.sync.sync_to_async."
            )
        normalized = route.lstrip("/")
        _check_group_template(group, normalized, handler)
        if auth is not False:
            # Fail at import time if the authenticator does not exist, not on
            # the first user's first connection.
            from .authentication import resolve_authenticators

            resolve_authenticators(auth)
        if rate_limit is not None:
            from .ratelimit import parse_rate

            parse_rate(rate_limit)     # blow up now if the format is wrong

        for existing in _routes:
            if existing.route == normalized:
                raise ValueError(
                    f"Route '{normalized}' is already registered by "
                    f"{existing.handler.__module__}.{existing.handler.__name__}."
                )

        _routes.append(
            Route(
                pattern=RoutePattern(normalized, is_endpoint=True),
                handler=handler,
                name=name or handler.__name__,
                auth=auth,
                group=group,
                route=normalized,
                rate_limit=rate_limit,
                burst=burst,
            )
        )
        return handler

    return decorator


def _check_group_template(group: str | None, route: str, handler: Callable) -> None:
    """Fail at import time, not on the first connection, if the group is wrong."""
    if not group:
        return
    referenced = {
        field for _, field, _, _ in string.Formatter().parse(group) if field
    }
    available = set(RoutePattern(route, is_endpoint=True).regex.groupindex)
    missing = referenced - available
    if missing:
        raise ValueError(
            f"group={group!r} on {handler.__name__} uses "
            f"{sorted(missing)}, which route '{route}' does not have. "
            f"Available: {sorted(available) or 'none'}."
        )


def resolve(path: str) -> tuple[Route, dict[str, Any]] | None:
    """Return (route, kwargs) for an ASGI path, or None if nothing matches."""
    candidate = path.lstrip("/")
    for r in _routes:
        match = r.pattern.match(candidate)
        if match is not None:
            _, _, kwargs = match
            return r, kwargs
    return None


def get_routes() -> list[Route]:
    return list(_routes)


def clear_routes() -> None:
    """Tests only."""
    _routes.clear()
