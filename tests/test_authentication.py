"""Autenticacion conectable: token, autenticadores propios, y el orden."""

import pytest
from django.test import override_settings

from django_socket import dispatch, extraer_token, ws
from django_socket.authentication import _limpiar_cache_resolver, resolver_lista
from django_socket.testing import WebSocketClient


@pytest.fixture(autouse=True)
def resolver_limpio():
    _limpiar_cache_resolver()
    yield
    _limpiar_cache_resolver()


async def correr(t):
    await dispatch.handle_websocket(t.scope, t.receive, t.send)
    return t


def sock_falso(transporte, **kwargs):
    from django_socket import groups
    from django_socket.websocket import WebSocket

    t = transporte(**kwargs)
    return WebSocket(t.scope, t.receive, t.send, layer=groups.get_layer())


# ------------------------------------------------- de donde se saca el token


def test_del_subprotocolo(transporte):
    """La via recomendada en navegador: no se ve en la URL ni en los logs."""
    sock = sock_falso(transporte, subprotocols=["bearer", "abc123"])
    assert extraer_token(sock) == "abc123"


def test_de_la_cabecera_authorization(transporte):
    """Para clientes nativos, que si pueden poner cabeceras."""
    sock = sock_falso(transporte, headers={"Authorization": "Bearer abc123"})
    assert extraer_token(sock) == "abc123"


def test_el_esquema_no_distingue_mayusculas(transporte):
    assert extraer_token(sock_falso(transporte, headers={"Authorization": "bearer x"})) == "x"
    assert extraer_token(sock_falso(transporte, subprotocols=["Bearer", "y"])) == "y"


def test_de_la_query(transporte):
    sock = sock_falso(transporte, query="token=abc123")
    assert extraer_token(sock) == "abc123"


def test_sin_token_devuelve_none(transporte):
    assert extraer_token(sock_falso(transporte)) is None


def test_el_subprotocolo_gana_a_la_query(transporte):
    """Preferimos la via que no deja el token escrito en los logs."""
    sock = sock_falso(
        transporte, subprotocols=["bearer", "del-protocolo"], query="token=de-la-query"
    )
    assert extraer_token(sock) == "del-protocolo"


def test_un_subprotocolo_normal_no_se_confunde_con_un_token(transporte):
    sock = sock_falso(transporte, subprotocols=["graphql-ws"])
    assert extraer_token(sock) is None


# ------------------------------------------------------- resolver del token


async def resolver_de_prueba(crudo):
    from django.contrib.auth.models import User

    if crudo == "token-bueno":
        return await User.objects.filter(username="ramon").afirst()
    return None


@pytest.mark.django_db(transaction=True)
@override_settings(
    DJANGO_SOCKET={
        "AUTH": ["token"],
        "TOKEN_RESOLVER": "tests.test_authentication.resolver_de_prueba",
    }
)
async def test_un_token_valido_autentica(crear_usuario):
    await crear_usuario("ramon")

    @ws("feed/")
    async def feed(sock):
        await sock.send_json({"quien": sock.user.username, "auth": True})

    async with WebSocketClient("/feed/", subprotocols=["bearer", "token-bueno"]) as c:
        assert await c.receive_json() == {"quien": "ramon", "auth": True}


@pytest.mark.django_db(transaction=True)
@override_settings(
    DJANGO_SOCKET={
        "AUTH": ["token"],
        "TOKEN_RESOLVER": "tests.test_authentication.resolver_de_prueba",
    }
)
async def test_un_token_invalido_deja_anonimo():
    @ws("feed/")
    async def feed(sock):
        await sock.send_json({"auth": sock.user.is_authenticated})

    async with WebSocketClient("/feed/", query="token=basura") as c:
        assert await c.receive_json() == {"auth": False}


async def resolver_que_revienta(crudo):
    raise ValueError("firma invalida")


@override_settings(
    DJANGO_SOCKET={
        "AUTH": ["token"],
        "TOKEN_RESOLVER": "tests.test_authentication.resolver_que_revienta",
    }
)
async def test_un_resolver_que_lanza_no_tumba_la_conexion(caplog):
    """
    Un token corrupto es lo normal, no un incidente.

    Debe quedar anonimo y sin traza en el log: si cada intento fallido dejara
    un traceback, un escaneo automatico te llenaria el disco.
    """
    @ws("feed/")
    async def feed(sock):
        await sock.send_json({"auth": sock.user.is_authenticated})

    async with WebSocketClient("/feed/", query="token=corrupto") as c:
        assert await c.receive_json() == {"auth": False}
    assert "Traceback" not in caplog.text


