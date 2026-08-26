"""
ASGI config for demo project.

Generado por `django-admin startproject` y NO modificado: django_socket
amplia ASGIHandler desde su AppConfig.ready(), asi que esto sirve WebSockets
tal cual.
"""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")

application = get_asgi_application()
