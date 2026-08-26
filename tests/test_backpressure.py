"""Un cliente que no consume no puede arrastrar a los demas.

Medido antes de escribir esto: uvicorn frena de verdad (se bloquea a los ~24 MB
encolados hacia un cliente que no lee), asi que `send()` deja de volver. Con el
reparto esperando a cada miembro, un solo cliente asi dejaba colgado para
siempre al que difunde -- y con el, su bucle de lectura y su limpieza.
"""

import asyncio

import pytest
from django.test import override_settings

from django_socket import group_size, ws
from django_socket.testing import WebSocketClient
from django_socket.websocket import CLOSED, WebSocket


def sock_de(transporte, **kwargs):
    from django_socket import groups

    t = transporte(**kwargs)
    return WebSocket(t.scope, t.receive, t.send, layer=groups.get_layer()), t


def atascado(transporte):
    """Un socket cuya escritura no completa nunca: el cliente no lee."""
    sock, t = sock_de(transporte)

    async def nunca(_):
        await asyncio.Event().wait()

    sock._send = nunca
    return sock, t


# ------------------------------------------------------- lo que se arreglo


async def test_un_cliente_atascado_no_cuelga_al_que_difunde(transporte):
    """El bug de raiz: antes de esto, `broadcast` no volvia jamas."""
    emisor, t_emisor = sock_de(transporte)
    lento, _ = atascado(transporte)
    await emisor.join("sala")
    await lento.join("sala")

    # Sin arreglo, este wait_for daba TimeoutError.
    await asyncio.wait_for(emisor.broadcast("hola"), timeout=1)


async def test_los_demas_siguen_recibiendo(transporte):
    rapido, t_rapido = sock_de(transporte)
    lento, _ = atascado(transporte)
    await rapido.join("sala")
    await lento.join("sala")

    await rapido.broadcast("uno")
    await rapido.broadcast("dos")
    await rapido.drain()

    assert t_rapido.textos == ["uno", "dos"]


async def test_el_handler_sigue_leyendo_su_socket(transporte):
    """
    La consecuencia que mas dolia: el que difunde dejaba de leer su propio
    socket, asi que no procesaba nada mas ni ejecutaba su limpieza.
    """
    procesados = []

    @ws("sala/<str:n>/", group="s:{n}", auth=False)
    async def sala(sock, n):
        async for msg in sock:
            await sock.broadcast(msg.text)
            procesados.append(msg.text)

    async with WebSocketClient("/sala/x/") as c:
        # Metemos un socket que no consume en el mismo grupo.
        from django_socket import groups

        lento, _ = atascado(transporte)
        await groups.get_layer().add("s:x", lento)

        await c.send("uno")
        await c.send("dos")
        await c.send("tres")
        await asyncio.sleep(0.05)

    assert procesados == ["uno", "dos", "tres"]


# ------------------------------------------------------------ el buzon


@override_settings(DJANGO_SOCKET={"SEND_QUEUE_MAX": 3})
async def test_el_buzon_lleno_echa_al_cliente(transporte):
    rapido, _ = sock_de(transporte)
    lento, _ = atascado(transporte)
    await rapido.join("sala")
    await lento.join("sala")
    assert await group_size("sala") == 2

    # El primero se lo lleva el escritor y se queda atascado ahi; a partir del
    # cuarto el buzon (3) no da mas de si.
    for i in range(6):
        await rapido.broadcast(f"m{i}")

    assert await group_size("sala") == 1, "no se echo al que no consume"
    assert lento._state == CLOSED
    assert lento.close_code == 1013            # "Try Again Later"


@override_settings(DJANGO_SOCKET={"SEND_QUEUE_MAX": 2, "SEND_QUEUE_FULL": "drop_oldest"})
async def test_drop_oldest_conserva_al_cliente(transporte):
    """Para flujos que toleran huecos: posiciones de cursor, telemetria."""
    rapido, _ = sock_de(transporte)
    lento, _ = atascado(transporte)
    await rapido.join("sala")
    await lento.join("sala")

    for i in range(10):
        await rapido.broadcast(f"m{i}")

    assert await group_size("sala") == 2, "no deberia echarse a nadie"
    assert lento._state != CLOSED


@override_settings(DJANGO_SOCKET={"SEND_QUEUE_MAX": 4})
async def test_una_rafaga_normal_no_echa_a_nadie(transporte):
    """El buzon no puede ser tan estricto que castigue a un cliente sano."""
    a, ta = sock_de(transporte)
    b, tb = sock_de(transporte)
    await a.join("sala")
    await b.join("sala")

    for i in range(50):                 # muy por encima del buzon
        await a.broadcast(f"m{i}")
        await b.drain()                 # b va leyendo, como haria uno sano

    assert await group_size("sala") == 2
    assert len(tb.textos) == 50


async def test_el_orden_se_respeta(transporte):
    sock, t = sock_de(transporte)
    await sock.join("sala")

    for i in range(20):
        await sock.broadcast(str(i))
    await sock.drain()

    assert t.textos == [str(i) for i in range(20)]


async def test_send_directo_sigue_esperando(transporte):
    """
    `sock.send()` no pasa por el buzon a proposito: ahi el bloqueo es sano.
    Si el cliente no puede seguirte, tu handler va mas despacio, que es lo
    correcto en un flujo uno-a-uno.
    """
    sock, _ = atascado(transporte)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(sock.send_text("bloquea"), timeout=0.2)


