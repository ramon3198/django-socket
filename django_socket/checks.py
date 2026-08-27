"""Avisos via `manage.py check`, para que los fallos de integracion salgan
antes de desplegar y no como un socket que calla.
"""

from __future__ import annotations

from django.core.checks import Error, Warning, register

W001 = "django_socket.W001"  # capa memory con varios workers
W002 = "django_socket.W002"  # ninguna ruta registrada
W003 = "django_socket.W003"  # origenes abiertos a todo
E001 = "django_socket.E001"  # opcion desconocida en DJANGO_SOCKET

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
                f"Opcion(es) desconocida(s) en DJANGO_SOCKET: {sorted(unknown)}.",
                hint=f"Las validas son: {sorted(KNOWN_KEYS)}.",
                id=E001,
            )
        )

    allowed = conf.get("ALLOWED_ORIGINS")
    if allowed and "*" in allowed and not settings.DEBUG:
        problems.append(
            Warning(
                "DJANGO_SOCKET['ALLOWED_ORIGINS'] contiene '*' con DEBUG=False.",
                hint=(
                    "Cualquier web podra abrir un socket contra la tuya con las "
                    "cookies de sesion de tus usuarios (cross-site WebSocket "
                    "hijacking). Enumera los origenes que confias."
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
            "django_socket esta instalado pero no hay ninguna ruta websocket.",
            hint=(
                "Crea <tu_app>/sockets.py y decora un 'async def' con @ws('...'). "
                "Se autodescubre igual que admin.py."
            ),
            id=W002,
        )
    ]


W004 = "django_socket.W004"  # token por query sin resolver configurado


@register()
def check_auth(app_configs, **kwargs):
    """Avisa de la combinacion que deja a todo el mundo anonimo en silencio."""
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
                    "DJANGO_SOCKET['AUTH'] incluye 'token' pero no hay "
                    "TOKEN_RESOLVER.",
                    hint=(
                        "La libreria transporta el token pero no sabe validarlo. "
                        "Define TOKEN_RESOLVER con una funcion "
                        "async(token) -> user | None, o instala "
                        "rest_framework.authtoken. Sin eso, todo el mundo "
                        "entra como anonimo y no es evidente por que."
                    ),
                    id=W004,
                )
            ]
    return []
