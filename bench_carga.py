"""Prueba de carga: ¿donde se rompe esto?  Uso:  python bench_carga.py [N ...]

Levanta un servidor de verdad en un proceso aparte y le abre N conexiones
concurrentes desde este. Mide lo que importa antes de publicar nada:

  1. si todas las conexiones llegan a abrirse, y cuanto tardan
  2. cuanta memoria cuesta cada conexion en reposo
  3. cuanto tarda un broadcast en llegar a los N (p50, p95, maximo)
  4. si aguanta trafico sostenido sin perder mensajes ni echar a nadie

Todo se mide desde el proceso cliente, asi que los tiempos son comparables
entre si aunque el servidor viva en otro proceso.

    python bench_carga.py 100 500 1000
    DJANGO_SOCKET_LAYER=redis python bench_carga.py 500
"""

import asyncio
import json
import os
import statistics
import subprocess
import sys
import time

PUERTO = int(os.environ.get("BENCH_PORT", "8210"))
CAPA = os.environ.get("DJANGO_SOCKET_LAYER", "memory")
ORIGEN = f"http://127.0.0.1:{PUERTO}"


# --------------------------------------------------------------- el servidor


def arrancar_servidor():
    import django
    from django.conf import settings

    settings.configure(
        DEBUG=False,
        SECRET_KEY="bench",
        ALLOWED_HOSTS=["*"],
        INSTALLED_APPS=["django_socket"],
        DATABASES={},
        DJANGO_SOCKET={
            "ALLOWED_ORIGINS": ["*"],
            "LAYER": CAPA,
            "REDIS_URL": os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        },
    )
    django.setup()

    from django_socket import ws
    from django_socket.asgi import ASGIApplication

    @ws("sala/", group="sala", auth=False)
    async def sala(sock):
        async for msg in sock:
            await sock.broadcast(msg.text)

    import uvicorn

    uvicorn.run(
        ASGIApplication(),
        host="127.0.0.1",
        port=PUERTO,
        log_level="error",
        ws_ping_interval=None,      # el ping no debe falsear la medida
    )


# ----------------------------------------------------------------- utilidades


def fmt(seg):
    return f"{seg * 1000:.1f} ms" if seg < 1 else f"{seg:.2f} s"


def rss_total(proc):
    """
    Memoria del servidor, contando los hijos.

    Hace falta en Windows: el `python.exe` de un venv es un lanzador que
    re-ejecuta el interprete real en otro proceso, asi que medir solo el padre
    te da su tamaño fijo y parece que las conexiones no cuestan nada.
    """
    import psutil

    total = 0
    for p in [proc] + proc.children(recursive=True):
        try:
            total += p.memory_info().rss
        except psutil.Error:
            pass
    return total


async def esperar_puerto(timeout=30):
    for _ in range(int(timeout * 10)):
        try:
            _, w = await asyncio.open_connection("127.0.0.1", PUERTO)
            w.close()
            return True
        except OSError:
            await asyncio.sleep(0.1)
    return False


# ------------------------------------------------------------------- medidas


