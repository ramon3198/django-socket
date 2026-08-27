"""El cliente de pruebas publico. Se prueba a si mismo usandolo."""

import pytest

from django_socket import Events, login_required, ws
from django_socket.testing import (
    ReceiveTimeout,
    WebSocketClient,
    WebSocketDisconnect,
)

# --------------------------------------------------------------- lo basico


async def test_ida_y_vuelta():
    @ws("eco/", auth=False)
    async def eco(sock):
        async for msg in sock:
            await sock.send_text(f"eco: {msg.text}")

    async with WebSocketClient("/eco/") as c:
        assert c.connected
        await c.send("hola")
        assert await c.receive_text() == "eco: hola"


async def test_json_en_los_dos_sentidos():
    @ws("j/", auth=False)
    async def j(sock):
        async for datos in sock.iter_json():
            await sock.send_json({"vino": datos})

    async with WebSocketClient("/j/") as c:
        await c.send_json({"a": 1})
        assert await c.receive_json() == {"vino": {"a": 1}}


async def test_parametros_de_ruta():
    @ws("p/<int:pk>/", auth=False)
    async def p(sock, pk):
        await sock.send_json({"pk": pk, "tipo": type(pk).__name__})

    async with WebSocketClient("/p/42/") as c:
        assert await c.receive_json() == {"pk": 42, "tipo": "int"}


async def test_query_params():
    @ws("q/", auth=False)
    async def q(sock):
        await sock.send_json(sock.query_params)

    async with WebSocketClient("/q/", query="token=abc&n=3") as c:
        assert await c.receive_json() == {"token": "abc", "n": "3"}


async def test_binario():
    @ws("b/", auth=False)
    async def b(sock):
        async for msg in sock:
            await sock.send_bytes(msg.bytes[::-1])

    async with WebSocketClient("/b/") as c:
        await c.send(b"abc")
        assert await c.receive_bytes() == b"cba"


# ------------------------------------------------------------------ grupos


async def test_dos_clientes_en_la_misma_sala():
    @ws("sala/<str:n>/", group="s:{n}", auth=False)
    async def sala(sock, n):
        async for msg in sock:
            await sock.broadcast(msg.text)

    async with WebSocketClient("/sala/uno/") as a, WebSocketClient("/sala/uno/") as b:
        await b.send("hola a todos")
        assert await a.receive_text() == "hola a todos"
        assert await b.receive_text() == "hola a todos"


async def test_exclude_self():
    @ws("sala/<str:n>/", group="s:{n}", auth=False)
    async def sala(sock, n):
        async for msg in sock:
            await sock.broadcast(msg.text, exclude_self=True)

    async with WebSocketClient("/sala/x/") as a, WebSocketClient("/sala/x/") as b:
        await b.send("solo para a")
        assert await a.receive_text() == "solo para a"
        assert await b.receive_nothing()


async def test_salas_aisladas():
    @ws("sala/<str:n>/", group="s:{n}", auth=False)
    async def sala(sock, n):
        async for msg in sock:
            await sock.broadcast(msg.text)

    async with WebSocketClient("/sala/uno/") as a, WebSocketClient("/sala/dos/") as b:
        await b.send("privado")
        assert await a.receive_nothing()


# ------------------------------------------------------------------ rechazos


async def test_ruta_inexistente():
    async with WebSocketClient("/no-existe/") as c:
        assert not c.connected
        assert c.close_code == 4404


async def test_origen_ajeno():
    @ws("x/", auth=False)
    async def x(sock): ...

    async with WebSocketClient("/x/", origin="http://atacante.example") as c:
        assert not c.connected
        assert not c.accepted          # el origen se tumba en el handshake
        assert c.close_code == 403


async def test_json_invalido_da_4400():
    @ws("j/", auth=False)
    async def j(sock):
        async for msg in sock:
            msg.json()

    async with WebSocketClient("/j/") as c:
        await c.send("{roto")
        assert await c.wait_closed() == 4400


