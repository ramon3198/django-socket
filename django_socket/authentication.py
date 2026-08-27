"""Pluggable authentication.

An authenticator is `async def (sock) -> user | None`. They are tried in order
and the first one that returns something wins:

    DJANGO_SOCKET = {
        "AUTH": ["session", "token"],
        "TOKEN_RESOLVER": "myapp.auth.from_jwt",
    }

or per route, when only one endpoint needs it:

    @ws("feed/", auth="token")
    @ws("panel/", auth=["session", "token"])
    @ws("public/", auth=False)          # don't even try

Writing your own is just a function:

    async def by_api_key(sock):
        key = sock.query_params.get("k")
        return await Client.objects.filter(api_key=key).afirst()

    DJANGO_SOCKET = {"AUTH": ["myapp.auth.by_api_key"]}
"""

from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger("django_socket")

# How the token arrives, in order of preference.
SCHEME = "bearer"


# ------------------------------------------------------------------- session


class _SessionCarrier:
    """The minimum `django.contrib.auth.aget_user` expects from a request."""

    __slots__ = ("session",)

    def __init__(self, session):
        self.session = session


async def session(sock) -> Any | None:
    """
    Django's session cookie. This is the default.

    It only works when the browser sends the cookie, which means a same-site
    frontend. A SPA on another domain or a mobile app has no cookie: use
    `token`.
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
    key = sock.cookies.get(settings.SESSION_COOKIE_NAME)
    sock.session = engine.SessionStore(key)

    try:
        from django.contrib.auth import aget_user
    except ImportError:  # Django < 5.0
        from asgiref.sync import sync_to_async
        from django.contrib.auth import get_user

        user = await sync_to_async(get_user)(_SessionCarrier(sock.session))
    else:
        user = await aget_user(_SessionCarrier(sock.session))

    return user if getattr(user, "is_authenticated", False) else None


# --------------------------------------------------------------------- token


def extract_token(sock) -> str | None:
    """
    Pull the token from wherever the client was able to put it.

    There are three places because **a browser cannot set headers** on a
    WebSocket: the `new WebSocket(url, protocols)` API only lets you touch the
    URL and `Sec-WebSocket-Protocol`. So:

    1. `Sec-WebSocket-Protocol: bearer, <token>` -- the recommended route for
       browsers. Not in the URL, so it never reaches your access logs.
    2. `Authorization: Bearer <token>` -- for native clients, which can set
       headers.
    3. `?token=<token>` -- works everywhere, but **ends up written in the
       access logs of your server and of every proxy in between**. Use it only
       with short-lived, single-use tokens.
    """
    protos = [p.strip() for p in sock.subprotocols]
    if len(protos) >= 2 and protos[0].lower() == SCHEME:
        return protos[1]

    header = sock.headers.get("authorization", "")
    if header.lower().startswith(SCHEME + " "):
        return header[len(SCHEME) + 1:].strip()

    return sock.query_params.get("token")


async def token(sock) -> Any | None:
    """
    A token, validated by the function you point us at.

    The library cannot validate your token -- it might be a JWT, DRF's, or
    something of your own -- so it only handles the transport and delegates the
    part that matters:

        DJANGO_SOCKET = {"TOKEN_RESOLVER": "myapp.auth.from_jwt"}

        async def from_jwt(token):
            data = jwt.decode(token, KEY, algorithms=["HS256"])
            return await User.objects.filter(pk=data["sub"]).afirst()

    If you set no `TOKEN_RESOLVER` and have `rest_framework.authtoken`
    installed, that one is used as a reasonable shortcut.
    """
    raw = extract_token(sock)
    if not raw:
        return None

    resolver = _get_token_resolver()
    if resolver is None:
        logger.warning(
            "django_socket: a token arrived but nothing validates it. "
            "Set DJANGO_SOCKET['TOKEN_RESOLVER'] to an "
            "async(token) -> user | None function."
        )
        return None

    try:
        return await resolver(raw)
    except Exception:
        # An invalid token is normal, not an incident: don't fill the log with
        # a traceback on every attempt.
        logger.debug("django_socket: the resolver rejected the token", exc_info=True)
        return None


_resolver_cache: Callable | None = None
_resolver_resolved = False


def _get_token_resolver() -> Callable | None:
    global _resolver_cache, _resolver_resolved
    if _resolver_resolved:
        return _resolver_cache

    from django.conf import settings
    from django.utils.module_loading import import_string

    conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
    path = conf.get("TOKEN_RESOLVER")

    if path:
        _resolver_cache = import_string(path) if isinstance(path, str) else path
    else:
        _resolver_cache = _drf_resolver()

    _resolver_resolved = True
    return _resolver_cache


def _drf_resolver() -> Callable | None:
    """A shortcut for anyone already using `rest_framework.authtoken`."""
    from django.apps import apps

    if not apps.is_installed("rest_framework.authtoken"):
        return None

    async def from_drf(raw):
        from rest_framework.authtoken.models import Token as DRFToken

        row = await DRFToken.objects.select_related("user").filter(key=raw).afirst()
        return row.user if row else None

    return from_drf


# ------------------------------------------------------------------ registry

BUILTIN: dict[str, Callable] = {"session": session, "token": token}


def resolve_authenticators(spec) -> list[Callable]:
    """Normalize whatever `auth=` or settings hold into a list of functions."""
    from django.utils.module_loading import import_string

    if spec is True or spec is None:
        spec = _default_spec()
    if isinstance(spec, (str, bytes)) or callable(spec):
        spec = [spec]

    out = []
    for item in spec:
        if callable(item):
            out.append(item)
        elif item in BUILTIN:
            out.append(BUILTIN[item])
        else:
            try:
                out.append(import_string(item))
            except ImportError as exc:
                raise ValueError(
                    f"Unknown authenticator: {item!r}. Use one of "
                    f"{sorted(BUILTIN)}, an importable path, or an "
                    f"async(sock) -> user | None function."
                ) from exc
    return out


def _default_spec():
    from django.conf import settings

    conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
    return conf.get("AUTH", ["session"])


async def resolve(sock, spec=True) -> None:
    """Fill `sock.user` with the first authenticator that recognises anyone."""
    from django.contrib.auth.models import AnonymousUser

    for authenticator in resolve_authenticators(spec):
        try:
            user = await authenticator(sock)
        except Exception:
            logger.exception(
                "django_socket: authenticator %s failed",
                getattr(authenticator, "__name__", authenticator),
            )
            continue
        if user is not None:
            sock.user = user
            return

    sock.user = AnonymousUser()


def _clear_resolver_cache() -> None:
    """Tests only: force TOKEN_RESOLVER to be read from settings again."""
    global _resolver_cache, _resolver_resolved
    _resolver_cache, _resolver_resolved = None, False


def login_required(handler):
    """
    Close the connection with 4401 when the user is not authenticated.

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


# --------------------------------------------------------- 0.2.x compatibility
# `extraer_token` was exported from `django_socket` in 0.2.x. Kept so upgrading
# does not break anyone; removed at 1.0.
extraer_token = extract_token
