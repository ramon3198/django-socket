"""El dispatcher de punta a punta: routing, group=, auth, errores y lifespan."""

import pytest
from django.test import override_settings

from django_socket import dispatch, ws
from django_socket.dispatch import CLOSE_NO_ROUTE, CLOSE_SERVER_ERROR


async def correr(t):
    """Ejecuta el dispatcher contra un transporte falso ya cargado."""
    await dispatch.handle_websocket(t.scope, t.receive, t.send)
    return t


# ------------------------------------------------------------------- routing


async def test_ruta_encontrada_se_ejecuta(transporte):
    @ws("saluda/", auth=False)
    async def handler(sock):
        await sock.send_text("hola")

    t = await correr(transporte(path="/saluda/").cliente_conecta().cliente_cierra())
    assert t.textos == ["hola"]


async def test_parametros_llegan_al_handler(transporte):
    vistos = {}

    @ws("p/<int:pk>/<str:nombre>/", auth=False)
    async def handler(sock, pk, nombre):
        vistos.update(pk=pk, nombre=nombre, params=sock.path_params)

    await correr(transporte(path="/p/7/ramon/").cliente_conecta().cliente_cierra())
    assert vistos["pk"] == 7 and vistos["nombre"] == "ramon"
    assert vistos["params"] == {"pk": 7, "nombre": "ramon"}


async def test_sin_ruta_cierra_con_4404(transporte):
    t = await correr(transporte(path="/no-existe/").cliente_conecta())
    assert t.cierre["code"] == CLOSE_NO_ROUTE


async def test_sin_evento_connect_no_hace_nada(transporte):
    """Si el primer evento no es websocket.connect, salimos sin tocar nada."""
    t = transporte()
    t._entrantes.put_nowait({"type": "websocket.receive", "text": "raro"})
    await correr(t)
    assert t.enviados == []


# ---------------------------------------------------------------- group=


async def test_group_hace_join_automatico(transporte):
    visto = {}

    @ws("sala/<str:nombre>/", group="g:{nombre}", auth=False)
    async def handler(sock, nombre):
        visto["group"] = sock.group
        visto["groups"] = set(sock.groups)

    await correr(transporte(path="/sala/siete/").cliente_conecta().cliente_cierra())
    assert visto == {"group": "g:siete", "groups": {"g:siete"}}


async def test_broadcast_tras_el_bucle_sigue_funcionando(transporte):
    """El aviso de salida es el caso que mas facil se rompe."""
    @ws("sala/<str:n>/", group="g:{n}", auth=False)
    async def handler(sock, n):
        async for msg in sock:
            await sock.broadcast(f"dice: {msg.text}")
        await sock.broadcast("se fue")

    import asyncio

    a = transporte(path="/sala/uno/").cliente_conecta()
    b = transporte(path="/sala/uno/").cliente_conecta()
    tarea_a = asyncio.create_task(correr(a))
    await asyncio.sleep(0)                  # deja que A entre al grupo
    b.cliente_envia("hola").cliente_cierra()
    await correr(b)
    a.cliente_cierra()
    await tarea_a

    assert "dice: hola" in a.textos
    assert "se fue" in a.textos


# ---------------------------------------------------------------- errores


async def test_excepcion_en_el_handler_cierra_con_1011(transporte, caplog):
    @ws("boom/", auth=False)
    async def handler(sock):
        raise RuntimeError("algo se rompio")

    t = await correr(transporte(path="/boom/").cliente_conecta())
    assert t.cierre["code"] == CLOSE_SERVER_ERROR
    assert "algo se rompio" in caplog.text     # queda en el log, no se traga


async def test_desconexion_del_cliente_no_es_error(transporte, caplog):
    """
    Que el cliente se vaya es la salida normal: ni traza en el log, ni un
    websocket.close de vuelta hacia una conexion que ya no existe.
    """
    visto = {}

    @ws("normal/", auth=False)
    async def handler(sock):
        async for _ in sock:
            pass
        visto["code"] = sock.close_code

    t = await correr(
        transporte(path="/normal/").cliente_conecta().cliente_cierra(code=1001)
    )
    assert "Traceback" not in caplog.text
    assert visto["code"] == 1001        # el handler ve por que se fue
    assert t.cierre is None             # y no le contestamos al vacio
    assert t.acepto


async def test_el_handler_puede_rechazar_antes_de_aceptar(transporte):
    @ws("privado/", auth=False)
    async def handler(sock):
        await sock.deny()

    t = await correr(transporte(path="/privado/").cliente_conecta())
    assert not t.acepto and t.cierre["code"] == 403


# ------------------------------------------------------------------- origen


