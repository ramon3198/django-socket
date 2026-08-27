"""Autenticacion conectable.

Un autenticador es `async def (sock) -> user | None`. Se prueban en orden y
gana el primero que devuelva algo:

    DJANGO_SOCKET = {
        "AUTH": ["session", "token"],
        "TOKEN_RESOLVER": "miapp.auth.desde_jwt",
    }

o por ruta, cuando solo un endpoint lo necesita:

    @ws("feed/", auth="token")
    @ws("panel/", auth=["session", "token"])
    @ws("publico/", auth=False)          # ni lo intentes

Escribir el tuyo es una funcion:

    async def por_api_key(sock):
        clave = sock.query_params.get("k")
        return await Cliente.objects.filter(api_key=clave).afirst()

    DJANGO_SOCKET = {"AUTH": ["miapp.auth.por_api_key"]}
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("django_socket")

# Como llega el token, en orden de preferencia.
ESQUEMA = "bearer"


# --------------------------------------------------------------------- sesion


class _SessionCarrier:
    """Lo minimo que `django.contrib.auth.aget_user` espera de un request."""

    __slots__ = ("session",)

    def __init__(self, session):
        self.session = session


async def session(sock) -> Any | None:
    """
    La cookie de sesion de Django. Es el modo por defecto.

    Solo funciona si el navegador manda la cookie, o sea con el frontend
    servido desde el mismo sitio. Para un SPA en otro dominio o una app movil
    no hay cookie: usa `token`.
    """
    from importlib import import_module

    from django.apps import apps
    from django.conf import settings

    if not (
        apps.is_installed("django.contrib.auth")
        and apps.is_installed("django.contrib.sessions")
    ):
        return None

    engine = import_module(settings.SESSION_ENGINE)
    clave = sock.cookies.get(settings.SESSION_COOKIE_NAME)
    sock.session = engine.SessionStore(clave)

    try:
        from django.contrib.auth import aget_user
    except ImportError:  # Django < 5.0
        from asgiref.sync import sync_to_async
        from django.contrib.auth import get_user

        user = await sync_to_async(get_user)(_SessionCarrier(sock.session))
    else:
        user = await aget_user(_SessionCarrier(sock.session))

    return user if getattr(user, "is_authenticated", False) else None


# ---------------------------------------------------------------------- token


def extraer_token(sock) -> str | None:
    """
    Saca el token de donde el cliente haya podido ponerlo.

    Hay tres sitios porque **el navegador no puede fijar cabeceras** en un
    WebSocket: la API `new WebSocket(url, protocols)` solo deja tocar la URL y
    `Sec-WebSocket-Protocol`. Asi que:

    1. `Sec-WebSocket-Protocol: bearer, <token>` -- la via recomendada para
       navegadores. No se ve en la URL, luego no acaba en los logs.
    2. `Authorization: Bearer <token>` -- para clientes nativos, que si pueden
       poner cabeceras.
    3. `?token=<token>` -- funciona en todas partes, pero **queda escrito en
       los logs de acceso del servidor y de cualquier proxy por el que pase**.
       Usalo solo con tokens de un solo uso y vida corta.
    """
    protocolos = [p.strip() for p in sock.subprotocols]
    if len(protocolos) >= 2 and protocolos[0].lower() == ESQUEMA:
        return protocolos[1]

    cabecera = sock.headers.get("authorization", "")
    if cabecera.lower().startswith(ESQUEMA + " "):
        return cabecera[len(ESQUEMA) + 1:].strip()

    return sock.query_params.get("token")


async def token(sock) -> Any | None:
    """
    Un token, resuelto por la funcion que tu indiques.

    La libreria no sabe validar tu token -- puede ser un JWT, el de DRF, o algo
    tuyo -- asi que solo hace el transporte y te delega la parte que importa:

        DJANGO_SOCKET = {"TOKEN_RESOLVER": "miapp.auth.desde_jwt"}

        async def desde_jwt(token):
            datos = jwt.decode(token, KEY, algorithms=["HS256"])
            return await User.objects.filter(pk=datos["sub"]).afirst()

    Si no configuras `TOKEN_RESOLVER` y tienes `rest_framework.authtoken`
    instalado, se usa ese como atajo razonable.
    """
    crudo = extraer_token(sock)
    if not crudo:
        return None

    resolver = _get_token_resolver()
    if resolver is None:
        logger.warning(
            "django_socket: llego un token pero no hay quien lo valide. "
            "Define DJANGO_SOCKET['TOKEN_RESOLVER'] con una funcion "
            "async(token) -> user | None."
        )
        return None

    try:
        return await resolver(crudo)
    except Exception:
        # Un token invalido es lo normal, no un incidente: no ensucies el log
        # con una traza por cada intento.
        logger.debug("django_socket: el resolver rechazo el token", exc_info=True)
        return None


_resolver_cache: Callable | None = None
_resolver_resuelto = False


def _get_token_resolver() -> Callable | None:
    global _resolver_cache, _resolver_resuelto
    if _resolver_resuelto:
        return _resolver_cache

    from django.conf import settings
    from django.utils.module_loading import import_string

    conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
    ruta = conf.get("TOKEN_RESOLVER")

    if ruta:
        _resolver_cache = import_string(ruta) if isinstance(ruta, str) else ruta
    else:
        _resolver_cache = _resolver_drf()

    _resolver_resuelto = True
    return _resolver_cache


def _resolver_drf() -> Callable | None:
    """Atajo para quien ya use `rest_framework.authtoken`."""
    from django.apps import apps

    if not apps.is_installed("rest_framework.authtoken"):
        return None

    async def desde_drf(crudo):
        from rest_framework.authtoken.models import Token as DRFToken

        fila = await DRFToken.objects.select_related("user").filter(
            key=crudo
        ).afirst()
        return fila.user if fila else None

    return desde_drf


# ------------------------------------------------------------------ registro

INCORPORADOS: dict[str, Callable] = {"session": session, "token": token}


def resolver_lista(spec) -> list[Callable]:
    """Normaliza lo que venga en `auth=` o en settings a una lista de funciones."""
    from django.utils.module_loading import import_string

    if spec is True or spec is None:
        spec = _por_defecto()
    if isinstance(spec, (str, bytes)) or callable(spec):
        spec = [spec]

    salida = []
    for item in spec:
        if callable(item):
            salida.append(item)
        elif item in INCORPORADOS:
            salida.append(INCORPORADOS[item])
        else:
            try:
                salida.append(import_string(item))
            except ImportError as exc:
                raise ValueError(
                    f"Autenticador desconocido: {item!r}. Usa "
                    f"{sorted(INCORPORADOS)}, una ruta importable, o una "
                    f"funcion async(sock) -> user | None."
                ) from exc
    return salida


def _por_defecto():
    from django.conf import settings

    conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
    return conf.get("AUTH", ["session"])


async def resolve(sock, spec=True) -> None:
    """Rellena `sock.user` con el primer autenticador que reconozca a alguien."""
    from django.contrib.auth.models import AnonymousUser

    for autenticador in resolver_lista(spec):
        try:
            user = await autenticador(sock)
        except Exception:
            logger.exception(
                "django_socket: el autenticador %s fallo",
                getattr(autenticador, "__name__", autenticador),
            )
            continue
        if user is not None:
            sock.user = user
            return

    sock.user = AnonymousUser()


def _limpiar_cache_resolver() -> None:
    """Solo para tests: obliga a releer TOKEN_RESOLVER de settings."""
    global _resolver_cache, _resolver_resuelto
    _resolver_cache, _resolver_resuelto = None, False


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
