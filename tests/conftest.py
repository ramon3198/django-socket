"""Configuracion de la suite y el transporte falso.

Los tests no levantan ningun servidor: hablan el protocolo ASGI directamente
contra `FakeTransport`, que es el lado servidor de una conexion websocket.
"""

from __future__ import annotations

import asyncio

import pytest

# Los settings los carga pytest-django desde DJANGO_SETTINGS_MODULE
# (ver [tool.pytest.ini_options] en pyproject.toml).


@pytest.fixture(autouse=True)
def rutas_limpias():
    """Cada test parte de un registro de rutas vacio."""
    from django_socket import routing

    previas = routing.get_routes()
    routing.clear_routes()
    yield
    routing.clear_routes()
    for r in previas:
        routing._routes.append(r)


@pytest.fixture(autouse=True)
def capa_limpia():
    """Y de una capa de difusion nueva, para que no se filtren miembros."""
    from django_socket import groups

    groups.set_layer(groups.MemoryLayer())
    yield
    groups.set_layer(None)


# --------------------------------------------------------------- transporte


class FakeTransport:
    """
    El lado servidor de una conexion websocket ASGI, sin red.

        t = FakeTransport()
        t.cliente_conecta()
        t.cliente_envia("hola")
        await handler(...)          # consume de t.receive, escribe en t.enviados
    """

    def __init__(self, path: str = "/test/", headers=None, query: str = "", **extra):
        cabeceras = {"origin": "http://testserver", **(headers or {})}
        self.scope = {
            "type": "websocket",
            "path": path,
            "raw_path": path.encode(),
            "query_string": query.encode(),
            "headers": [
                (k.encode(), v.encode())
                for k, v in cabeceras.items()
                if v is not None
            ],
            "subprotocols": [],
            "client": ("127.0.0.1", 55555),
            **extra,
        }
        self._entrantes: asyncio.Queue = asyncio.Queue()
        self.enviados: list[dict] = []

    # --- lo que consume la libreria -------------------------------------

    async def receive(self) -> dict:
        return await self._entrantes.get()

    async def send(self, message: dict) -> None:
        self.enviados.append(message)

    # --- lo que dirige el test ------------------------------------------

    def cliente_conecta(self):
        self._entrantes.put_nowait({"type": "websocket.connect"})
        return self

    def cliente_envia(self, texto: str = None, datos: bytes = None):
        evento = {"type": "websocket.receive"}
        if texto is not None:
            evento["text"] = texto
        if datos is not None:
            evento["bytes"] = datos
        self._entrantes.put_nowait(evento)
        return self

    def cliente_cierra(self, code: int = 1000):
        self._entrantes.put_nowait({"type": "websocket.disconnect", "code": code})
        return self

    # --- lo que comprueba el test ---------------------------------------

    @property
    def tipos(self) -> list[str]:
        return [e["type"] for e in self.enviados]

    @property
    def textos(self) -> list[str]:
        return [e["text"] for e in self.enviados if "text" in e]

    @property
    def acepto(self) -> bool:
        return "websocket.accept" in self.tipos

    @property
    def cierre(self) -> dict | None:
        for e in reversed(self.enviados):
            if e["type"] == "websocket.close":
                return e
        return None


@pytest.fixture
def transporte():
    return FakeTransport


@pytest.fixture
def sock(transporte):
    """Un WebSocket ya conectado a un transporte falso, listo para usar."""
    from django_socket import groups
    from django_socket.websocket import WebSocket

    t = transporte()
    s = WebSocket(t.scope, t.receive, t.send, layer=groups.get_layer())
    s.transporte = t  # para que el test llegue a los eventos
    return s