async def test_evict_no_espera_al_que_esta_bloqueado(transporte):
    """
    Echarlo no puede pasar por `await close()`: escribir hacia el esta
    bloqueado, que es justo el motivo por el que lo echamos.
    """
    lento, _ = atascado(transporte)
    await lento.join("sala")

    await asyncio.wait_for(asyncio.to_thread(lambda: None), timeout=1)
    lento.evict()                        # es sincrono y no bloquea

    assert lento._state == CLOSED
    assert lento.close_code == 1013


async def test_drain_espera_a_que_salga_todo(transporte):
    """
    `broadcast` cede el turno al volver, asi que en el caso normal el mensaje
    ya salio. `drain()` es la garantia: espera aunque el escritor vaya
    retrasado, sin dormir a ciegas.
    """
    sock, t = sock_de(transporte)
    await sock.join("sala")

    for i in range(30):
        await sock.broadcast(str(i))
    await sock.drain()

    assert t.textos == [str(i) for i in range(30)]


async def test_broadcast_cede_el_turno_a_los_escritores(transporte):
    """Un lote difundido en bucle no puede llenar el buzon de un cliente sano."""
    emisor, _ = sock_de(transporte)
    sano, t_sano = sock_de(transporte)
    await emisor.join("sala")
    await sano.join("sala")

    for i in range(1000):                # muy por encima del buzon (256)
        await emisor.broadcast(str(i))
    await sano.drain()

    assert await group_size("sala") == 2, "se echo a un cliente que si consume"
    assert len(t_sano.textos) == 1000


async def test_cerrar_vacia_lo_pendiente(transporte):
    """Lo encolado justo antes de cerrar debe salir, no perderse."""
    sock, t = sock_de(transporte)
    await sock.join("sala")

    await sock.broadcast("ultimo aviso")
    await sock.close()

    assert "ultimo aviso" in t.textos


async def test_el_escritor_no_sobrevive_al_socket(transporte):
    sock, _ = sock_de(transporte)
    await sock.join("sala")
    await sock.broadcast("x")
    escritor = sock._writer
    assert escritor is not None

    await sock.close()
    await asyncio.sleep(0)
    assert escritor.done() or escritor.cancelled()


async def test_la_desconexion_normal_no_deja_tareas_huerfanas(transporte):
    """
    El camino habitual no es close(): es que el cliente se vaya. Ahi
    `receive()` marca el socket cerrado y `close()` sale de vuelta sin llegar
    a cancelar nada, asi que el escritor quedaba vivo en cada conexion.
    """
    from django_socket.websocket import WebSocketDisconnect

    sock, t = sock_de(transporte)
    await sock.join("sala")
    await sock.broadcast("algo")
    escritor = sock._writer
    assert escritor is not None and not escritor.done()

    t.cliente_cierra()
    with pytest.raises(WebSocketDisconnect):
        await sock.receive()

    await asyncio.sleep(0)
    assert escritor.cancelled() or escritor.done(), "el escritor quedo huerfano"


async def test_no_quedan_escritores_vivos_tras_una_sala_entera(transporte):
    """Ni uno por conexion: en un servidor de verdad eso se acumula."""
    vivos_antes = len([t for t in asyncio.all_tasks() if "_drenar" in str(t.get_coro())])

    for _ in range(20):
        sock, _ = sock_de(transporte)
        await sock.join("sala")
        await sock.broadcast("x")
        await sock.close()

    await asyncio.sleep(0)
    vivos = [t for t in asyncio.all_tasks() if "_drenar" in str(t.get_coro())]
    assert len(vivos) <= vivos_antes, f"quedaron {len(vivos)} escritores vivos"


async def test_echar_a_un_cliente_termina_su_bucle_limpiamente(transporte):
    """
    Cuando somos nosotros quienes cerramos (expulsion, escritor muerto), el
    `async for` del handler debe terminar como con una desconexion normal.
    Si no, revienta con WebSocketClosed y la limpieza posterior no corre.
    """
    from django_socket.websocket import WebSocketDisconnect

    sock, _ = sock_de(transporte)
    sock.evict()

    with pytest.raises(WebSocketDisconnect) as exc:
        await sock.receive()
    assert exc.value.code == 1013


async def test_el_handler_ejecuta_su_limpieza_tras_una_expulsion(transporte):
    """
    El aviso de salida tras el bucle es justo lo que se perdia.

    Al echar a alguien, su handler sigue parado en `receive()`; despierta
    cuando el servidor ASGI entrega el `websocket.disconnect` que sigue al
    cierre (con uvicorn, como mucho ping_interval + ping_timeout). Lo que se
    comprueba aqui es que al despertar el bucle termina limpio en vez de
    reventar con WebSocketClosed.
    """
    limpiezas = []
    sock, t = sock_de(transporte)

    async def handler():
        async for _ in sock:
            pass
        limpiezas.append("limpio")

    tarea = asyncio.create_task(handler())
    await asyncio.sleep(0)

    sock.evict()
    t.cliente_cierra(code=1013)          # lo que manda el servidor tras el cierre
    await asyncio.wait_for(tarea, timeout=1)

    assert limpiezas == ["limpio"], "el bucle no termino limpiamente"


async def test_un_socket_ya_cerrado_no_revienta_el_bucle(transporte):
    """Sin esto salia WebSocketClosed, que el `async for` no sabe atrapar."""
    sock, _ = sock_de(transporte)
    sock.evict()

    recorrido = [m async for m in sock]   # debe terminar, no lanzar
    assert recorrido == []