async def medir(n, proc_servidor):
    import psutil
    import websockets

    servidor = psutil.Process(proc_servidor.pid)
    rss0 = rss_total(servidor)

    # --- 1. abrir N conexiones
    t0 = time.perf_counter()
    conns, fallos = [], 0
    for lote in range(0, n, 50):                 # de 50 en 50, sin avalancha
        tareas = [
            websockets.connect(
                f"ws://127.0.0.1:{PUERTO}/sala/", origin=ORIGEN,
                max_queue=None, open_timeout=20,
            )
            for _ in range(min(50, n - lote))
        ]
        for r in await asyncio.gather(*tareas, return_exceptions=True):
            if isinstance(r, Exception):
                fallos += 1
            else:
                conns.append(r)
    abrir = time.perf_counter() - t0

    if not conns:
        print(f"  no se abrio ni una conexion ({fallos} fallos)")
        return

    await asyncio.sleep(1.5)                     # que se asiente
    rss_reposo = rss_total(servidor)
    por_conexion = (rss_reposo - rss0) / len(conns)

    # --- 2. un broadcast a todos, midiendo cuando llega a cada uno
    emisor = conns[0]
    oyentes = conns[1:]
    llegadas = []

    async def escuchar(ws):
        try:
            await asyncio.wait_for(ws.recv(), timeout=30)
            llegadas.append(time.perf_counter())
        except Exception:
            pass

    tareas = [asyncio.create_task(escuchar(w)) for w in oyentes]
    await asyncio.sleep(0.2)
    t_envio = time.perf_counter()
    await emisor.send("ping-fanout")
    await asyncio.gather(*tareas)
    try:
        await asyncio.wait_for(emisor.recv(), timeout=5)
    except Exception:
        pass

    latencias = sorted(t - t_envio for t in llegadas)
    entregados = len(latencias)

    # --- 3. trafico sostenido
    RONDAS = 20
    recibidos = [0] * len(oyentes)

    async def contar(i, ws):
        try:
            while True:
                await asyncio.wait_for(ws.recv(), timeout=2)
                recibidos[i] += 1
        except Exception:
            pass

    contadores = [asyncio.create_task(contar(i, w)) for i, w in enumerate(oyentes)]
    t0 = time.perf_counter()
    for i in range(RONDAS):
        await emisor.send(f"r{i}")
        await asyncio.sleep(0.01)

    # Espera hasta que deje de llegar nada, no un plazo fijo: con 6000 miembros
    # son 120.000 frames y un 2,5 s a ojo te hace informar una perdida que no
    # existe -- solo estabas mirando demasiado pronto.
    esperados = RONDAS * len(oyentes)
    quieto = 0
    limite = time.perf_counter() + 60
    while time.perf_counter() < limite:
        antes = sum(recibidos)
        await asyncio.sleep(0.5)
        if sum(recibidos) >= esperados:
            break
        quieto = quieto + 1 if sum(recibidos) == antes else 0
        if quieto >= 4:                      # 2 s sin novedad: ya no viene mas
            break
    sostenido = time.perf_counter() - t0
    for c in contadores:
        c.cancel()

    entregados_sost = sum(recibidos)
    rss_carga = rss_total(servidor)

    # --- informe
    print(f"  conexiones abiertas   {len(conns):>6} de {n}"
          + (f"   ({fallos} fallaron)" if fallos else ""))
    print(f"  tiempo en abrirlas    {fmt(abrir):>9}"
          f"   ({len(conns) / abrir:,.0f}/s)")
    print(f"  memoria por conexion  {por_conexion / 1024:>6.1f} KB"
          f"   (total {(rss_reposo - rss0) / 1e6:.0f} MB)")
    print(f"  fan-out entregado     {entregados:>6} de {len(oyentes)}")
    if latencias:
        print(f"     p50 {fmt(statistics.median(latencias))}"
              f"   p95 {fmt(latencias[int(len(latencias) * 0.95)])}"
              f"   max {fmt(latencias[-1])}")
    print(f"  sostenido {RONDAS} rondas   {entregados_sost:>6} de {esperados}"
          f"   ({100 * entregados_sost / max(1, esperados):.1f}%)"
          f"   en {fmt(sostenido)}")
    print(f"  memoria bajo carga    {(rss_carga - rss0) / 1e6:>6.0f} MB")

    await asyncio.gather(*(w.close() for w in conns), return_exceptions=True)
    await asyncio.sleep(1)


async def main(tamanos):
    print(f"Servidor en :{PUERTO}   capa: {CAPA}\n")
    proc = subprocess.Popen(
        [sys.executable, __file__, "--servidor"],
        env={**os.environ, "BENCH_PORT": str(PUERTO)},
    )
    try:
        if not await esperar_puerto():
            print("el servidor no arranco")
            return 1
        for n in tamanos:
            print(f"--- {n} conexiones ---")
            await medir(n, proc)
            print()
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    return 0


if __name__ == "__main__":
    if "--servidor" in sys.argv:
        arrancar_servidor()
    else:
        tamanos = [int(a) for a in sys.argv[1:] if a.isdigit()] or [100, 500, 1000]
        raise SystemExit(asyncio.run(main(tamanos)))
