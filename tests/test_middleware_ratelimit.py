"""Middleware y limite de tasa: lo que un proyecto grande necesita alrededor."""


import pytest
from django.test import override_settings

from django_socket import ws
from django_socket.middleware import log_connections, max_connections_per_user
from django_socket.ratelimit import CLOSE_RATE_LIMIT, TokenBucket, parse_rate
from django_socket.testing import WebSocketClient

# ---------------------------------------------------------------- middleware


async def test_envuelve_el_handler():
    orden = []

    async def fuera(sock, next_):
        orden.append("fuera:antes")
        await next_()
        orden.append("fuera:despues")

    @ws("x/", auth=False)
    async def handler(sock):
        orden.append("handler")

    with override_settings(DJANGO_SOCKET={"MIDDLEWARE": [fuera]}):
        async with WebSocketClient("/x/"):
            pass

    assert orden == ["fuera:antes", "handler", "fuera:despues"]


async def test_el_primero_de_la_lista_es_el_mas_externo():
    """Como el MIDDLEWARE de Django, para que no haya que adivinarlo."""
    orden = []

    def hacer(nombre):
        async def mw(sock, next_):
            orden.append(f"{nombre}:entra")
            await next_()
            orden.append(f"{nombre}:sale")
        mw.__name__ = nombre
        return mw

    @ws("x/", auth=False)
    async def handler(sock):
        orden.append("handler")

    with override_settings(DJANGO_SOCKET={"MIDDLEWARE": [hacer("A"), hacer("B")]}):
        async with WebSocketClient("/x/"):
            pass

    assert orden == ["A:entra", "B:entra", "handler", "B:sale", "A:sale"]


async def test_puede_cortar_la_conexion():
    llamado = []

    async def portero(sock, next_):
        await sock.close(4403, "Nope")
        # sin llamar a next_(): el handler no debe correr

    @ws("x/", auth=False)
    async def handler(sock):
        llamado.append(1)

    with override_settings(DJANGO_SOCKET={"MIDDLEWARE": [portero]}):
        async with WebSocketClient("/x/") as c:
            assert await c.wait_closed() == 4403

    assert llamado == []


async def test_ve_el_usuario_ya_resuelto(transporte):
    """El middleware corre despues de autenticar, para poder decidir con eso."""
    visto = {}

    async def mira(sock, next_):
        visto["auth"] = sock.user.is_authenticated
        await next_()

    @ws("x/")
    async def handler(sock): ...

    with override_settings(DJANGO_SOCKET={"MIDDLEWARE": [mira]}):
        async with WebSocketClient("/x/"):
            pass

    assert visto["auth"] is False


async def test_un_fallo_del_middleware_se_trata_como_uno_del_handler(caplog):
    async def revienta(sock, next_):
        raise RuntimeError("bug en el middleware")

    @ws("x/", auth=False)
    async def handler(sock): ...

    with override_settings(DJANGO_SOCKET={"MIDDLEWARE": [revienta]}):
        async with WebSocketClient("/x/") as c:
            assert await c.wait_closed() == 1011
    assert "bug en el middleware" in caplog.text


async def test_el_finally_del_middleware_corre_aunque_el_cliente_se_vaya():
    """Es donde va la metrica de duracion: tiene que cerrarse siempre."""
    cerrados = []

    async def medir(sock, next_):
        try:
            await next_()
        finally:
            cerrados.append(sock.path)

    @ws("x/", auth=False)
    async def handler(sock):
        async for _ in sock:
            pass

    with override_settings(DJANGO_SOCKET={"MIDDLEWARE": [medir]}):
        async with WebSocketClient("/x/") as c:
            await c.send("hola")

    assert cerrados == ["/x/"]


async def test_sin_middleware_no_hay_sobrecoste():
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send("ok")

    with override_settings(DJANGO_SOCKET={}):
        async with WebSocketClient("/x/") as c:
            assert await c.receive_text() == "ok"


# ------------------------------------------------- middlewares incorporados


async def test_max_conexiones_por_usuario():
    @ws("x/", auth=False)
    async def handler(sock):
        async for _ in sock:
            pass

    with override_settings(DJANGO_SOCKET={"MIDDLEWARE": [max_connections_per_user(2)]}):
        a = await WebSocketClient("/x/").connect()
        b = await WebSocketClient("/x/").connect()
        c = await WebSocketClient("/x/").connect()

        assert a.connected and b.connected
        assert await c.wait_closed() == 4429, "la tercera deberia rechazarse"

        await a.disconnect()
        d = await WebSocketClient("/x/").connect()
        assert d.connected, "al cerrar una, deberia caber otra"
        await b.disconnect()
        await d.disconnect()


async def test_log_connections_deja_una_linea_por_conexion(caplog):
    import logging

    caplog.set_level(logging.INFO, logger="django_socket.access")

    @ws("x/", auth=False)
    async def handler(sock): ...

    with override_settings(DJANGO_SOCKET={"MIDDLEWARE": [log_connections()]}):
        async with WebSocketClient("/x/"):
            pass

    assert "/x/" in caplog.text and "dur=" in caplog.text


# --------------------------------------------------------- parseo del limite


