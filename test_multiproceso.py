"""Prueba de fan-out entre procesos distintos a traves de Redis.

Levanta dos `manage.py runserver` con DJANGO_SOCKET_LAYER=redis en puertos
distintos -- dos procesos de verdad, como `uvicorn --workers 2` -- y comprueba
que un mensaje enviado a uno llega a un cliente conectado al otro.

    python test_multiproceso.py 8091 8092

Con la capa 'memory' esto DEBE fallar: es justo lo que Redis viene a arreglar.
"""

import asyncio
import json
import sys

import websockets

A = int(sys.argv[1]) if len(sys.argv) > 1 else 8091
B = int(sys.argv[2]) if len(sys.argv) > 2 else 8092

resultados = []


def check(nombre, ok, detalle=""):
    resultados.append((nombre, ok))
    print(f"  {'PASS' if ok else 'FALLA'}  {nombre}" + (f"  -> {detalle}" if detalle else ""),
          flush=True)


def url(puerto, ruta):
    return f"ws://127.0.0.1:{puerto}{ruta}"


def origen(puerto):
    return f"http://127.0.0.1:{puerto}"


async def recibir(ws, timeout=3.0):
    return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))


async def esperar(ws, condicion, timeout=4.0):
    """
    Lee hasta encontrar el mensaje que interesa.

    Hace falta porque un broadcast sin exclude_self llega TAMBIEN al que lo
    envio: quien manda tiene su propia copia en la cola, y leerla a ciegas te
    hace comparar contra el mensaje anterior.
    """
    vistos = []
    fin = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < fin:
        msg = await recibir(ws, timeout=max(0.1, fin - asyncio.get_event_loop().time()))
        vistos.append(msg)
        if condicion(msg):
            return msg
    raise AssertionError(f"no llego el mensaje esperado; vistos: {vistos}")


async def test_mensaje_cruza_de_worker_a_worker():
    """El caso que la capa de memoria no puede hacer."""
    async with websockets.connect(url(A, "/chat/multi/"), origin=origen(A)) as en_a:
        async with websockets.connect(url(B, "/chat/multi/"), origin=origen(B)) as en_b:
            entra = await esperar(en_a, lambda m: m["tipo"] == "entra")
            check("worker A ve conectarse a alguien del worker B", True, str(entra))

            await en_b.send("hola desde el otro proceso")
            msg = await esperar(en_a, lambda m: m.get("texto") == "hola desde el otro proceso")
            check("un mensaje del worker B llega al worker A", True, str(msg))

            await en_a.send("y de vuelta")
            vuelta = await esperar(en_b, lambda m: m.get("texto") == "y de vuelta")
            check("y en sentido contrario tambien", True, str(vuelta))

        salida = await esperar(en_a, lambda m: m["tipo"] == "sale")
        check("worker A ve desconectarse a alguien del worker B", True, str(salida))


async def test_las_salas_siguen_aisladas_entre_procesos():
    """Redis reparte a todos los procesos, pero el grupo sigue mandando."""
    async with websockets.connect(url(A, "/chat/sala-uno/"), origin=origen(A)) as en_a:
        async with websockets.connect(url(B, "/chat/sala-dos/"), origin=origen(B)) as en_b:
            await en_b.send("no deberia salir de sala-dos")
            try:
                fuga = await recibir(en_a, timeout=1.5)
                check("las salas no se mezclan entre procesos", False, f"fuga: {fuga}")
            except asyncio.TimeoutError:
                check("las salas no se mezclan entre procesos", True)


async def test_sin_duplicados_en_el_proceso_que_publica():
    """
    Dos clientes en el MISMO worker: el mensaje se entrega en local y ademas
    se publica en Redis. Si el eco no se descartara, llegaria dos veces.
    """
    async with websockets.connect(url(A, "/chat/dobles/"), origin=origen(A)) as uno:
        async with websockets.connect(url(A, "/chat/dobles/"), origin=origen(A)) as dos:
            await recibir(uno)                      # "entra"
            await dos.send("una sola vez")

            recibidos = [await recibir(uno)]
            try:
                recibidos.append(await recibir(uno, timeout=1.5))
            except asyncio.TimeoutError:
                pass

            mensajes = [m for m in recibidos if m.get("texto") == "una sola vez"]
            check("el emisor no duplica su propio mensaje",
                  len(mensajes) == 1, f"{len(mensajes)} copias")


async def main():
    print(f"\nWorker A: {A}   Worker B: {B}\n", flush=True)
    for test in (
        test_mensaje_cruza_de_worker_a_worker,
        test_las_salas_siguen_aisladas_entre_procesos,
        test_sin_duplicados_en_el_proceso_que_publica,
    ):
        try:
            await asyncio.wait_for(test(), timeout=20)
        except asyncio.TimeoutError:
            check(test.__name__, False, "COLGADO (>20s)")
        except Exception as exc:
            check(test.__name__, False, f"{type(exc).__name__}: {exc}")

    fallos = [n for n, ok in resultados if not ok]
    print(f"\n{len(resultados) - len(fallos)}/{len(resultados)} OK", flush=True)
    if fallos:
        print("Fallan: " + ", ".join(fallos))
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
