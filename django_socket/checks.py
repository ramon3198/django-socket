"""Warnings via `manage.py check`, so integration mistakes surface before a
deploy rather than as a socket that quietly does nothing.
"""

from __future__ import annotations

from django.core.checks import Error, Warning, register

W001 = "django_socket.W001"  # memory layer with several workers
W002 = "django_socket.W002"  # no routes registered
W003 = "django_socket.W003"  # origins open to everything
E001 = "django_socket.E001"  # unknown option in DJANGO_SOCKET

KNOWN_KEYS = {
    "LAYER",
    "REDIS_URL",
    "PREFIX",
    "ALLOWED_ORIGINS",
    "REQUIRE_ORIGIN",
    "PATCH_ASGI",
    "SEND_QUEUE_MAX",
    "SEND_QUEUE_FULL",
    "AUTH",
    "TOKEN_RESOLVER",
    "MIDDLEWARE",
    "RATE_LIMIT",
    "RATE_LIMIT_BURST",
}


@register()
def check_settings(app_configs, **kwargs):
    from django.conf import settings

    problems = []
    conf = getattr(settings, "DJANGO_SOCKET", {}) or {}

    unknown = set(conf) - KNOWN_KEYS
    if unknown:
        problems.append(
            Error(
                f"Unknown option(s) in DJANGO_SOCKET: {sorted(unknown)}.",
                hint=f"The valid ones are: {sorted(KNOWN_KEYS)}.",
                id=E001,
            )
        )

    allowed = conf.get("ALLOWED_ORIGINS")
    if allowed and "*" in allowed and not settings.DEBUG:
        problems.append(
            Warning(
                "DJANGO_SOCKET['ALLOWED_ORIGINS'] contains '*' with DEBUG=False.",
                hint=(
                    "Any website will be able to open a socket against yours carrying "
                    "your users' session cookies (cross-site WebSocket "
                    "hijacking). List the origins you trust."
                ),
                id=W003,
            )
        )

    return problems


@register()
def check_routes(app_configs, **kwargs):
    from . import routing

    if routing.get_routes():
        return []
    return [
        Warning(
            "django_socket is installed but there are no websocket routes.",
            hint=(
                "Create <your_app>/sockets.py and decorate an 'async def' with "
                "@ws('...'). It is auto-discovered, the same way admin.py is."
            ),
            id=W002,
        )
    ]


W004 = "django_socket.W004"  # token auth with no resolver configured


@register()
def check_auth(app_configs, **kwargs):
    """Warn about the combination that silently leaves everyone anonymous."""
    from django.conf import settings

    conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
    autenticadores = conf.get("AUTH", ["session"])
    usa_token = any(
        a == "token" or getattr(a, "__name__", "") == "token" for a in autenticadores
    )
    if usa_token and not conf.get("TOKEN_RESOLVER"):
        from django.apps import apps

        if not apps.is_installed("rest_framework.authtoken"):
            return [
                Warning(
                    "DJANGO_SOCKET['AUTH'] includes 'token' but there is no "
                    "TOKEN_RESOLVER.",
                    hint=(
                        "The library carries the token but cannot validate it. "
                        "Set TOKEN_RESOLVER to an async(token) -> user | None "
                        "function, or install rest_framework.authtoken. "
                        "Without it everyone comes in anonymous and it is not "
                        "obvious why."
                    ),
                    id=W004,
                )
            ]
    return []
