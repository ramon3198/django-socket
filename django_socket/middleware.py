"""Middleware: envuelve cada conexion, para lo que hay que hacer en todas.

Trazas, metricas, reportar errores a Sentry, limitar conexiones por usuario.
Un middleware es `async def (sock, siguiente)`, y `siguiente()` corre el resto
de la cadena y al final tu handler:

    # miapp/ws.py
    import time, logging

    log = logging.getLogger("miapp.sockets")

    async def medir(sock, siguiente):
        inicio = time.monotonic()
        try:
            await siguiente()
        finally:
            log.info("%s duro %.1fs", sock.path, time.monotonic() - inicio)

    # settings.py
    DJANGO_SOCKET = {"MIDDLEWARE": ["miapp.ws.medir"]}

Se aplican en orden: el primero de la lista es el mas externo, igual que el
MIDDLEWARE de Django.

Para cortar una conexion, cierra y no llames a `siguiente()`:

    async def solo_de_pago(sock, siguiente):
        if not await es_de_pago(sock.user):
            await sock.close(4403, "Plan insuficiente")
            return
        await siguiente()
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

logger = logging.getLogger("django_socket")

Siguiente = Callable[[], Awaitable[None]]
Middleware = Callable[..., Awaitable[None]]

_cadena: list[Middleware] | None = None


def get_middleware() -> list[Middleware]:
    """Lee y cachea la lista de settings. Se resuelve una vez, no por conexion."""
    global _cadena
    if _cadena is None:
        from django.conf import settings
        from django.utils.module_loading import import_string

        conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
        _cadena = [
            import_string(m) if isinstance(m, str) else m
            for m in conf.get("MIDDLEWARE", [])
        ]
        if _cadena:
            logger.debug(
                "django_socket: %d middleware(s): %s",
                len(_cadena),
                ", ".join(getattr(m, "__name__", str(m)) for m in _cadena),
            )
    return _cadena


def limpiar_cache() -> None:
    """Solo para tests: obliga a releer MIDDLEWARE de settings."""
    global _cadena
    _cadena = None


async def aplicar(sock, handler_final: Siguiente) -> None:
    """
    Corre la cadena y, al final, el handler.

    Se monta de dentro hacia fuera para que el primero de la lista quede el mas
    externo, que es lo que la gente espera al leer un MIDDLEWARE de Django.
    """
    cadena = get_middleware()
    if not cadena:
        await handler_final()
        return

    siguiente = handler_final
    for mw in reversed(cadena):
        siguiente = _envolver(mw, sock, siguiente)
    await siguiente()


def _envolver(mw: Middleware, sock, siguiente: Siguiente) -> Siguiente:
    async def llamada() -> None:
        await mw(sock, siguiente)

    llamada.__name__ = getattr(mw, "__name__", "middleware")
    return llamada


# ------------------------------------------------------- middlewares utiles


def max_conexiones_por_usuario(limite: int = 5, code: int = 4429):
    """
    Corta al usuario que abre mas de `limite` sockets a la vez.

        DJANGO_SOCKET = {
            "MIDDLEWARE": [max_conexiones_por_usuario(10)],
        }

    Cuenta por proceso. Con varios workers el limite real es
    `limite x workers`; para un tope global harian falta contadores en Redis, y
    eso vale la pena solo si de verdad te hace falta esa precision.
    """
    from collections import defaultdict

    abiertas: dict[object, int] = defaultdict(int)

    async def middleware(sock, siguiente):
        user = getattr(sock, "user", None)
        autenticado = getattr(user, "is_authenticated", False)
        clave = getattr(user, "pk", None) if autenticado else None
        if clave is None:
            clave = (sock.client or ("?", 0))[0]      # anonimos, por IP

        if abiertas[clave] >= limite:
            logger.warning(
                "django_socket: %r supero %d conexiones simultaneas en %s",
                clave, limite, sock.path,
            )
            await sock.close(code, "Too many connections")
            return

        abiertas[clave] += 1
        try:
            await siguiente()
        finally:
            abiertas[clave] -= 1
            if abiertas[clave] <= 0:
                abiertas.pop(clave, None)

    middleware.__name__ = "max_conexiones_por_usuario"
    return middleware


def registrar(nivel: int = logging.INFO, logger_name: str = "django_socket.access"):
    """Una linea por conexion: ruta, usuario, duracion y como termino."""
    import time

    log = logging.getLogger(logger_name)

    async def middleware(sock, siguiente):
        inicio = time.monotonic()
        try:
            await siguiente()
        finally:
            log.log(
                nivel,
                "%s user=%s dur=%.2fs code=%s",
                sock.path,
                getattr(sock.user, "pk", None) or "anon",
                time.monotonic() - inicio,
                sock.close_code,
            )

    middleware.__name__ = "registrar"
    return middleware
