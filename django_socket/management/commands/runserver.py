"""`manage.py runserver` sobre uvicorn, para que los WebSockets funcionen en dev.

El runserver de Django es WSGI puro y rechaza el scope 'websocket'. Este
comando reutiliza el parseo de argumentos de Django (addrport, --ipv6,
--noreload) y arranca uvicorn contra tu ASGI_APPLICATION.
"""

from __future__ import annotations

from django.conf import settings
from django.core.management import CommandError
from django.core.management.commands.runserver import Command as RunserverCommand


class Command(RunserverCommand):
    help = "Arranca un servidor de desarrollo ASGI (uvicorn) con soporte WebSocket."

    def add_arguments(self, parser):
        super().add_arguments(parser)
        parser.add_argument(
            "--log-level",
            default="info",
            help="Nivel de log de uvicorn (error, warning, info, debug, trace).",
        )

    def run(self, **options):
        try:
            import uvicorn
        except ImportError as exc:
            raise CommandError(
                "django_socket necesita uvicorn para el servidor de desarrollo.\n"
                "  pip install 'uvicorn[standard]'"
            ) from exc

        app_path, is_factory, origen = self._import_string()

        from ... import routing

        rutas = routing.get_routes()
        self.stdout.write(
            self.style.SUCCESS(
                f"django_socket sobre uvicorn -- http://{self.addr}:{self.port}/"
            )
        )
        self.stdout.write(
            f"  {len(rutas)} ruta(s) websocket"
            + (": " + ", ".join(f"/{r.route}" for r in rutas) if rutas else "")
        )
        self.stdout.write(f"  app: {app_path}  ({origen})")
        self.stdout.write("Ctrl-C para salir.\n")

        uvicorn.run(
            app_path,
            factory=is_factory,
            host=self.addr,
            port=int(self.port),
            reload=options["use_reloader"],
            log_level=options["log_level"],
            # Django ya loguea las peticiones cuando DEBUG esta activo.
            access_log=True,
        )

    def _import_string(self) -> tuple[str, bool, str]:
        """
        Devuelve (ruta_de_importacion, es_factory, de_donde_sale).

        Si el proyecto declara ASGI_APPLICATION la respetamos; si no, montamos
        una al vuelo, para que la libreria funcione recien instalada sin pedir
        ni una linea de configuracion.
        """
        path = getattr(settings, "ASGI_APPLICATION", None)
        if not path:
            return "django_socket.asgi:factory", True, "generada al vuelo"

        module, _, attr = path.rpartition(".")
        if not module:
            raise CommandError(
                f"ASGI_APPLICATION invalido: {path!r}. Deberia ser algo como "
                f'"miproyecto.asgi.application".'
            )
        return f"{module}:{attr}", False, "ASGI_APPLICATION"
