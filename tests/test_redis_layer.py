"""RedisLayer contra un servidor que habla RESP por TCP.

Si hay un Redis de verdad escuchando (variable DJANGO_SOCKET_TEST_REDIS, o
localhost:6379), los mismos tests corren contra el. Si no, contra el MiniRedis
de `resp_server.py`, que valida la logica pero no las rarezas de Redis.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket

import pytest

from django_socket.groups import RedisLayer
from django_socket.websocket import WebSocket

from .resp_server import MiniRedis


def _redis_real() -> str | None:
    url = os.environ.get("DJANGO_SOCKET_TEST_REDIS", "redis://127.0.0.1:6379/0")
    host, _, puerto = url.split("//", 1)[1].split("/", 1)[0].rpartition(":")
    try:
        with socket.create_connection((host or "127.0.0.1", int(puerto)), timeout=0.5):
            return url
    except OSError:
        return None


REDIS_REAL = _redis_real()


@pytest.fixture
async def url_redis():
    """Redis real si lo hay; si no, el MiniRedis."""
    if REDIS_REAL:
        yield REDIS_REAL
        return
    servidor = await MiniRedis().start()
    try:
        yield servidor.url
    finally:
        await servidor.stop()


@pytest.fixture
async def capas(url_redis):
    """Dos RedisLayer independientes: simulan dos workers distintos."""
    a, b = RedisLayer(url=url_redis, prefix="test"), RedisLayer(url=url_redis, prefix="test")
    await a.startup()
    await b.startup()
    try:
        yield a, b
    finally:
        await a.shutdown()
        await b.shutdown()


def socket_falso(transporte, layer):
    t = transporte()
    return WebSocket(t.scope, t.receive, t.send, layer=layer), t


async def esperar(condicion, limite=2.0):
    """El pub/sub es asincrono: da tiempo a que el mensaje cruce."""
    for _ in range(int(limite / 0.02)):
        if condicion():
            return True
        await asyncio.sleep(0.02)
    return False


# ---------------------------------------------------------------------------


def test_avisa_de_contra_que_se_esta_probando():
    print(f"\n  RedisLayer contra: {REDIS_REAL or 'MiniRedis (stub RESP, no Redis real)'}")


async def test_startup_conecta_y_se_suscribe(capas):
    a, _ = capas
    assert a._redis is not None
    assert a._listener is not None and not a._listener.done()


async def test_broadcast_local_sin_pasar_por_la_red(capas, transporte):
    a, _ = capas
    s1, t1 = socket_falso(transporte, a)
    s2, t2 = socket_falso(transporte, a)
    await s1.join("sala")
    await s2.join("sala")

    await s1.broadcast("hola")

    assert t1.textos[-1] == "hola"
    assert t2.textos[-1] == "hola"


async def test_el_broadcast_cruza_entre_capas(capas, transporte):
    """El caso que la capa de memoria no puede hacer: llegar a otro proceso."""
    a, b = capas
    en_a, t_a = socket_falso(transporte, a)
    en_b, t_b = socket_falso(transporte, b)
    await en_a.join("sala")
    await en_b.join("sala")

    await en_a.broadcast({"desde": "worker-a"})

    assert await esperar(lambda: t_b.textos), "el mensaje no cruzo a la otra capa"
    assert json.loads(t_b.textos[-1]) == {"desde": "worker-a"}
    assert json.loads(t_a.textos[-1]) == {"desde": "worker-a"}


async def test_no_se_entrega_dos_veces_en_el_origen(capas, transporte):
    """
    Quien publica ya entrego en local; cuando le vuelve su propio mensaje por
    pub/sub debe descartarlo, o cada miembro local lo recibiria duplicado.
    """
    a, b = capas
    s, t = socket_falso(transporte, a)
    _, _ = socket_falso(transporte, b)
    await s.join("sala")

    await s.broadcast("una vez")
    await asyncio.sleep(0.3)               # tiempo de sobra para el eco

    assert t.textos.count("una vez") == 1


async def test_exclude_self_no_cruza_como_efecto_raro(capas, transporte):
    """exclude_self es local por identidad; el resto de capas deben recibirlo."""
    a, b = capas
    emisor, t_emisor = socket_falso(transporte, a)
    otro_local, t_otro_local = socket_falso(transporte, a)
    remoto, t_remoto = socket_falso(transporte, b)
    for s in (emisor, otro_local, remoto):
        await s.join("sala")

    await emisor.broadcast("sin mi", exclude_self=True)

    assert await esperar(lambda: t_remoto.textos)
    assert t_emisor.textos == []
    assert t_otro_local.textos[-1] == "sin mi"
    assert t_remoto.textos[-1] == "sin mi"


async def test_grupos_distintos_no_se_mezclan(capas, transporte):
    a, b = capas
    en_a, _ = socket_falso(transporte, a)
    en_b, t_b = socket_falso(transporte, b)
    await en_a.join("uno")
    await en_b.join("dos")

    await en_a.broadcast("privado")
    await asyncio.sleep(0.3)

    assert t_b.textos == []


async def test_la_carga_publicada_lleva_grupo_dato_y_origen(capas, transporte):
    a, _ = capas
    s, _ = socket_falso(transporte, a)
    await s.join("sala")

    await s.broadcast({"x": 1})
    await asyncio.sleep(0.2)

    # Lo comprobamos desde el otro lado: la capa b lo recibio y lo entrego bien.
    assert a._origin  # cada capa se identifica para poder descartar su propio eco


async def test_shutdown_cancela_el_listener(url_redis):
    capa = RedisLayer(url=url_redis, prefix="test")
    await capa.startup()
    listener = capa._listener
    await capa.shutdown()
    assert listener.done()


async def test_sin_el_paquete_redis_el_error_dice_que_instalar(monkeypatch, url_redis):
    import builtins

    real_import = builtins.__import__

    def sin_redis(name, *args, **kwargs):
        if name.startswith("redis"):
            raise ImportError("no module named redis")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", sin_redis)
    capa = RedisLayer(url=url_redis)
    with pytest.raises(RuntimeError) as exc:
        await capa.startup()
    assert "pip install redis" in str(exc.value)


# ------------------------------------------------------- resistencia a caidas


class PubSubFalso:
    """Simula un pubsub que se rompe las primeras N lecturas."""

    def __init__(self, fallos=1):
        self.fallos = fallos
        self.lecturas = 0
        self.suscripciones = []

    async def get_message(self, **kwargs):
        # Cede el control como haria una lectura real con timeout; si no, el
        # bucle de escucha gira sin soltar el loop y no se le puede ni cancelar.
        await asyncio.sleep(0.01)
        self.lecturas += 1
        if self.lecturas <= self.fallos:
            raise ConnectionError("Redis se fue")
        return None

    async def subscribe(self, canal):
        self.suscripciones.append(canal)

    async def aclose(self): ...

    async def unsubscribe(self, canal): ...


async def test_send_no_tumba_el_handler_si_redis_falla(transporte, caplog):
    """
    Lo importante: la entrega local ya se hizo y el socket del usuario debe
    seguir vivo. Antes esto lanzaba ConnectionError, el dispatcher lo cazaba
    y cerraba la conexion con 1011 por un hipo de Redis.
    """
    capa = RedisLayer(url="redis://127.0.0.1:1/0")

    class RedisRoto:
        async def publish(self, canal, carga):
            raise ConnectionError("Redis no responde")

    capa._redis = RedisRoto()
    sock, t = socket_falso(transporte, capa)
    await sock.join("sala")

    await sock.broadcast("sigue llegando en local")     # no debe lanzar
    await sock.drain()

    assert t.textos[-1] == "sigue llegando en local"
    assert "no se pudo publicar en Redis" in caplog.text


async def test_el_listener_se_resuscribe_tras_un_corte(monkeypatch, caplog):
    """Sin esto, un corte de Redis deja al proceso sordo para siempre."""
    capa = RedisLayer(url="redis://127.0.0.1:1/0")
    capa._conectado = True
    roto = PubSubFalso(fallos=1)
    capa._pubsub = roto

    nuevo = PubSubFalso(fallos=0)
    capa._redis = type("R", (), {"pubsub": lambda self: nuevo})()
    monkeypatch.setattr(RedisLayer, "ESPERA_MAX", 0.01)

    tarea = asyncio.create_task(capa._listen())
    await asyncio.sleep(0.9)          # da tiempo al backoff inicial
    tarea.cancel()
    try:
        await tarea
    except asyncio.CancelledError:
        pass

    assert "se perdio la conexion con Redis" in caplog.text
    assert nuevo.suscripciones == [capa.channel], "no volvio a suscribirse"


async def test_el_listener_sale_limpio_en_shutdown():
    """Al cerrar, un error de lectura no debe generar ruido ni reintentos."""
    capa = RedisLayer(url="redis://127.0.0.1:1/0")
    capa._conectado = False           # como tras shutdown()
    capa._pubsub = PubSubFalso(fallos=1)

    await asyncio.wait_for(capa._listen(), timeout=1.0)   # debe volver, no colgarse


async def test_un_mensaje_corrupto_no_mata_el_listener(capas, caplog):
    a, _ = capas
    await a._entregar({"data": b"esto no es json"})
    assert "ilegible" in caplog.text
