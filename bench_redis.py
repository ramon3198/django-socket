"""Cuanto aguanta RedisLayer, en numeros.  Uso:  python bench_redis.py

Necesita un Redis escuchando (REDIS_URL para apuntar a otro):

    docker run -d --rm -p 6379:6379 redis:7-alpine
    python bench_redis.py


Mide tres cosas distintas que se confunden facil:
  1. coste de publicar     -- lo que tarda broadcast() en volver
  2. latencia de cruce     -- cuanto tarda un mensaje en llegar al otro proceso
  3. escalado con miembros -- que pasa al crecer el grupo
"""

import asyncio
import os
import statistics
import sys
import time

import django
from django.conf import settings

settings.configure(
    DEBUG=False,
    SECRET_KEY="x",
    ALLOWED_HOSTS=["*"],
    INSTALLED_APPS=["django_socket"],
    DATABASES={},
    DJANGO_SOCKET={},
)
django.setup()

from django_socket.groups import MemoryLayer, RedisLayer  # noqa: E402

URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0")
CARGA = {"tipo": "mensaje", "texto": "x" * 200}


class Sock:
    """Un miembro que consume al instante, para aislar el coste de la capa."""

    def __init__(self):
        self.n = 0
        self.ultimo = None

    async def enqueue(self, data):
        self.n += 1
        self.ultimo = data
        return True

    async def send(self, data):
        self.n += 1

    def evict(self, *a, **k): ...


def fmt(seg):
    return f"{seg * 1000:.3f} ms" if seg < 1 else f"{seg:.2f} s"


async def coste_de_publicar(capa, miembros, n=2000):
    grupo = f"g{miembros}"
    socks = [Sock() for _ in range(miembros)]
    for s in socks:
        await capa.add(grupo, s)

    t0 = time.perf_counter()
    for _ in range(n):
        await capa.send(grupo, CARGA)
    total = time.perf_counter() - t0

    for s in socks:
        await capa.discard(grupo, s)
    return total / n, n / total


async def latencia_de_cruce(a, b, n=300):
    """Tiempo desde que A publica hasta que B lo entrega."""
    recibido = asyncio.Queue()

    class Espia(Sock):
        async def enqueue(self, data):
            recibido.put_nowait(time.perf_counter())
            return True

    espia = Espia()
    await b.add("cruce", espia)
    emisor = Sock()
    await a.add("cruce", emisor)

    muestras = []
    for _ in range(n):
        t0 = time.perf_counter()
        await a.send("cruce", CARGA)
        try:
            t1 = await asyncio.wait_for(recibido.get(), timeout=5)
            muestras.append(t1 - t0)
        except asyncio.TimeoutError:
            break
    return muestras


async def main():
    print(f"Redis: {URL}\n")

    memoria = MemoryLayer()
    a = RedisLayer(url=URL, prefix="carga")
    b = RedisLayer(url=URL, prefix="carga")
    await a.startup()
    await b.startup()

    try:
        print("1. Coste de un broadcast (lo que tarda en volver)")
        print(f"   {'miembros':>9}  {'memoria':>14}  {'redis':>14}  {'redis msg/s':>12}")
        for miembros in (1, 10, 100, 1000):
            m_seg, _ = await coste_de_publicar(memoria, miembros)
            r_seg, r_ops = await coste_de_publicar(a, miembros, n=500)
            print(f"   {miembros:>9}  {fmt(m_seg):>14}  {fmt(r_seg):>14}  {r_ops:>12,.0f}")

        print("\n2. Latencia de cruce entre dos procesos (300 muestras)")
        m = await latencia_de_cruce(a, b)
        if m:
            m.sort()
            print(f"   mediana  {fmt(statistics.median(m))}")
            print(f"   p95      {fmt(m[int(len(m) * 0.95)])}")
            print(f"   p99      {fmt(m[int(len(m) * 0.99)])}")
            print(f"   maximo   {fmt(m[-1])}")
        else:
            print("   sin muestras")

        print("\n3. Cuanto tarda en llenarse el buzon por defecto (256)")
        r_seg, _ = await coste_de_publicar(a, 100, n=200)
        print(f"   a {1 / r_seg:,.0f} broadcast/s, un cliente que no lee nada")
        print(f"   llena el buzon en {fmt(256 * r_seg)}")
        print("   (es el margen que tiene un cliente lento antes de que se le eche)")
    finally:
        await a.shutdown()
        await b.shutdown()


asyncio.run(main())
