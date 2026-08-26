"""`manage.py ws` -- que rutas hay y como esta montado todo."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from ... import patch, routing


class Command(BaseCommand):
    help = "Lista las rutas WebSocket registradas y revisa la integracion."

    def handle(self, *args, **options):
        conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
        routes = routing.get_routes()

        self.stdout.write(self.style.MIGRATE_HEADING("Rutas WebSocket"))
        if not routes:
            self.stdout.write(
                "  ninguna. Crea <tu_app>/sockets.py y decora un 'async def' con @ws()."
            )
        for r in routes:
            flags = []
            if r.group:
                flags.append(f"group={r.group}")
            if not r.auth:
                flags.append("auth=False")
            suffix = f"  [{', '.join(flags)}]" if flags else ""
            where = f"{r.handler.__module__}.{r.handler.__name__}"
            self.stdout.write(f"  ws:///{r.route}".ljust(42) + f"{where}{suffix}")

        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("Integracion"))
        self._row("Capa de difusion", conf.get("LAYER", "memory"))
        if conf.get("LAYER") == "redis":
            self._row("Redis", conf.get("REDIS_URL", "redis://localhost:6379/0"))
        self._row(
            "asgi.py",
            "no hace falta tocarlo (ASGIHandler ampliado)"
            if patch.is_installed()
            else "PATCH_ASGI=False -> debes usar ASGIApplication() a mano",
        )
        self._row(
            "Origenes permitidos",
            conf.get("ALLOWED_ORIGINS")
            or f"ALLOWED_HOSTS={list(settings.ALLOWED_HOSTS) or '[] (DEBUG: localhost)'}",
        )
        self._row(
            "Origin ausente",
            "rechazado" if conf.get("REQUIRE_ORIGIN") else "aceptado (clientes nativos)",
        )

        if settings.DEBUG and conf.get("LAYER", "memory") == "memory":
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "  Aviso: con la capa 'memory' un broadcast no cruza entre\n"
                    "  procesos. En produccion con varios workers usa LAYER='redis'."
                )
            )

    def _row(self, label: str, value) -> None:
        self.stdout.write(f"  {label:<22}{value}")
