"""El camino explicito: ASGIApplication, el comando runserver y broadcast_sync."""

import pytest
from django.core.management import CommandError
from django.test import override_settings

from django_socket import ws
from django_socket.asgi import ASGIApplication


class HttpFalso:
    """Se hace pasar por la app HTTP de Django para ver que le llega."""

    def __init__(self):
        self.llamadas = []

    async def __call__(self, scope, receive, send):
        self.llamadas.append(scope["type"])


# --------------------------------------------------------- ASGIApplication


async def test_desvia_el_websocket_y_no_toca_el_http(transporte):
    @ws("explicito/", auth=False)
    async def handler(sock):
        await sock.send_text("por ASGIApplication")

    http = HttpFalso()
    app = ASGIApplication(http_app=http)

    t = transporte(path="/explicito/").cliente_conecta().cliente_cierra()
    await app(t.scope, t.receive, t.send)

    assert t.textos == ["por ASGIApplication"]
    assert http.llamadas == []          # el HTTP ni se entero


async def test_el_http_pasa_de_largo():
    http = HttpFalso()
    app = ASGIApplication(http_app=http)

    await app({"type": "http", "path": "/"}, None, None)

    assert http.llamadas == ["http"]


async def test_atiende_el_lifespan():
    eventos = []
    entrantes = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return entrantes.pop(0)

    async def send(msg):
        eventos.append(msg["type"])

    app = ASGIApplication(http_app=HttpFalso())
    await app({"type": "lifespan"}, receive, send)

    assert eventos == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


# ------------------------------------------------------------- runserver


def comando():
    from django_socket.management.commands.runserver import Command

    return Command()


@override_settings(ASGI_APPLICATION="miweb.asgi.application")
def test_runserver_respeta_asgi_application():
    ruta, es_factory, origen = comando()._import_string()
    assert ruta == "miweb.asgi:application"
    assert es_factory is False
    assert origen == "ASGI_APPLICATION"


def test_runserver_se_apana_sin_asgi_application():
    """Sin configurar nada, monta la app al vuelo: por eso instalar es un paso."""
    with override_settings():
        from django.conf import settings

        if hasattr(settings, "ASGI_APPLICATION"):
            del settings.ASGI_APPLICATION
        ruta, es_factory, origen = comando()._import_string()

    assert ruta == "django_socket.asgi:factory"
    assert es_factory is True
    assert "on the fly" in origen


@override_settings(ASGI_APPLICATION="sinpuntos")
def test_runserver_rechaza_un_asgi_application_invalido():
    with pytest.raises(CommandError) as exc:
        comando()._import_string()
    assert "myproject.asgi.application" in str(exc.value)    # dice como deberia ser


def test_la_factory_construye_una_app_valida(monkeypatch):
    from django_socket import asgi

    monkeypatch.setattr(asgi, "_wrap_static", lambda app: app)
    monkeypatch.setattr(
        "django.core.asgi.get_asgi_application", lambda: HttpFalso()
    )
    app = asgi.factory()
    assert isinstance(app, ASGIApplication)


# --------------------------------------------------------- broadcast_sync


def test_broadcast_sync_desde_codigo_sincrono(transporte):
    """
    Para vistas normales, señales o Celery. Se prueba en un test sincrono
    porque async_to_sync no puede llamarse desde un loop en marcha.
    """
    from asgiref.sync import async_to_sync

    from django_socket import broadcast_sync, groups
    from django_socket.websocket import WebSocket

    capa = groups.MemoryLayer()
    groups.set_layer(capa)

    t = transporte()
    sock = WebSocket(t.scope, t.receive, t.send, layer=capa)
    async_to_sync(sock.join)("avisos")

    broadcast_sync({"aviso": "reinicio"}, to="avisos")

    assert t.textos[-1] == '{"aviso": "reinicio"}'


def test_la_capa_base_obliga_a_implementar():
    from django_socket.groups import BaseLayer

    capa = BaseLayer()
    from asgiref.sync import async_to_sync

    for metodo, args in [
        ("add", ("g", None)),
        ("discard", ("g", None)),
        ("send", ("g", "x")),
        ("size", ("g",)),
    ]:
        with pytest.raises(NotImplementedError):
            async_to_sync(getattr(capa, metodo))(*args)


# ------------------------------------------------------------ static en DEBUG


@override_settings(DEBUG=True, STATIC_URL="/static/")
def test_en_debug_se_envuelve_para_servir_static(monkeypatch):
    """runserver sirve /static/; con uvicorn hay que hacerlo aqui."""
    from django.apps import apps

    from django_socket import asgi

    monkeypatch.setattr(apps, "is_installed", lambda n: n == "django.contrib.staticfiles")
    envuelta = asgi._wrap_static(HttpFalso())
    assert type(envuelta).__name__ == "ASGIStaticFilesHandler"


@override_settings(DEBUG=False)
def test_sin_debug_no_se_envuelve():
    from django_socket import asgi

    app = HttpFalso()
    assert asgi._wrap_static(app) is app