async def test_recibir_tras_el_cierre_lanza():
    @ws("corto/", auth=False)
    async def corto(sock):
        await sock.close(4001, "hasta luego")

    async with WebSocketClient("/corto/") as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            await c.receive()
        assert exc.value.code == 4001
        assert c.close_reason == "hasta luego"


# --------------------------------------------------------------------- auth


@pytest.mark.django_db(transaction=True)
async def test_login_required_sin_usuario():
    @ws("panel/")
    @login_required
    async def panel(sock):
        await sock.send_text("dentro")

    async with WebSocketClient("/panel/") as c:
        assert await c.wait_closed() == 4401


@pytest.mark.django_db(transaction=True)
async def test_user_deja_la_sesion_lista(crear_usuario):
    """Sin montar cookies a mano: le pasas el User y ya."""
    @ws("panel/")
    @login_required
    async def panel(sock):
        await sock.send_json({"quien": sock.user.username, "pk": sock.user.pk})

    user = await crear_usuario("ramon")
    async with WebSocketClient("/panel/", user=user) as c:
        assert await c.receive_json() == {"quien": "ramon", "pk": user.pk}


@pytest.mark.django_db(transaction=True)
async def test_sin_user_es_anonimo():
    @ws("quien/")
    async def quien(sock):
        await sock.send_json({"auth": sock.user.is_authenticated})

    async with WebSocketClient("/quien/") as c:
        assert await c.receive_json() == {"auth": False}


# ------------------------------------------------------------------- Events


async def test_con_el_enrutador_de_eventos():
    ev = Events()

    @ev.on("saluda")
    async def saluda(sock, datos):
        await sock.send_json({"type": "hola", "a": datos["nombre"]})

    @ws("e/", auth=False)
    async def e(sock):
        await ev.run(sock)

    async with WebSocketClient("/e/") as c:
        await c.send_json({"type": "saluda", "nombre": "ramon"})
        assert await c.receive_json() == {"type": "hola", "a": "ramon"}


# ---------------------------------------------------------------- comodidades


async def test_receive_all_recoge_lo_pendiente():
    @ws("varios/", auth=False)
    async def varios(sock):
        for i in range(3):
            await sock.send_json({"n": i})
        async for _ in sock:
            pass

    async with WebSocketClient("/varios/") as c:
        mensajes = await c.receive_all()
        assert [m.json()["n"] for m in mensajes] == [0, 1, 2]


async def test_el_timeout_falla_rapido_en_vez_de_colgarse():
    @ws("mudo/", auth=False)
    async def mudo(sock):
        async for _ in sock:
            pass

    async with WebSocketClient("/mudo/") as c:
        with pytest.raises(ReceiveTimeout) as exc:
            await c.receive(timeout=0.05)
        assert "/mudo/" in str(exc.value)


async def test_subprotocolo_negociado():
    @ws("gql/", auth=False)
    async def gql(sock):
        await sock.accept(subprotocol="graphql-ws")

    async with WebSocketClient("/gql/", subprotocols=["graphql-ws"]) as c:
        assert c.subprotocol == "graphql-ws"


async def test_cabeceras_propias():
    @ws("h/", auth=False)
    async def h(sock):
        await sock.send_json({"api": sock.headers.get("x-api-key")})

    async with WebSocketClient("/h/", headers={"X-Api-Key": "secreta"}) as c:
        assert await c.receive_json() == {"api": "secreta"}


async def test_el_desconectar_deja_correr_el_codigo_de_limpieza():
    """El aviso de salida tras el bucle tiene que llegar a los demas."""
    @ws("sala/<str:n>/", group="s:{n}", auth=False)
    async def sala(sock, n):
        async for msg in sock:
            await sock.broadcast(msg.text)
        await sock.broadcast("se fue alguien")

    async with WebSocketClient("/sala/z/") as a:
        b = await WebSocketClient("/sala/z/").connect()
        await b.disconnect()
        assert await a.receive_text() == "se fue alguien"


async def test_un_bug_del_handler_llega_como_1011():
    @ws("boom/", auth=False)
    async def boom(sock):
        raise RuntimeError("bug mio")

    async with WebSocketClient("/boom/") as c:
        assert await c.wait_closed() == 1011
