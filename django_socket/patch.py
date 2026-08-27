"""Makes the `asgi.py` that `startproject` generates serve WebSockets untouched.

`django.core.asgi.get_asgi_application()` returns an `ASGIHandler` that rejects
any scope that is not 'http' -- Django's own code carries a
`# FIXME: Allow to override this.` right there. Since `django.setup()` runs the
apps' `ready()` *before* instantiating the handler, our `ready()` gets there in
time to widen that door.

The result is that integrating the library takes two steps: install it and add
it to INSTALLED_APPS. Turn it off with DJANGO_SOCKET = {"PATCH_ASGI": False} if
you would rather declare `ASGIApplication()` by hand.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("django_socket")

FLAG = "_django_socket_patched"


def install() -> bool:
    """True if the patch is installed (or already was)."""
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
    logger.debug("django_socket: ASGIHandler widened with websocket + lifespan")
    return True


def is_installed() -> bool:
    from django.core.handlers.asgi import ASGIHandler

    return getattr(ASGIHandler, FLAG, False)
