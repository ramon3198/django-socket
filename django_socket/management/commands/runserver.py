"""`manage.py runserver` on uvicorn, so WebSockets work in development.

Django's runserver is pure WSGI and rejects the 'websocket' scope. This command
reuses Django's own argument parsing (addrport, --ipv6, --noreload) and starts
uvicorn against your ASGI_APPLICATION.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management import CommandError
from django.core.management.commands.runserver import Command as RunserverCommand


class Command(RunserverCommand):
    help = "Run an ASGI development server (uvicorn) with WebSocket support."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--log-level",
            default="info",
            help="uvicorn log level (error, warning, info, debug, trace).",
        )

    def run(self, **options):
        try:
            import uvicorn
        except ImportError as exc:
            raise CommandError(
                "django_socket needs uvicorn for the development server.\n"
                "  pip install 'uvicorn[standard]'"
            ) from exc

        app_path, is_factory, source = self._import_string()

        from ... import routing

        routes = routing.get_routes()
        self.stdout.write(
            self.style.SUCCESS(
                f"django_socket on uvicorn -- http://{self.addr}:{self.port}/"
            )
        )
        self.stdout.write(
            f"  {len(routes)} websocket path(s)"
            + (": " + ", ".join(f"/{r.route}" for r in routes) if routes else "")
        )
        self.stdout.write(f"  app: {app_path}  ({source})")
        self.stdout.write("Ctrl-C to quit.\n")

        uvicorn.run(
            app_path,
            factory=is_factory,
            host=self.addr,
            port=int(self.port),
            reload=options["use_reloader"],
            log_level=options["log_level"],
            # Django already logs requests when DEBUG is on.
            access_log=True,
        )

    def _import_string(self) -> tuple[str, bool, str]:
        """
        Return (import_path, is_factory, where_it_came_from).

        If the project declares ASGI_APPLICATION we honour it; if not, we
        build one on the fly, so the library works right after install
        without asking for a single line of configuration.
        """
        path = getattr(settings, "ASGI_APPLICATION", None)
        if not path:
            return "django_socket.asgi:factory", True, "built on the fly"

        module, _, attr = path.rpartition(".")
        if not module:
            raise CommandError(
                f"Invalid ASGI_APPLICATION: {path!r}. It should look like "
                f'"myproject.asgi.application".'
            )
        return f"{module}:{attr}", False, "ASGI_APPLICATION"
