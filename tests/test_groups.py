"""Grupos, broadcast y la distincion entre `sock.group` y `sock.groups`."""

import pytest

from django_socket import broadcast, group_size
from django_socket.groups import MemoryLayer
from django_socket.websocket import WebSocket


def nuevo_sock(transporte, **kwargs):
    from django_socket import groups

    t = transporte(**kwargs)
    return WebSocket(t.scope, t.receive, t.send, layer=groups.get_layer()), t


async def test_join_y_broadcast_llega_a_todos(transporte):
    a, ta = nuevo_sock(transporte)
    b, tb = nuevo_sock(transporte)
    await a.join("sala")
    await b.join("sala")

    await a.broadcast({"hola": 1})
    await a.drain(); await b.drain()

    assert ta.textos[-1] == '{"hola": 1}'
    assert tb.textos[-1] == '{"hola": 1}'


async def test_exclude_self(transporte):
    a, ta = nuevo_sock(transporte)
    b, tb = nuevo_sock(transporte)
    await a.join("sala")
    await b.join("sala")

    await a.broadcast("solo para b", exclude_self=True)
    await b.drain()

    assert "solo para b" not in ta.textos
    assert tb.textos[-1] == "solo para b"


async def test_grupos_aislados(transporte):
    a, ta = nuevo_sock(transporte)
    b, tb = nuevo_sock(transporte)
    await a.join("uno")
    await b.join("dos")

    await a.broadcast("privado")

    assert tb.textos == []


async def test_primer_join_fija_el_grupo_por_defecto(transporte):
    sock, _ = nuevo_sock(transporte)
    await sock.join("primero", "segundo")
    assert sock.group == "primero"
    assert sock.groups == {"primero", "segundo"}


async def test_to_manda_a_otro_grupo(transporte):
    a, _ = nuevo_sock(transporte)
    b, tb = nuevo_sock(transporte)
    await a.join("mio")
    await b.join("ajeno")

    await a.broadcast("cruzado", to="ajeno")
    await b.drain()

    assert tb.textos[-1] == "cruzado"


async def test_broadcast_sin_grupo_explica_que_hacer(transporte):
    sock, _ = nuevo_sock(transporte)
    with pytest.raises(ValueError) as exc:
        await sock.broadcast("nada")

    mensaje = str(exc.value)
    assert "sock.join" in mensaje and "to=" in mensaje and "group=" in mensaje


async def test_leave_rota_el_grupo_por_defecto(transporte):
    sock, _ = nuevo_sock(transporte)
    await sock.join("uno", "dos")
    await sock.leave("uno")
    assert sock.group == "dos"
    assert sock.groups == {"dos"}


async def test_al_desconectar_sale_de_los_grupos_pero_conserva_el_destino(transporte):
    """
    El patron `avisar de la salida tras el bucle` depende de esto: al cerrar
    dejas de ser miembro, pero broadcast() sigue sabiendo a donde apuntar.
    """
    sock, _ = nuevo_sock(transporte)
    otro, t_otro = nuevo_sock(transporte)
    await sock.join("sala")
    await otro.join("sala")

    sock.transporte = None
    await sock.close()

    assert sock.groups == frozenset()      # ya no es miembro
    assert sock.group == "sala"            # pero el destino sigue ahi
    assert await group_size("sala") == 1   # y no se le manda nada mas

    await sock.broadcast("me voy")
    await otro.drain()
    assert t_otro.textos[-1] == "me voy"


async def test_el_socket_muerto_se_descarta_del_grupo(transporte):
    """
    Si escribir falla, ese socket sale del grupo sin arrastrar a los demas.

    El fallo aparece ahora en la tarea escritora, no en el reparto: `broadcast`
    ya no espera a nadie.
    """
    vivo, t_vivo = nuevo_sock(transporte)
    muerto, _ = nuevo_sock(transporte)
    await vivo.join("sala")
    await muerto.join("sala")

    async def revienta(_):
        raise ConnectionResetError("el cliente se fue")

    muerto._send = revienta

    await vivo.broadcast("sigue")
    await vivo.drain()
    await muerto.drain()

    assert t_vivo.textos[-1] == "sigue"
    assert await group_size("sala") == 1


async def test_broadcast_desde_fuera_de_un_handler(transporte):
    sock, t = nuevo_sock(transporte)
    await sock.join("avisos")

    await broadcast({"aviso": "mantenimiento"}, to="avisos")
    await sock.drain()

    assert t.textos[-1] == '{"aviso": "mantenimiento"}'


async def test_grupo_vacio_se_borra():
    capa = MemoryLayer()

    class Falso:
        async def send(self, data): ...

    s = Falso()
    await capa.add("efimero", s)
    await capa.discard("efimero", s)
    assert capa._groups == {}


async def test_broadcast_a_grupo_inexistente_no_falla():
    await broadcast("al vacio", to="nadie")   # no debe lanzar
