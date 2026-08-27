import logging

from django.apps import AppConfig

logger = logging.getLogger("django_socket")


class DjangoSocketConfig(AppConfig):
    name = "django_socket"
    label = "django_socket"
    verbose_name = "Django Socket"

    def ready(self):
        from django.conf import settings
        from django.utils.module_loading import autodiscover_modules

        from . import (
            checks,  # noqa: F401  (registered on import)
            patch,
            routing,
        )

        # Import <each_app>/sockets.py, the same way admin does with admin.py.
        autodiscover_modules("sockets")

        conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
        if conf.get("PATCH_ASGI", True):
            patch.install()

        found = routing.get_routes()
        logger.debug(
            "django_socket: %d path(s): %s",
            len(found),
            ", ".join(f"/{r.route}" for r in found) or "(none)",
        )
