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

    `auth` decide como se resuelve `sock.user`:

        auth=True                  los autenticadores de settings (por defecto,
                                   la sesion de Django)
        auth=False                 ninguno; `sock.user` queda a None
        auth="token"               solo por token
        auth=["session", "token"]  el primero que reconozca a alguien
        auth=mi_funcion            async(sock) -> user | None

    Ver `django_socket.authentication`.

    `rate_limit` acota los mensajes entrantes de cada socket ("60/m", "10/s").
    Al pasarse se cierra con 4429. `burst` deja pasar picos mayores sin subir
    el ritmo sostenido. Ver `django_socket.ratelimit`.
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
        if auth is not False:
            # Falla al importar si el autenticador no existe, no en la primera
            # conexion del primer usuario.
            from .authentication import resolver_lista

            resolver_lista(auth)
        if rate_limit is not None:
            from .ratelimit import parsear

            parsear(rate_limit)     # revienta ahora si el formato esta mal

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
                rate_limit=rate_limit,
                burst=burst,
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