@pytest.mark.parametrize(
    "origin, permitido",
    [
        ("http://testserver", True),        # en ALLOWED_HOSTS
        ("https://miapp.com", True),
        ("https://miapp.com:8443", True),   # el puerto no cuenta
        ("http://atacante.example", False),
        ("null", False),
        ("", False),
    ],
)
async def test_origen_se_valida_contra_allowed_hosts(transporte, origin, permitido):
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send_text("dentro")

    t = await correr(
        transporte(path="/x/", headers={"origin": origin}).cliente_conecta().cliente_cierra()
    )
    assert ("dentro" in t.textos) is permitido
    if not permitido:
        assert not t.acepto                 # rechazado en el handshake
        assert t.cierre["code"] == 403


async def test_sin_origin_se_acepta_por_defecto(transporte):
    """Los navegadores siempre mandan Origin; omitirlo es cosa de clientes nativos."""
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send_text("dentro")

    t = transporte(path="/x/", headers={"origin": None}).cliente_conecta().cliente_cierra()
    await correr(t)
    assert "dentro" in t.textos


@override_settings(DJANGO_SOCKET={"REQUIRE_ORIGIN": True})
async def test_require_origin_rechaza_los_que_no_lo_mandan(transporte):
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send_text("dentro")

    t = transporte(path="/x/", headers={"origin": None}).cliente_conecta()
    await correr(t)
    assert t.cierre["code"] == 403


@override_settings(DJANGO_SOCKET={"ALLOWED_ORIGINS": ["https://solo-este.com"]})
async def test_allowed_origins_explicito_ignora_allowed_hosts(transporte):
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send_text("dentro")

    bueno = transporte(
        path="/x/", headers={"origin": "https://solo-este.com"}
    ).cliente_conecta().cliente_cierra()
    malo = transporte(
        path="/x/", headers={"origin": "http://testserver"}
    ).cliente_conecta()

    await correr(bueno)
    await correr(malo)
    assert "dentro" in bueno.textos
    assert malo.cierre["code"] == 403


@override_settings(DJANGO_SOCKET={"ALLOWED_ORIGINS": ["*"]})
async def test_comodin_acepta_todo(transporte):
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send_text("dentro")

    t = transporte(
        path="/x/", headers={"origin": "http://cualquiera.example"}
    ).cliente_conecta().cliente_cierra()
    await correr(t)
    assert "dentro" in t.textos


@override_settings(ALLOWED_HOSTS=[".miapp.com"])
async def test_comodin_de_subdominio(transporte):
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send_text("dentro")

    for origin in ("https://api.miapp.com", "https://miapp.com"):
        t = transporte(
            path="/x/", headers={"origin": origin}
        ).cliente_conecta().cliente_cierra()
        await correr(t)
        assert "dentro" in t.textos, origin


# ----------------------------------------------------------------- lifespan


async def test_lifespan_arranca_y_para_la_capa():
    eventos = []
    entrantes = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]

    async def receive():
        return entrantes.pop(0)

    async def send(msg):
        eventos.append(msg["type"])

    await dispatch.handle_lifespan({"type": "lifespan"}, receive, send)
    assert eventos == ["lifespan.startup.complete", "lifespan.shutdown.complete"]


async def test_lifespan_reporta_un_fallo_de_arranque(monkeypatch):
    from django_socket import groups

    class CapaRota(groups.MemoryLayer):
        async def startup(self):
            raise RuntimeError("redis no responde")

    groups.set_layer(CapaRota())
    dispatch._layer_started = False

    eventos = []

    async def receive():
        return {"type": "lifespan.startup"}

    async def send(msg):
        eventos.append(msg)

    await dispatch.handle_lifespan({"type": "lifespan"}, receive, send)
    assert eventos[0]["type"] == "lifespan.startup.failed"
    assert "redis no responde" in eventos[0]["message"]


@override_settings(ALLOWED_HOSTS=["*"])
async def test_asterisco_en_allowed_hosts_acepta_cualquier_origen(transporte):
    """Es lo que hace Django con ALLOWED_HOSTS=['*']; aqui replicamos su semantica."""
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send_text("dentro")

    t = transporte(
        path="/x/", headers={"origin": "http://loquesea.example"}
    ).cliente_conecta().cliente_cierra()
    await correr(t)
    assert "dentro" in t.textos


@override_settings(ALLOWED_HOSTS=["*.miapp.com"])
async def test_patron_estrella_punto(transporte):
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send_text("dentro")

    for origin, esperado in [
        ("https://api.miapp.com", True),
        ("https://miapp.com", True),
        ("https://miapp.com.atacante.net", False),   # no debe colar
    ]:
        t = transporte(
            path="/x/", headers={"origin": origin}
        ).cliente_conecta().cliente_cierra()
        await correr(t)
        assert ("dentro" in t.textos) is esperado, origin


@override_settings(ALLOWED_HOSTS=[], CSRF_TRUSTED_ORIGINS=["https://front.miapp.com"])
async def test_csrf_trusted_origins_tambien_vale(transporte):
    @ws("x/", auth=False)
    async def handler(sock):
        await sock.send_text("dentro")

    t = transporte(
        path="/x/", headers={"origin": "https://front.miapp.com"}
    ).cliente_conecta().cliente_cierra()
    await correr(t)
    assert "dentro" in t.textos
