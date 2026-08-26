"""Cliente de pruebas: testea tus handlers sin levantar ningun servidor.

    from django_socket.testing import WebSocketClient

    async def test_el_chat_reparte():
        async with WebSocketClient("/chat/general/") as a, \\
                   WebSocketClient("/chat/general/") as b:
            await b.send_json({"type": "mensaje", "texto": "hola"})
            assert (await a.receive_json())["texto"] == "hola"

Habla el protocolo ASGI directamente contra el dispatcher, asi que pasa por el
mismo camino que una conexion real -- ruta, conversores, validacion de origen,
sesion, grupos -- pero sin sockets, sin puertos y en milisegundos.

Todos los `receive` llevan timeout: un test que espera algo que no llega falla
en un segundo en vez de colgarse.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .websocket import InvalidJSON, Message, WebSocketDisconnect

TIMEOUT = 1.0


class TimeoutDelEsperado(AssertionError):
    """No llego nada en el plazo. Es AssertionError para que lea como un fallo de test."""


class WebSocketClient:
    """
    Un cliente websocket de mentira contra el dispatcher de verdad.

    `path`         la ruta, tal cual la escribiria un navegador ("/chat/x/")
    `user`         un User: se le crea sesion y `sock.user` lo vera autenticado
    `headers`      cabeceras extra (`origin` ya va puesto)
    `cookies`      dict de cookies; se combina con la sesion de `user`
    `query`        "token=abc&n=3"
    `subprotocols` los que ofreceria el navegador
    """

    def __init__(
        self,
        path: str,
        *,
        user=None,
        headers: dict | None = None,
        cookies: dict | None = None,
        query: str = "",
        subprotocols=(),
        origin: str = "http://testserver",
    ):
        self.path = path
        self.user = user
        self._headers = dict(headers or {})
        self._cookies = dict(cookies or {})
        self._query = query
        self._subprotocols = list(subprotocols)
        if origin is not None:
            self._headers.setdefault("origin", origin)

        self._al_servidor: asyncio.Queue = asyncio.Queue()
        self._al_cliente: asyncio.Queue = asyncio.Queue()
        self._buffer: list[dict] = []      # lo que ya sacamos de la cola
        self._tarea: asyncio.Task | None = None

        self.accepted = False
        self.subprotocol: str | None = None
        self.close_code: int | None = None
        self.close_reason: str = ""

    @property
    def connected(self) -> bool:
        """
        Si tienes una conexion usable.

        Distinto de `accepted`, que dice literalmente si el servidor mando un
        `websocket.accept`. Un rechazo con codigo (4404, 4401...) se ve en el
        protocolo como accept + close, porque cerrar sin aceptar dejaria al
        navegador con un 1006 sin motivo. Para "¿me dejo entrar?" usa este.
        """
        return self.accepted and self.close_code is None

    # ------------------------------------------------------------ ciclo de vida

    async def connect(self, timeout: float = TIMEOUT) -> "WebSocketClient":
        """
        Abre la conexion y espera la respuesta al handshake.

        No lanza si el servidor rechaza: mira `connected` y `close_code`, que
        es lo que quieres afirmar cuando pruebas un rechazo.
        """
        if self.user is not None:
            self._cookies.setdefault(
                await _nombre_cookie_sesion(), await _crear_sesion(self.user)
            )
        if self._cookies:
            self._headers["cookie"] = "; ".join(
                f"{k}={v}" for k, v in self._cookies.items()
            )

        from . import dispatch

        self._al_servidor.put_nowait({"type": "websocket.connect"})
        self._tarea = asyncio.create_task(
            dispatch.handle_websocket(self._scope(), self._recibir, self._enviar)
        )

        evento = await self._siguiente(timeout, que="la respuesta al handshake")
        if evento["type"] == "websocket.accept":
            self.accepted = True
            self.subprotocol = evento.get("subprotocol")
            await self._mirar_si_cierra_enseguida()
        else:
            self._anotar_cierre(evento)
        return self

    async def _mirar_si_cierra_enseguida(self) -> None:
        """
        Deja avanzar al handler y anota el cierre si ya viene de camino, para
        que `connected` diga la verdad nada mas volver de connect().

        Lo que se saca de la cola se guarda en el buffer, asi que `receive()`
        lo sigue viendo en orden: no se pierde ni un mensaje ni el cierre.
        """
        for _ in range(3):
            await asyncio.sleep(0)
        while not self._al_cliente.empty():
            evento = self._al_cliente.get_nowait()
            self._buffer.append(evento)
            if evento["type"] == "websocket.close":
                self._anotar_cierre(evento)
                return

    async def disconnect(self, code: int = 1000) -> None:
        """El cliente se va. Espera a que el handler termine su limpieza."""
        if self._tarea is None or self._tarea.done():
            return
        self._al_servidor.put_nowait({"type": "websocket.disconnect", "code": code})
        try:
            await asyncio.wait_for(asyncio.shield(self._tarea), timeout=TIMEOUT)
        except asyncio.TimeoutError:
            self._tarea.cancel()
        self._vaciar_cierres()

    async def __aenter__(self) -> "WebSocketClient":
        return await self.connect()

    async def __aexit__(self, *exc) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------ enviar

    async def send(self, data: Any) -> None:
        """str -> texto, bytes -> binario, cualquier otra cosa -> JSON."""
        if isinstance(data, str):
            await self.send_text(data)
        elif isinstance(data, (bytes, bytearray)):
            await self.send_bytes(bytes(data))
        else:
            await self.send_json(data)

    async def send_text(self, text: str) -> None:
        self._al_servidor.put_nowait({"type": "websocket.receive", "text": text})
        await asyncio.sleep(0)      # deja correr al handler

    async def send_bytes(self, data: bytes) -> None:
        self._al_servidor.put_nowait({"type": "websocket.receive", "bytes": data})
        await asyncio.sleep(0)

    async def send_json(self, data: Any) -> None:
        import json

        await self.send_text(json.dumps(data, default=str))

    # ----------------------------------------------------------------- recibir

    async def receive(self, timeout: float = TIMEOUT) -> Message:
        """
        El siguiente mensaje. Lanza `WebSocketDisconnect` si el servidor cierra
        y `TimeoutDelEsperado` si no llega nada.
        """
        evento = await self._siguiente(timeout, que="un mensaje")
        if evento["type"] == "websocket.close":
            self._anotar_cierre(evento)
            raise WebSocketDisconnect(self.close_code, self.close_reason)
        return Message(text=evento.get("text"), data=evento.get("bytes"))

    async def receive_text(self, timeout: float = TIMEOUT) -> str:
        msg = await self.receive(timeout)
        if msg.text is None:
            raise AssertionError("Se esperaba texto y llego un frame binario.")
        return msg.text

    async def receive_bytes(self, timeout: float = TIMEOUT) -> bytes:
        msg = await self.receive(timeout)
        if msg.bytes is None:
            raise AssertionError("Se esperaba binario y llego un frame de texto.")
        return msg.bytes

    async def receive_json(self, timeout: float = TIMEOUT) -> Any:
        return (await self.receive(timeout)).json()

    async def receive_all(self, timeout: float = 0.1) -> list[Message]:
        """Todo lo que haya pendiente ahora mismo. Util tras un broadcast."""
        mensajes = []
        while True:
            try:
                mensajes.append(await self.receive(timeout))
            except (TimeoutDelEsperado, WebSocketDisconnect):
                return mensajes

    async def receive_nothing(self, timeout: float = 0.1) -> bool:
        """True si NO llega nada. Para afirmar que un grupo esta aislado."""
        try:
            await self._siguiente(timeout, que="nada")
        except TimeoutDelEsperado:
            return True
        return False

    async def wait_closed(self, timeout: float = TIMEOUT) -> int:
        """Espera a que el servidor cierre y devuelve el codigo."""
        if self.close_code is not None:
            return self.close_code
        try:
            while True:
                evento = await self._siguiente(timeout, que="el cierre")
                if evento["type"] == "websocket.close":
                    self._anotar_cierre(evento)
                    return self.close_code
        except TimeoutDelEsperado:
            raise AssertionError(
                f"El servidor no cerro en {timeout}s (sigue abierto)."
            ) from None

    # ------------------------------------------------------------------ dentro

    def _scope(self) -> dict:
        cabeceras = [
            (k.lower().encode("latin-1"), str(v).encode("latin-1"))
            for k, v in self._headers.items()
            if v is not None
        ]
        return {
            "type": "websocket",
            "path": self.path,
            "raw_path": self.path.encode(),
            "query_string": self._query.encode(),
            "headers": cabeceras,
            "subprotocols": self._subprotocols,
            "client": ("127.0.0.1", 54321),
            "server": ("testserver", 80),
            "scheme": "ws",
        }

    async def _recibir(self) -> dict:
        return await self._al_servidor.get()

    async def _enviar(self, message: dict) -> None:
        await self._al_cliente.put(message)

    async def _siguiente(self, timeout: float, que: str) -> dict:
        if self._buffer:
            return self._buffer.pop(0)
        try:
            return await asyncio.wait_for(self._al_cliente.get(), timeout=timeout)
        except asyncio.TimeoutError:
            self._reventar_si_el_handler_murio()
            raise TimeoutDelEsperado(
                f"No llego {que} de {self.path} en {timeout}s."
            ) from None

    def _reventar_si_el_handler_murio(self) -> None:
        """Si el handler murio de verdad, ese error es mas util que el timeout."""
        if self._tarea is not None and self._tarea.done():
            exc = self._tarea.exception()
            if exc is not None:
                raise exc

    def _anotar_cierre(self, evento: dict) -> None:
        self.close_code = evento.get("code", 1000)
        self.close_reason = evento.get("reason", "")

    def _vaciar_cierres(self) -> None:
        while not self._al_cliente.empty():
            self._buffer.append(self._al_cliente.get_nowait())
        for evento in self._buffer:
            if evento["type"] == "websocket.close":
                self._anotar_cierre(evento)


# --------------------------------------------------------------------- sesion


async def _nombre_cookie_sesion() -> str:
    from django.conf import settings

    return settings.SESSION_COOKIE_NAME


async def _crear_sesion(user) -> str:
    """Deja una sesion autenticada en la BD, como haria un login de verdad."""
    from importlib import import_module

    from django.conf import settings
    from django.contrib.auth import (
        BACKEND_SESSION_KEY,
        HASH_SESSION_KEY,
        SESSION_KEY,
    )

    engine = import_module(settings.SESSION_ENGINE)
    sesion = engine.SessionStore()
    sesion[SESSION_KEY] = str(user.pk)
    sesion[BACKEND_SESSION_KEY] = settings.AUTHENTICATION_BACKENDS[0]
    sesion[HASH_SESSION_KEY] = user.get_session_auth_hash()

    if hasattr(sesion, "acreate"):
        await sesion.acreate()
    else:  # pragma: no cover - Django < 5.0
        from asgiref.sync import sync_to_async

        await sync_to_async(sesion.create)()
    return sesion.session_key


__all__ = ["WebSocketClient", "TimeoutDelEsperado", "WebSocketDisconnect", "InvalidJSON"]
