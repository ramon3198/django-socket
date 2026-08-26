"""El objeto WebSocket: handshake, entrada, salida, cierre."""

import pytest

from django_socket.websocket import (
    Message,
    WebSocket,
    WebSocketClosed,
    WebSocketDisconnect,
)


def nuevo(transporte, **kwargs):
    from django_socket import groups

    t = transporte(**kwargs)
    return WebSocket(t.scope, t.receive, t.send, layer=groups.get_layer()), t


# ------------------------------------------------------------------ handshake


async def test_accept_implicito_al_enviar(transporte):
    """No hace falta llamar a accept(): enviar lo dispara."""
    sock, t = nuevo(transporte)
    await sock.send_text("hola")
    assert t.tipos == ["websocket.accept", "websocket.send"]
    assert t.enviados[1] == {"type": "websocket.send", "text": "hola"}


async def test_accept_implicito_al_recibir(transporte):
    sock, t = nuevo(transporte)
    t.cliente_envia("ping")
    assert (await sock.receive()).text == "ping"
    assert t.acepto


async def test_accept_explicito_no_se_repite(transporte):
    sock, t = nuevo(transporte)
    await sock.accept(subprotocol="graphql-ws")
    await sock.accept()
    await sock.send_text("x")
    assert t.tipos.count("websocket.accept") == 1
    assert t.enviados[0]["subprotocol"] == "graphql-ws"


async def test_accept_con_headers(transporte):
    sock, t = nuevo(transporte)
    await sock.accept(headers=[("x-servidor", "django_socket")])
    assert t.enviados[0]["headers"] == [(b"x-servidor", b"django_socket")]


# --------------------------------------------------------------------- cierre


async def test_close_acepta_antes_para_entregar_el_codigo(transporte):
    """
    Cerrar sin aceptar deja al navegador con un 1006 sin motivo; aceptando
    primero, el codigo llega intacto al onclose del cliente.
    """
    sock, t = nuevo(transporte)
    await sock.close(4401, "falta login")
    assert t.tipos == ["websocket.accept", "websocket.close"]
    assert t.cierre == {
        "type": "websocket.close",
        "code": 4401,
        "reason": "falta login",
    }


async def test_deny_no_acepta(transporte):
    """deny() tumba el handshake: el cliente ve un 403 y no hay socket."""
    sock, t = nuevo(transporte)
    await sock.deny()
    assert not t.acepto
    assert t.cierre["code"] == 403


async def test_close_es_idempotente(transporte):
    sock, t = nuevo(transporte)
    await sock.close(1000)
    await sock.close(4000)
    assert t.tipos.count("websocket.close") == 1


async def test_usar_socket_cerrado_avisa(transporte):
    sock, _ = nuevo(transporte)
    await sock.close()
    with pytest.raises(WebSocketClosed):
        await sock.send_text("tarde")


# -------------------------------------------------------------------- entrada


async def test_desconexion_lanza_con_codigo(transporte):
    sock, t = nuevo(transporte)
    t.cliente_cierra(code=1001)
    with pytest.raises(WebSocketDisconnect) as exc:
        await sock.receive()
    assert exc.value.code == 1001
    assert sock.close_code == 1001


async def test_iteracion_termina_al_desconectar(transporte):
    sock, t = nuevo(transporte)
    t.cliente_envia("uno").cliente_envia("dos").cliente_cierra()
    recibidos = [m.text async for m in sock]
    assert recibidos == ["uno", "dos"]


async def test_receive_text_rechaza_binario(transporte):
    sock, t = nuevo(transporte)
    t.cliente_envia(datos=b"\x00\x01")
    with pytest.raises(TypeError):
        await sock.receive_text()


async def test_receive_json(transporte):
    sock, t = nuevo(transporte)
    t.cliente_envia('{"a": 1}')
    assert await sock.receive_json() == {"a": 1}


async def test_iter_text_ignora_binarios(transporte):
    sock, t = nuevo(transporte)
    t.cliente_envia("a").cliente_envia(datos=b"bin").cliente_envia("b").cliente_cierra()
    assert [x async for x in sock.iter_text()] == ["a", "b"]


# --------------------------------------------------------------------- salida


@pytest.mark.parametrize(
    "dato, esperado",
    [
        ("texto", {"type": "websocket.send", "text": "texto"}),
        (b"bin", {"type": "websocket.send", "bytes": b"bin"}),
        ({"a": 1}, {"type": "websocket.send", "text": '{"a": 1}'}),
        ([1, 2], {"type": "websocket.send", "text": "[1, 2]"}),
    ],
)
async def test_send_elige_el_frame_por_el_tipo(transporte, dato, esperado):
    sock, t = nuevo(transporte)
    await sock.send(dato)
    assert t.enviados[-1] == esperado


async def test_send_json_serializa_lo_no_serializable(transporte):
    """default=str evita que un Decimal o un datetime tumben la conexion."""
    from decimal import Decimal

    sock, t = nuevo(transporte)
    await sock.send({"precio": Decimal("9.99")})
    assert t.textos[-1] == '{"precio": "9.99"}'


# ------------------------------------------------------------------ contexto


async def test_query_params(transporte):
    sock, _ = nuevo(transporte, query="token=abc&n=3&n=4")
    assert sock.query_params == {"token": "abc", "n": "3"}
    assert sock.query_lists["n"] == ["3", "4"]


async def test_cookies_y_headers(transporte):
    sock, _ = nuevo(
        transporte, headers={"Cookie": "sessionid=xyz; otra=1", "X-Raro": "si"}
    )
    assert sock.cookies == {"sessionid": "xyz", "otra": "1"}
    assert sock.headers["x-raro"] == "si"      # normalizados a minusculas


async def test_client_y_path(transporte):
    sock, _ = nuevo(transporte, path="/sala/7/")
    assert sock.path == "/sala/7/"
    assert sock.client == ("127.0.0.1", 55555)


# ------------------------------------------------------------------- Message


@pytest.mark.parametrize(
    "msg, otro, igual",
    [
        (Message(text="ping"), "ping", True),
        (Message(text="ping"), "pong", False),
        (Message(data=b"ab"), b"ab", True),
        (Message(text="x"), Message(text="x"), True),
    ],
)
def test_message_se_compara_directamente(msg, otro, igual):
    assert (msg == otro) is igual


def test_message_vacio_al_parsear_json():
    with pytest.raises(ValueError):
        Message().json()


def test_message_repr_recorta():
    largo = Message(text="x" * 100)
    assert len(repr(largo)) < 60
