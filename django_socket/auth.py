"""Sesion y usuario de Django a partir de las cookies del handshake.

Todo es async de verdad: Django 5+ expone `aget_user` y `SessionStore.aget`,
asi que no hace falta pasar por un thread pool.
"""

from __future__ import annotations

import logging
from importlib import import_module

logger = logging.getLogger("django_socket")


class _SessionCarrier:
    """Lo minimo que `django.contrib.auth.aget_user` espera de un request."""

    __slots__ = ("session",)

    def __init__(self, session):
        self.session = session


def _auth_installed() -> bool:
    from django.apps import apps

    return apps.is_installed("django.contrib.auth") and apps.is_installed(
        "django.contrib.sessions"
    )


async def resolve(sock) -> None:
    """Rellena `sock.session` y `sock.user`. Nunca lanza."""
    from django.conf import settings

    if not _auth_installed():
        return

    engine = import_module(settings.SESSION_ENGINE)
    session_key = sock.cookies.get(settings.SESSION_COOKIE_NAME)
    session = engine.SessionStore(session_key)
    sock.session = session

    carrier = _SessionCarrier(session)
    try:
        try:
            from django.contrib.auth import aget_user
        except ImportError:
            # Django < 5.0 no tiene ni aget_user ni SessionStore.aget: al hilo.
            from asgiref.sync import sync_to_async
            from django.contrib.auth import get_user

            sock.user = await sync_to_async(get_user)(carrier)
        else:
            sock.user = await aget_user(carrier)
    except Exception:
        logger.exception("django_socket: fallo al resolver el usuario")
        from django.contrib.auth.models import AnonymousUser

        sock.user = AnonymousUser()


def login_required(handler):
    """
    Cierra la conexion con 4401 si el usuario no esta autenticado.

        @ws("panel/")
        @login_required
        async def panel(sock): ...
    """
    import functools

    @functools.wraps(handler)
    async def wrapper(sock, *args, **kwargs):
        user = getattr(sock, "user", None)
        if user is None or not user.is_authenticated:
            await sock.close(4401, "Authentication required")
            return
        return await handler(sock, *args, **kwargs)

    return wrapper