@override_settings(DJANGO_SOCKET={"AUTH": ["token"]})
async def test_sin_resolver_configurado_avisa(caplog):
    """Llega un token y no hay quien lo valide: dilo, no lo ignores."""
    @ws("feed/")
    async def feed(sock):
        await sock.send_json({"auth": sock.user.is_authenticated})

    async with WebSocketClient("/feed/", query="token=algo") as c:
        assert await c.receive_json() == {"auth": False}
    assert "TOKEN_RESOLVER" in caplog.text


# --------------------------------------------------- orden y autenticadores


@pytest.mark.django_db(transaction=True)
@override_settings(
    DJANGO_SOCKET={
        "AUTH": ["session", "token"],
        "TOKEN_RESOLVER": "tests.test_authentication.resolver_de_prueba",
    }
)
async def test_se_prueban_en_orden_y_gana_el_primero(crear_usuario):
    """Sin cookie, la sesion no reconoce a nadie y el token toma el relevo."""
    await crear_usuario("ramon")

    @ws("feed/")
    async def feed(sock):
        await sock.send_json({"quien": str(sock.user)})

    async with WebSocketClient("/feed/", query="token=token-bueno") as c:
        assert await c.receive_json() == {"quien": "ramon"}


async def por_cabecera_propia(sock):
    """Un autenticador escrito por el usuario de la libreria."""
    class Falso:
        username = "de-cabecera"
        is_authenticated = True
        pk = 99

    return Falso() if sock.headers.get("x-mi-clave") == "secreta" else None


@override_settings(DJANGO_SOCKET={"AUTH": ["tests.test_authentication.por_cabecera_propia"]})
async def test_un_autenticador_propio_por_ruta_de_import():
    @ws("feed/")
    async def feed(sock):
        await sock.send_json({"quien": sock.user.username})

    async with WebSocketClient("/feed/", headers={"X-Mi-Clave": "secreta"}) as c:
        assert await c.receive_json() == {"quien": "de-cabecera"}


async def test_un_autenticador_como_funcion_en_la_ruta():
    @ws("feed/", auth=por_cabecera_propia)
    async def feed(sock):
        await sock.send_json({"quien": sock.user.username})

    async with WebSocketClient("/feed/", headers={"X-Mi-Clave": "secreta"}) as c:
        assert await c.receive_json() == {"quien": "de-cabecera"}


async def test_la_ruta_manda_sobre_los_settings():
    with override_settings(DJANGO_SOCKET={"AUTH": ["session"]}):
        @ws("feed/", auth=por_cabecera_propia)
        async def feed(sock):
            await sock.send_json({"quien": sock.user.username})

        async with WebSocketClient("/feed/", headers={"X-Mi-Clave": "secreta"}) as c:
            assert await c.receive_json() == {"quien": "de-cabecera"}


async def test_uno_que_falla_no_impide_que_pruebe_el_siguiente(caplog):
    async def revienta(sock):
        raise RuntimeError("la BD no responde")

    @ws("feed/", auth=[revienta, por_cabecera_propia])
    async def feed(sock):
        await sock.send_json({"quien": sock.user.username})

    async with WebSocketClient("/feed/", headers={"X-Mi-Clave": "secreta"}) as c:
        assert await c.receive_json() == {"quien": "de-cabecera"}
    assert "la BD no responde" in caplog.text


# ------------------------------------------------------------- validaciones


def test_un_autenticador_inventado_falla_al_registrar():
    """Falla al importar el modulo, no en la primera conexion de un usuario."""
    with pytest.raises(ValueError) as exc:
        @ws("x/", auth="no-existe-esto")
        async def handler(sock): ...

    mensaje = str(exc.value)
    assert "session" in mensaje and "token" in mensaje


def test_resolver_lista_normaliza_las_formas():
    assert len(resolver_lista("token")) == 1
    assert len(resolver_lista(["session", "token"])) == 2
    assert len(resolver_lista(por_cabecera_propia)) == 1


@override_settings(DJANGO_SOCKET={"AUTH": ["session", "token"]})
def test_true_significa_lo_que_diga_settings():
    assert len(resolver_lista(True)) == 2
