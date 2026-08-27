"""Explicit ASGI entry point.

You normally **need nothing from here**: adding "django_socket" to
INSTALLED_APPS is enough, and the `asgi.py` that `startproject` generated
serves WebSockets as-is (see `patch.py`).

`ASGIApplication` exists for anyone who would rather declare it by hand, to
compose with other ASGI middleware, or for anyone who set PATCH_ASGI=False.
"""

from __future__ import annotations

from . import dispatch


class ASGIApplication:
    """
    Explicit use in your `asgi.py`:

        from django_socket import ASGIApplication
        application = ASGIApplication()

    HTTP traffic goes to Django untouched; only the 'websocket' scope is
    intercepted (and 'lifespan', to start and stop the fan-out layer).
    """

    def __init__(self, http_app=None):
        if http_app is None:
            from django.core.asgi import get_asgi_application

            http_app = _wrap_static(get_asgi_application())  # runs django.setup()
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
    An ASGI app built on the fly, with nothing declared by the project.

    `runserver` uses it when there is no ASGI_APPLICATION in settings, so the
    library works straight after installing.
    """
    return ASGIApplication()


def _wrap_static(app):
    """In DEBUG serve /static/ like `runserver` does, without touching sockets."""
    from django.apps import apps
    from django.conf import settings

    if not settings.DEBUG or not apps.is_installed("django.contrib.staticfiles"):
        return app
    from django.contrib.staticfiles.handlers import ASGIStaticFilesHandler

    return ASGIStaticFilesHandler(app)
