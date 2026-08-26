"""Registro y resolucion de rutas WebSocket."""

from __future__ import annotations

import inspect
import string
from typing import Any, Callable, NamedTuple

from django.urls.resolvers import RoutePattern


class Route(NamedTuple):
    pattern: RoutePattern
    handler: Callable
    name: str
    auth: bool
    group: str | None
    route: str


_routes: list[Route] = []


def ws(
    route: str,
    *,
    group: str | None = None,
    auth: bool = True,
    name: str | None = None,
):
    """
    Registra un handler WebSocket.

        @ws("chat/<str:room>/", group="room:{room}")
        async def chat(sock, room):
            async for msg in sock:
                await sock.broadcast(msg.text)

    `route` usa la sintaxis de `django.urls.path` y sus mismos conversores
    (`<int:pk>`, `<slug:x>`, `<uuid:x>`...), asi que los parametros llegan al
    handler ya convertidos.

    `group` se rellena con esos mismos parametros: el socket entra en el grupo
    al conectar, sale al desconectar, y `sock.broadcast(dato)` va ahi por
    defecto.

    `auth=False` salta la resolucion de sesion y usuario si el endpoint es
    publico (una consulta menos por conexion).
    """

    def decorator(handler: Callable) -> Callable:
        if not inspect.iscoroutinefunction(handler):
            raise TypeError(
                f"@ws espera 'async def', y {handler.__name__} es una funcion "
                f"normal.\n"
                f"    async def {handler.__name__}(sock, ...):\n"
                f"Un WebSocket vive en el loop de eventos. Para tocar el ORM "
                f"usa su API async (await Model.objects.aget(...)) o envuelve "
                f"lo sincrono en asgiref.sync.sync_to_async."
            )
        normalized = route.lstrip("/")
        _check_group_template(group, normalized, handler)

        for existing in _routes:
            if existing.route == normalized:
                raise ValueError(
                    f"La ruta '{normalized}' ya la tiene registrada "
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
            )
        )
        return handler

    return decorator


def _check_group_template(group: str | None, route: str, handler: Callable) -> None:
    """Falla al importar, no en la primera conexion, si el grupo no cuadra."""
    if not group:
        return
    referenced = {
        field for _, field, _, _ in string.Formatter().parse(group) if field
    }
    available = set(RoutePattern(route, is_endpoint=True).regex.groupindex)
    missing = referenced - available
    if missing:
        raise ValueError(
            f"group={group!r} en {handler.__name__} usa "
            f"{sorted(missing)}, que no existe(n) en la ruta '{route}'. "
            f"Disponibles: {sorted(available) or 'ninguno'}."
        )


def resolve(path: str) -> tuple[Route, dict[str, Any]] | None:
    """Devuelve (ruta, kwargs) para un path ASGI, o None si no casa ninguna."""
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
    """Solo para tests."""
    _routes.clear()
