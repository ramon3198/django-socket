"""La promesa de instalacion: el parche ASGI, los checks y el comando `ws`."""

import io

import pytest
from django.core.management import call_command
from django.test import override_settings

from django_socket import patch, ws


# ------------------------------------------------------------------- parche


def test_el_parche_esta_puesto_al_arrancar_la_app():
    """Se instala en DjangoSocketConfig.ready(), sin que el proyecto haga nada."""
    assert patch.is_installed()


def test_el_parche_es_idempotente():
    from django.core.handlers.asgi import ASGIHandler

    antes = ASGIHandler.__call__
    patch.install()
    assert ASGIHandler.__call__ is antes


async def test_el_handler_de_django_ahora_acepta_websocket(transporte):
    """
    El corazon de la promesa: `get_asgi_application()` sin tocar sirve sockets.
    """
    from django.core.handlers.asgi import ASGIHandler

    @ws("parcheado/", auth=False)
    async def handler(sock):
        await sock.send_text("por la puerta de Django")

    app = ASGIHandler()
    t = transporte(path="/parcheado/").cliente_conecta().cliente_cierra()
    await app(t.scope, t.receive, t.send)

    assert t.textos == ["por la puerta de Django"]


async def test_el_http_sigue_yendo_a_django():
    """Ampliar la puerta no debe desviar el trafico normal."""
    from django.core.handlers.asgi import ASGIHandler

    import asyncio

    recibido = []
    cuerpo_enviado = False

    async def receive():
        # El cuerpo una vez; despues Django escucha una desconexion que no
        # llegara, y su task group cancela esta espera al responder.
        nonlocal cuerpo_enviado
        if not cuerpo_enviado:
            cuerpo_enviado = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Event().wait()

    async def send(msg):
        recibido.append(msg)

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [(b"host", b"testserver")],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 1),
        "scheme": "http",
        "http_version": "1.1",
        "root_path": "",
    }
    with override_settings(ROOT_URLCONF="tests.urls"):
        await ASGIHandler()(scope, receive, send)

    assert recibido[0]["type"] == "http.response.start"
    assert recibido[0]["status"] == 200


async def test_lifespan_pasa_por_nosotros():
    from django.core.handlers.asgi import ASGIHandler

    eventos = []
    entrantes = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return entrantes.pop(0)

    async def send(msg):
        eventos.append(msg["type"])

    # Sin el parche esto reventaria con ValueError.
    await ASGIHandler()({"type": "lifespan"}, receive, send)
    assert eventos == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


# ------------------------------------------------------------------- checks


def test_check_avisa_si_no_hay_rutas():
    from django_socket.checks import check_routes

    problemas = check_routes(None)
    assert [p.id for p in problemas] == ["django_socket.W002"]
    assert "sockets.py" in problemas[0].hint


def test_check_calla_si_hay_rutas():
    from django_socket.checks import check_routes

    @ws("algo/")
    async def handler(sock): ...

    assert check_routes(None) == []


@override_settings(DJANGO_SOCKET={"LAYERR": "memory"})
def test_check_caza_una_clave_mal_escrita():
    from django_socket.checks import check_settings

    problemas = check_settings(None)
    assert [p.id for p in problemas] == ["django_socket.E001"]
    assert "LAYERR" in problemas[0].msg


@override_settings(DEBUG=False, DJANGO_SOCKET={"ALLOWED_ORIGINS": ["*"]})
def test_check_avisa_del_comodin_en_produccion():
    from django_socket.checks import check_settings

    ids = [p.id for p in check_settings(None)]
    assert "django_socket.W003" in ids


@override_settings(DEBUG=True, DJANGO_SOCKET={"ALLOWED_ORIGINS": ["*"]})
def test_el_comodin_en_debug_no_molesta():
    from django_socket.checks import check_settings

    assert check_settings(None) == []


# --------------------------------------------------------------- comando ws


def test_comando_ws_lista_las_rutas():
    @ws("chat/<str:room>/", group="room:{room}")
    async def chat(sock, room): ...

    @ws("publico/", auth=False)
    async def publico(sock): ...

    salida = io.StringIO()
    call_command("ws", stdout=salida)
    texto = salida.getvalue()

    assert "ws:///chat/<str:room>/" in texto
    assert "group=room:{room}" in texto
    assert "auth=False" in texto
    assert "no hace falta tocarlo" in texto     # estado del parche


def test_comando_ws_sin_rutas_orienta():
    salida = io.StringIO()
    call_command("ws", stdout=salida)
    assert "sockets.py" in salida.getvalue()


# ------------------------------------------------------------ capa por config


@override_settings(DJANGO_SOCKET={"LAYER": "redis", "REDIS_URL": "redis://x:1/0"})
def test_la_capa_sale_de_settings():
    from django_socket import groups

    groups.set_layer(None)
    capa = groups.get_layer()
    assert isinstance(capa, groups.RedisLayer)
    assert capa.url == "redis://x:1/0"


@override_settings(DJANGO_SOCKET={"LAYER": "inventada"})
def test_capa_desconocida_explica_las_validas():
    from django_socket import groups

    groups.set_layer(None)
    with pytest.raises(ValueError) as exc:
        groups.get_layer()
    assert "memory" in str(exc.value) and "redis" in str(exc.value)


def test_se_puede_enchufar_una_capa_propia():
    from django_socket import groups

    class Mia(groups.BaseLayer):
        pass

    with override_settings(DJANGO_SOCKET={"LAYER": Mia}):
        groups.set_layer(None)
        assert isinstance(groups.get_layer(), Mia)


# ------------------------------------------------------------ cliente JS


def render(plantilla: str) -> str:
    from django.template import Context, Template

    return Template(plantilla).render(Context({}))


def test_ws_client_pinta_la_etiqueta_script():
    salida = render("{% load django_socket %}{% ws_client %}")
    assert salida == '<script src="/static/django_socket/client.js"></script>'


def test_ws_client_admite_defer():
    salida = render("{% load django_socket %}{% ws_client defer=True %}")
    assert " defer></script>" in salida


def test_ws_client_url_para_meterlo_en_tu_bundle():
    assert render("{% load django_socket %}{% ws_client_url %}") == (
        "/static/django_socket/client.js"
    )


def test_el_js_va_dentro_del_paquete():
    """Si no se empaqueta, `{% ws_client %}` da un 404 en produccion."""
    from pathlib import Path

    import django_socket

    js = Path(django_socket.__file__).parent / "static" / "django_socket" / "client.js"
    assert js.is_file()
    fuente = js.read_text(encoding="utf-8")
    assert "global.djangoSocket" in fuente
    assert "esDefinitivo" in fuente         # la logica de no-reintentar