@pytest.mark.parametrize(
    "spec, esperado",
    [
        ("60/m", (60.0, 60.0)),
        ("10/s", (10.0, 1.0)),
        ("100/5m", (100.0, 300.0)),
        ("1000/h", (1000.0, 3600.0)),
        ("5/d", (5.0, 86400.0)),
        (" 60 / m ", (60.0, 60.0)),
    ],
)
def test_parsear(spec, esperado):
    assert parse_rate(spec) == esperado


@pytest.mark.parametrize("spec", ["60", "60/x", "abc", "", "-5/m", 60])
def test_un_formato_invalido_muestra_el_formato_bueno(spec):
    with pytest.raises(ValueError) as exc:
        parse_rate(spec)
    assert "60/m" in str(exc.value), "el error deberia enseñar un ejemplo valido"


def test_una_cantidad_de_cero_dice_exactamente_eso():
    """El formato es correcto; el problema es el numero. Dilo asi."""
    with pytest.raises(ValueError) as exc:
        parse_rate("0/s")
    assert "> 0" in str(exc.value)


def test_una_ruta_con_rate_limit_malo_falla_al_registrarse():
    with pytest.raises(ValueError):
        @ws("y/", rate_limit="esto no vale")
        async def handler(sock): ...


# ------------------------------------------------------------ el token bucket


def test_el_cubo_deja_pasar_hasta_la_capacidad():
    bucket = TokenBucket(5, 60)
    assert all(bucket.consume() for _ in range(5))
    assert not bucket.consume()


def test_el_cubo_se_rellena_con_el_tiempo():
    """
    Lo que lo distingue de un contador por ventana: no espera al corte del
    minuto, va reponiendo.
    """
    bucket = TokenBucket(10, 1)          # 10/s
    for _ in range(10):
        bucket.consume()
    assert not bucket.consume()

    bucket.stamp -= 0.5           # como si hubiera pasado medio segundo
    assert bucket.consume(), "medio segundo deberia reponer ~5"


def test_burst_permite_un_pico_sin_subir_el_ritmo():
    bucket = TokenBucket(10, 1, burst=50)
    assert sum(bucket.consume() for _ in range(50)) == 50
    assert not bucket.consume()


def test_el_cubo_dice_cuanto_esperar():
    bucket = TokenBucket(1, 10)          # 1 cada 10s
    bucket.consume()
    assert 9 <= bucket.retry_after <= 10


# --------------------------------------------------- de punta a punta


async def test_pasarse_del_limite_cierra_con_4429():
    @ws("chat/", auth=False, rate_limit="3/m")
    async def chat(sock):
        async for msg in sock:
            await sock.send(msg.text)

    async with WebSocketClient("/chat/") as c:
        for i in range(3):
            await c.send(f"m{i}")
            assert await c.receive_text() == f"m{i}"

        await c.send("uno de mas")
        assert await c.wait_closed() == CLOSE_RATE_LIMIT


async def test_el_motivo_dice_cuanto_esperar():
    @ws("chat/", auth=False, rate_limit="1/m")
    async def chat(sock):
        async for _ in sock:
            pass

    async with WebSocketClient("/chat/") as c:
        await c.send("uno")
        await c.send("dos")
        await c.wait_closed()
        assert "Rate limit" in c.close_reason and "retry in" in c.close_reason


async def test_un_ritmo_normal_no_molesta():
    @ws("chat/", auth=False, rate_limit="100/s")
    async def chat(sock):
        async for msg in sock:
            await sock.send(msg.text)

    async with WebSocketClient("/chat/") as c:
        for i in range(20):
            await c.send(f"m{i}")
            assert await c.receive_text() == f"m{i}"
        assert c.connected


@override_settings(DJANGO_SOCKET={"RATE_LIMIT": "2/m"})
async def test_el_limite_global_de_settings_aplica():
    @ws("chat/", auth=False)
    async def chat(sock):
        async for _ in sock:
            pass

    async with WebSocketClient("/chat/") as c:
        await c.send("uno")
        await c.send("dos")
        await c.send("tres")
        assert await c.wait_closed() == CLOSE_RATE_LIMIT


@override_settings(DJANGO_SOCKET={"RATE_LIMIT": "1/m"})
async def test_la_ruta_manda_sobre_el_global():
    @ws("chat/", auth=False, rate_limit="50/s")
    async def chat(sock):
        async for msg in sock:
            await sock.send(msg.text)

    async with WebSocketClient("/chat/") as c:
        for i in range(5):
            await c.send(f"m{i}")
            await c.receive_text()
        assert c.connected


async def test_sin_limite_configurado_no_se_toca_nada():
    @ws("chat/", auth=False)
    async def chat(sock):
        async for msg in sock:
            await sock.send(msg.text)

    with override_settings(DJANGO_SOCKET={}):
        async with WebSocketClient("/chat/") as c:
            for i in range(200):
                await c.send("x")
                await c.receive_text()
            assert c.connected


async def test_el_limite_es_por_socket_no_global():
    """Dos clientes distintos no comparten cubo."""
    @ws("chat/", auth=False, rate_limit="2/m")
    async def chat(sock):
        async for msg in sock:
            await sock.send(msg.text)

    async with WebSocketClient("/chat/") as a, WebSocketClient("/chat/") as b:
        for c in (a, b):
            for i in range(2):
                await c.send("x")
                await c.receive_text()
        assert a.connected and b.connected
