"""Hace que el `asgi.py` que genera `startproject` sirva WebSockets sin tocarlo.

`django.core.asgi.get_asgi_application()` devuelve un `ASGIHandler` que rechaza
todo scope que no sea 'http' -- el propio codigo de Django lleva ahi un
`# FIXME: Allow to override this.`. Como `django.setup()` ejecuta los `ready()`
de las apps *antes* de instanciar el handler, desde nuestro `ready()` llegamos
a tiempo de ensanchar esa puerta.

El resultado es que integrar la libreria son dos pasos: instalarla y añadirla a
INSTALLED_APPS. Desactivalo con DJANGO_SOCKET = {"PATCH_ASGI": False} si
prefieres declarar `ASGIApplication()` a mano.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("django_socket")

FLAG = "_django_socket_patched"


def install() -> bool:
    """Devuelve True si el parche quedo instalado (o ya lo estaba)."""
    from django.core.handlers.asgi import ASGIHandler

    if getattr(ASGIHandler, FLAG, False):
        return True

    original_call = ASGIHandler.__call__

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "websocket":
            from . import dispatch

            return await dispatch.handle_websocket(scope, receive, send)
        if kind == "lifespan":
            from . import dispatch

            return await dispatch.handle_lifespan(scope, receive, send)
        return await original_call(self, scope, receive, send)

    __call__.__doc__ = ASGIHandler.__call__.__doc__
    ASGIHandler.__call__ = __call__
    setattr(ASGIHandler, FLAG, True)
    logger.debug("django_socket: ASGIHandler ampliado con websocket + lifespan")
    return True


def is_installed() -> bool:
    from django.core.handlers.asgi import ASGIHandler

    return getattr(ASGIHandler, FLAG, False)
