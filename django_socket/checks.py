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
