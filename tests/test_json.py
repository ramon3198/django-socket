"""El camino JSON: serializacion, errores del cliente y enrutado por tipo."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from django.utils.translation import gettext_lazy

from django_socket import Events, InvalidJSON, dispatch, ws
from django_socket.dispatch import CLOSE_BAD_DATA, CLOSE_SERVER_ERROR
from django_socket.websocket import Message, WebSocket


def nuevo(transporte, **kwargs):
    from django_socket import groups

    t = transporte(**kwargs)
    return WebSocket(t.scope, t.receive, t.send, layer=groups.get_layer()), t


async def correr(t):
    await dispatch.handle_websocket(t.scope, t.receive, t.send)
    return t


# ------------------------------------------------------------- serializacion


@pytest.mark.parametrize(
    "valor, esperado",
    [
        (datetime(2026, 8, 26, 10, 30), '"2026-08-26T10:30:00"'),
        (date(2026, 8, 26), '"2026-08-26"'),
        (Decimal("9.99"), '"9.99"'),                    # cadena: sin perder precision
        (UUID(int=1), '"00000000-0000-0000-0000-000000000001"'),
        (timedelta(seconds=90), '"P0DT00H01M30S"'),   # duration_iso_string de Django
    ],
)
async def test_tipos_de_django_se_serializan_solos(transporte, valor, esperado):
    sock, t = nuevo(transporte)
    await sock.send_json({"v": valor})
    assert t.textos[-1] == '{"v": %s}' % esperado


async def test_la_fecha_sale_en_iso_no_en_str(transporte):
    """
    ISO-8601 es el unico formato que la spec de ECMAScript obliga a `Date` a
    parse_rate; el de str() depende del motor (V8 lo acepta, otros no siempre).
    """
    sock, t = nuevo(transporte)
    await sock.send_json({"cuando": datetime(2026, 8, 26, 10, 30)})
    assert "2026-08-26T10:30:00" in t.textos[-1]
    assert "2026-08-26 10:30:00" not in t.textos[-1]


async def test_los_microsegundos_se_truncan_a_milisegundos(transporte):
    """`Date` de JS solo llega al milisegundo; str() emitia 6 digitos."""
    from datetime import timezone as tz

    sock, t = nuevo(transporte)
    momento = datetime(2026, 8, 26, 19, 43, 30, 251057, tzinfo=tz.utc)
    await sock.send_json({"cuando": momento})
    assert t.textos[-1] == '{"cuando": "2026-08-26T19:43:30.251Z"}'
    assert "251057" not in t.textos[-1]


async def test_las_cadenas_lazy_de_traduccion_funcionan(transporte):
    sock, t = nuevo(transporte)
    await sock.send_json({"msg": gettext_lazy("hola")})
    assert t.textos[-1] == '{"msg": "hola"}'


async def test_un_objeto_raro_falla_diciendo_que_hacer(transporte):
    """Mejor un error ruidoso que mandar 'Usuario object (3)' al navegador."""
    class Cualquiera:
        pass

    sock, _ = nuevo(transporte)
    with pytest.raises(TypeError) as exc:
        await sock.send_json({"x": Cualquiera()})

    mensaje = str(exc.value)
    assert "Cualquiera" in mensaje
    assert "default=str" in mensaje          # ofrece la salida de emergencia


async def test_se_puede_volver_al_comportamiento_permisivo(transporte):
    class Cualquiera:
        def __str__(self):
            return "yo"

    sock, t = nuevo(transporte)
    await sock.send_json({"x": Cualquiera()}, default=str)
    assert t.textos[-1] == '{"x": "yo"}'


async def test_send_con_dict_usa_el_mismo_codificador(transporte):
    """sock.send({...}) y sock.send_json({...}) deben coincidir."""
    sock, t = nuevo(transporte)
    await sock.send({"cuando": datetime(2026, 1, 2, 3, 4)})
    assert t.textos[-1] == '{"cuando": "2026-01-02T03:04:00"}'


# --------------------------------------------------------- JSON del cliente


@pytest.mark.parametrize("crudo", ["{roto", "hola", "", "[1,2", "{'comillas': 1}"])
def test_json_malo_lanza_invalid_json(crudo):
    with pytest.raises(InvalidJSON):
        Message(text=crudo).json()


def test_invalid_json_es_una_value_error():
    """Para que `except ValueError` de toda la vida siga sirviendo."""
    assert issubclass(InvalidJSON, ValueError)


def test_invalid_json_recorta_el_frame_en_el_mensaje():
    with pytest.raises(InvalidJSON) as exc:
        Message(text="x" * 500).json()
    assert len(str(exc.value)) < 160
    assert exc.value.raw == "x" * 500      # el original sigue disponible


def test_bytes_que_no_son_utf8():
    with pytest.raises(InvalidJSON) as exc:
        Message(data=b"\xff\xfe").json()
    assert "UTF-8" in str(exc.value)


async def test_json_invalido_cierra_con_4400_y_no_con_1011(transporte, caplog):
    """
    Que un cliente mande basura no es un fallo del servidor: ni 1011 ni
    traceback en el log.
    """
    @ws("j/", auth=False)
    async def handler(sock):
        async for msg in sock:
            await sock.send_json(msg.json())

    t = transporte(path="/j/").cliente_conecta()
    t.cliente_envia("{esto no es json")
    await correr(t)

    assert t.cierre["code"] == CLOSE_BAD_DATA
    assert t.cierre["reason"] == "Invalid JSON"
    assert "Traceback" not in caplog.text
    assert "Invalid JSON from the client" in caplog.text


async def test_un_error_del_servidor_sigue_siendo_1011(transporte):
    """El 4400 es solo para el cliente; los bugs propios no se disfrazan."""
    @ws("j/", auth=False)
    async def handler(sock):
        raise RuntimeError("bug mio")

    t = await correr(transporte(path="/j/").cliente_conecta())
    assert t.cierre["code"] == CLOSE_SERVER_ERROR


async def test_receive_json_tambien(transporte):
    sock, t = nuevo(transporte)
    t.cliente_envia('{"a": [1, 2]}')
    assert await sock.receive_json() == {"a": [1, 2]}


async def test_ida_y_vuelta_completa(transporte):
    """Lo que el cliente pregunto: mandar JSON, recibirlo y contestar JSON."""
    @ws("eco/", auth=False)
    async def handler(sock):
        async for datos in sock.iter_json():
            await sock.send_json({"vino": datos, "n": len(datos)})

    t = transporte(path="/eco/").cliente_conecta()
    t.cliente_envia('{"nombre": "ramon", "edad": 30}').cliente_cierra()
    await correr(t)

    import json

    assert json.loads(t.textos[0]) == {
        "vino": {"nombre": "ramon", "edad": 30},
        "n": 2,
    }


# ----------------------------------------------------------------- Events


async def test_despacha_por_tipo(transporte):
    vistos = []
    ev = Events()

    @ev.on("mensaje")
    async def mensaje(sock, datos):
        vistos.append(("mensaje", datos))

    @ev.on("escribiendo")
    async def escribiendo(sock):
        vistos.append(("escribiendo", None))

    @ws("c/", auth=False)
    async def handler(sock):
        await ev.run(sock)

    t = transporte(path="/c/").cliente_conecta()
    t.cliente_envia('{"type": "mensaje", "texto": "hola"}')
    t.cliente_envia('{"type": "escribiendo"}').cliente_cierra()
    await correr(t)

    assert vistos == [("mensaje", {"texto": "hola"}), ("escribiendo", None)]


async def test_el_campo_type_no_llega_en_los_datos(transporte):
    recibido = {}
    ev = Events()

    @ev.on("x")
    async def x(sock, datos):
        recibido.update(datos)

    sock, _ = nuevo(transporte)
    await ev.handle(sock, {"type": "x", "a": 1})
    assert recibido == {"a": 1}


async def test_comodin_para_lo_que_no_case(transporte):
    vistos = []
    ev = Events()

    @ev.on("conocido")
    async def conocido(sock, datos):
        vistos.append("conocido")

    @ev.on("*")
    async def resto(sock, datos):
        vistos.append(f"resto:{datos}")

    sock, _ = nuevo(transporte)
    await ev.handle(sock, {"type": "conocido"})
    await ev.handle(sock, {"type": "raro", "a": 1})
    assert vistos == ["conocido", "resto:{'a': 1}"]


async def test_tipo_desconocido_se_ignora_por_defecto(transporte, caplog):
    ev = Events()

    @ev.on("solo-este")
    async def h(sock): ...

    sock, _ = nuevo(transporte)
    await ev.handle(sock, {"type": "otro"})     # no debe lanzar
    assert "nothing handles" in caplog.text
    assert "solo-este" in caplog.text   # dice cuales SI hay


async def test_strict_cierra_ante_un_tipo_desconocido(transporte):
    ev = Events(strict=True)

    @ev.on("solo-este")
    async def h(sock): ...

    @ws("c/", auth=False)
    async def handler(sock):
        await ev.run(sock)

    t = transporte(path="/c/").cliente_conecta()
    t.cliente_envia('{"type": "inventado"}')
    await correr(t)
    assert t.cierre["code"] == CLOSE_BAD_DATA


async def test_campo_personalizado(transporte):
    vistos = []
    ev = Events(key="action")

    @ev.on("borrar")
    async def borrar(sock, datos):
        vistos.append(datos)

    sock, _ = nuevo(transporte)
    await ev.handle(sock, {"action": "borrar", "id": 7})
    assert vistos == [{"id": 7}]


async def test_un_mensaje_que_no_es_objeto_es_error_del_cliente(transporte):
    ev = Events()
    sock, _ = nuevo(transporte)
    with pytest.raises(InvalidJSON):
        await ev.handle(sock, [1, 2, 3])


def test_registrar_dos_veces_el_mismo_tipo_se_rechaza():
    ev = Events()

    @ev.on("x")
    async def primero(sock): ...

    with pytest.raises(ValueError) as exc:
        @ev.on("x")
        async def segundo(sock): ...

    assert "primero" in str(exc.value)


def test_varios_tipos_en_un_decorador():
    ev = Events()

    @ev.on("entrar", "salir")
    async def mover(sock, datos): ...

    assert ev.types == ["entrar", "salir"]


def test_handler_sincrono_se_rechaza():
    ev = Events()
    with pytest.raises(TypeError) as exc:
        @ev.on("x")
        def sincrono(sock): ...
    assert "async def" in str(exc.value)


def test_firma_rara_se_rechaza_al_registrarse():
    ev = Events()
    with pytest.raises(TypeError) as exc:
        @ev.on("x")
        async def demasiados(sock, datos, extra): ...
    assert "(sock, data)" in str(exc.value)


def test_on_sin_tipos_se_rechaza():
    ev = Events()
    with pytest.raises(TypeError):
        ev.on()
