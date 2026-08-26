"""Registro de rutas, conversores y validacion de `group=`."""

import pytest

from django_socket import ws
from django_socket.routing import get_routes, resolve


def test_ruta_simple():
    @ws("echo/")
    async def handler(sock): ...

    ruta, kwargs = resolve("/echo/")
    assert ruta.handler is handler
    assert kwargs == {}


def test_conversores_de_django():
    @ws("partida/<int:pk>/jugador/<slug:nick>/")
    async def handler(sock, pk, nick): ...

    _, kwargs = resolve("/partida/42/jugador/ramon-g/")
    assert kwargs == {"pk": 42, "nick": "ramon-g"}
    assert isinstance(kwargs["pk"], int)   # convertido, no str


def test_conversor_que_no_casa_no_resuelve():
    @ws("n/<int:pk>/")
    async def handler(sock, pk): ...

    assert resolve("/n/abc/") is None


def test_ruta_inexistente():
    @ws("a/")
    async def handler(sock): ...

    assert resolve("/b/") is None


def test_barra_inicial_es_indiferente():
    @ws("/con-barra/")
    async def handler(sock): ...

    assert resolve("/con-barra/") is not None


def test_handler_sincrono_se_rechaza_con_explicacion():
    with pytest.raises(TypeError) as exc:
        @ws("sync/")
        def handler(sock): ...

    mensaje = str(exc.value)
    assert "async def" in mensaje
    assert "sync_to_async" in mensaje      # dice que hacer, no solo que fallo


def test_ruta_duplicada_se_rechaza():
    @ws("dup/")
    async def primero(sock): ...

    with pytest.raises(ValueError) as exc:
        @ws("dup/")
        async def segundo(sock): ...

    assert "primero" in str(exc.value)     # nombra al que ya la tenia


def test_group_valida_sus_parametros_al_importar():
    """Un group= mal escrito debe fallar ya, no en la primera conexion."""
    with pytest.raises(ValueError) as exc:
        @ws("chat/<str:room>/", group="sala:{sala}")
        async def handler(sock, room): ...

    mensaje = str(exc.value)
    assert "sala" in mensaje and "room" in mensaje


def test_group_correcto_se_registra():
    @ws("chat/<str:room>/", group="room:{room}")
    async def handler(sock, room): ...

    assert get_routes()[0].group == "room:{room}"


def test_auth_por_defecto_y_desactivado():
    @ws("con/")
    async def a(sock): ...

    @ws("sin/", auth=False)
    async def b(sock): ...

    assert resolve("/con/")[0].auth is True
    assert resolve("/sin/")[0].auth is False


def test_nombre_por_defecto_es_el_del_handler():
    @ws("x/")
    async def mi_handler(sock): ...

    assert get_routes()[0].name == "mi_handler"


def test_el_decorador_devuelve_el_handler():
    """@ws no debe envolver: la funcion sigue siendo llamable e inspeccionable."""
    async def original(sock): ...

    devuelto = ws("z/")(original)
    assert devuelto is original


def test_primera_ruta_que_casa_gana():
    @ws("a/<str:x>/")
    async def generico(sock, x): ...

    @ws("a/fijo/")
    async def especifico(sock): ...

    ruta, kwargs = resolve("/a/fijo/")
    assert ruta.handler is generico        # se registro antes
    assert kwargs == {"x": "fijo"}
