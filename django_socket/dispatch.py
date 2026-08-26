"""El nucleo: atiende los scopes 'websocket' y 'lifespan'.

Vive aparte de `asgi.py` porque hay dos caminos que llegan aqui: el parche
sobre `ASGIHandler` (modo cero-configuracion) y `ASGIApplication` explicito.
Los dos comparten este codigo.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from asgiref.sync import ThreadSensitiveContext

from . import auth as auth_mod
from . import groups, routing
from .websocket import InvalidJSON, WebSocket, WebSocketDisconnect

logger = logging.getLogger("django_socket")

# Codigos de cierre propios (rango privado 4000-4999).
CLOSE_NO_ROUTE = 4404
CLOSE_BAD_DATA = 4400      # el cliente mando algo que no se puede parsear
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
                logger.exception("django_socket: fallo en el arranque")
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
    # El primer evento del protocolo siempre es websocket.connect.
    event = await receive()
    if event["type"] != "websocket.connect":
        return

    await _start_layer()  # por si el servidor no soporta lifespan
    sock = WebSocket(scope, receive, send, layer=groups.get_layer())

    if not origin_allowed(sock):
        logger.warning(
            "django_socket: origen rechazado %r para %s",
            sock.headers.get("origin"),
            sock.path,
        )
        # En seco: un origen ajeno no debe tener un socket abierto ni un instante.
        await sock.deny()
        return

    match = routing.resolve(sock.path)
    if match is None:
        logger.warning(
            "django_socket: ninguna ruta casa con %s. Registradas: %s",
            sock.path,
            ", ".join(f"/{r.route}" for r in routing.get_routes()) or "(ninguna)",
        )
        await sock.close(CLOSE_NO_ROUTE, "No route")
        return

    route, kwargs = match
    sock.path_params = kwargs

    async with ThreadSensitiveContext():
        if route.auth:
            await auth_mod.resolve(sock)

        try:
            if route.group:
                # group="room:{room}" se rellena con los parametros de la ruta.
                await sock.join(route.group.format(**kwargs))
            await route.handler(sock, **kwargs)
        except WebSocketDisconnect:
            pass  # el cliente se fue; salida normal
        except InvalidJSON as exc:
            # Culpa del cliente, no del servidor: un aviso y un codigo que lo
            # diga. Nada de traceback ni de 1011, que harian pensar que el bug
            # es tuyo cada vez que alguien mande basura por el socket.
            logger.warning(
                "django_socket: %s en %s (cliente %s)", exc, sock.path, sock.client
            )
            await sock.close(CLOSE_BAD_DATA, "Invalid JSON")
            return
        except Exception:
            logger.exception("django_socket: excepcion en el handler de %s", sock.path)
            await sock.close(CLOSE_SERVER_ERROR, "Internal error")
            return
        await sock.close()


# ------------------------------------------------------------------ origen


def origin_allowed(sock) -> bool:
    """
    Los WebSockets no estan sujetos a la politica de mismo origen: sin esta
    comprobacion cualquier web podria abrir un socket autenticado contra la
    tuya (cross-site WebSocket hijacking).

    Un Origin ausente se acepta: los navegadores siempre lo mandan, asi que
    solo lo omiten clientes nativos. Ponlo estricto con REQUIRE_ORIGIN.
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

    # Por defecto: ALLOWED_HOSTS + CSRF_TRUSTED_ORIGINS, como hace Django.
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
    """'https://ejemplo.com:8000' -> 'ejemplo.com'."""
    try:
        return (urlparse(origin).hostname or "").lower()
    except ValueError:
        return ""


def _host_matches(host: str, pattern: str) -> bool:
    pattern = pattern.lower()
    if pattern == "*":
        return True
    if pattern.startswith("."):  # ".ejemplo.com" cubre subdominios y el apex
        return host == pattern[1:] or host.endswith(pattern)
    if pattern.startswith("*."):
        return host == pattern[2:] or host.endswith(pattern[1:])
    return host == pattern
