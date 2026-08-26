"""Punto de entrada ASGI explicito.

Normalmente **no necesitas nada de aqui**: basta con añadir "django_socket" a
INSTALLED_APPS y el `asgi.py` que genero `startproject` sirve WebSockets tal
cual (ver `patch.py`).

`ASGIApplication` existe para quien prefiere declararlo a mano, para componer
con otro middleware ASGI, o para quien puso PATCH_ASGI=False.
"""

from __future__ import annotations

from . import dispatch


class ASGIApplication:
    """
    Uso explicito en tu `asgi.py`:

        from django_socket import ASGIApplication
        application = ASGIApplication()

    El trafico HTTP va a Django sin tocarse; solo se intercepta el scope
    'websocket' (y 'lifespan', para arrancar y parar la capa de difusion).
    """

    def __init__(self, http_app=None):
        if http_app is None:
            from django.core.asgi import get_asgi_application

            http_app = _wrap_static(get_asgi_application())  # hace django.setup()
        self.http_app = http_app

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "websocket":
            return await dispatch.handle_websocket(scope, receive, send)
        if kind == "lifespan":
            return await dispatch.handle_lifespan(scope, receive, send)
        return await self.http_app(scope, receive, send)


def factory():
    """
    App ASGI construida al vuelo, sin que el proyecto declare nada.

    La usa `runserver` cuando no hay ASGI_APPLICATION en settings, para que la
    libreria funcione recien instalada.
    """
    return ASGIApplication()


def _wrap_static(app):
    """En DEBUG sirve /static/ igual que hace `runserver`, sin tocar websockets."""
    from django.apps import apps
    from django.conf import settings

    if not settings.DEBUG or not apps.is_installed("django.contrib.staticfiles"):
        return app
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

    return ASGIStaticFilesHandler(app)
