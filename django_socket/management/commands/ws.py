"""`manage.py ws` -- which routes exist and how everything is wired."""

from __future__ import annotations

from django.conf import settings
from django.core.management.base import BaseCommand

from ... import patch, routing


class Command(BaseCommand):
    help = "List the registered WebSocket routes and check the integration."

    def handle(self, *args, **options):
        conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
        routes = routing.get_routes()

        self.stdout.write(self.style.MIGRATE_HEADING("WebSocket routes"))
        if not routes:
            self.stdout.write(
                "  none. Create <your_app>/sockets.py and decorate an "
                "'async def' with @ws()."
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
        self.stdout.write(self.style.MIGRATE_HEADING("Integration"))
        self._row("Broadcast layer", conf.get("LAYER", "memory"))
        if conf.get("LAYER") == "redis":
            self._row("Redis", conf.get("REDIS_URL", "redis://localhost:6379/0"))
        self._row(
            "asgi.py",
            "nothing to do there (ASGIHandler widened)"
            if patch.is_installed()
            else "PATCH_ASGI=False -> use ASGIApplication() by hand",
        )
        self._row(
            "Allowed origins",
            conf.get("ALLOWED_ORIGINS")
            or f"ALLOWED_HOSTS={list(settings.ALLOWED_HOSTS) or '[] (DEBUG)'}",
        )
        self._row(
            "Missing Origin",
            "rejected" if conf.get("REQUIRE_ORIGIN") else "accepted (native clients)",
        )

        if settings.DEBUG and conf.get("LAYER", "memory") == "memory":
            self.stdout.write("")
            self.stdout.write(
                self.style.WARNING(
                    "  Warning: with the 'memory' layer a broadcast does not cross\n"
                    "  processes. In production with several workers use LAYER='redis'."
                )
            )

    def _row(self, label: str, value) -> None:
        self.stdout.write(f"  {label:<22}{value}")
