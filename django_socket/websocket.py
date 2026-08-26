"""El objeto WebSocket que recibe cada handler."""

from __future__ import annotations

import asyncio
import json
from http.cookies import SimpleCookie
from typing import Any
from urllib.parse import parse_qs


class WebSocketDisconnect(Exception):
    """El cliente cerro la conexion."""

    def __init__(self, code: int = 1000, reason: str = ""):
        self.code = code
        self.reason = reason
        super().__init__(f"WebSocket cerrado (code={code})")


class WebSocketClosed(Exception):
    """Se intento usar un socket que el servidor ya cerro."""


class InvalidJSON(ValueError):
    """
    El cliente mando algo que no es JSON valido.

    Es una ValueError, asi que `except ValueError` de toda la vida sigue
    valiendo. Existe como tipo propio para que el dispatcher la distinga de un
    fallo del servidor: culpa del cliente se cierra con 4400, no con un 1011
    que ademas te llena el log de tracebacks que no son tuyos.
    """

    def __init__(self, crudo, motivo=""):
        self.crudo = crudo
        muestra = str(crudo)
        if len(muestra) > 80:
            muestra = muestra[:77] + "..."
        super().__init__(f"JSON invalido del cliente: {muestra!r}"
                         + (f" ({motivo})" if motivo else ""))


_encoder = None


def _get_encoder():
    """
    DjangoJSONEncoder, cargado tarde para no tocar settings al importar.

    Importa porque es el que sabe de tipos de Django, y sobre todo por las
    fechas. `str(aware)` da "2026-08-26 19:43:30.251057+00:00"; el encoder da
    "2026-08-26T19:43:30.251Z". Dos diferencias que si cuentan:

    * ISO-8601 es el unico formato que la spec de ECMAScript obliga a `Date`
      a parsear. Lo demas es un fallback de cada motor -- V8 es permisivo y lo
      acepta, otros historicamente no.
    * `str()` emite microsegundos (6 digitos) y `Date` solo entiende
      milisegundos; el encoder trunca a 3, que es lo que JS puede representar.

    Y de paso `Decimal` sale como cadena para no perder precision, y `UUID` y
    las cadenas lazy de traduccion se serializan solas.
    """
    global _encoder
    if _encoder is None:
        from django.core.serializers.json import DjangoJSONEncoder

        class SocketJSONEncoder(DjangoJSONEncoder):
            def default(self, o):
                try:
                    return super().default(o)
                except TypeError:
                    raise TypeError(
                        f"No se puede enviar un {type(o).__name__} por el "
                        f"socket. Los tipos de Django habituales (datetime, "
                        f"date, time, timedelta, Decimal, UUID, cadenas lazy) "
                        f"van solos; el resto conviertelo tu: un modelo a dict, "
                        f"un QuerySet a lista. Si prefieres el comportamiento "
                        f"antiguo: sock.send_json(dato, default=str)."
                    ) from None

        _encoder = SocketJSONEncoder
    return _encoder


class Message:
    """Un mensaje entrante. Usa `.text`, `.bytes` o `.json()`."""

    __slots__ = ("text", "bytes")

    def __init__(self, text: str | None = None, data: bytes | None = None):
        self.text = text
        self.bytes = data

    def json(self, **kwargs) -> Any:
        """Parsea el mensaje como JSON. Lanza `InvalidJSON` si no lo es."""
        crudo = self.text
        if crudo is None:
            if self.bytes is None:
                raise InvalidJSON("", "mensaje vacio")
            try:
                crudo = self.bytes.decode()
            except UnicodeDecodeError as exc:
                raise InvalidJSON(self.bytes, "no es UTF-8 valido") from exc
        try:
            return json.loads(crudo, **kwargs)
        except ValueError as exc:
            raise InvalidJSON(crudo, str(exc)) from None

    @property
    def is_text(self) -> bool:
        return self.text is not None

    def __eq__(self, other) -> bool:
        """Permite `if msg == "ping"` sin sacar .text a mano."""
        if isinstance(other, str):
            return self.text == other
        if isinstance(other, (bytes, bytearray)):
            return self.bytes == bytes(other)
        if isinstance(other, Message):
            return self.text == other.text and self.bytes == other.bytes
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.text, self.bytes))

    def __str__(self) -> str:
        return self.text if self.text is not None else repr(self.bytes)

    def __repr__(self) -> str:
        preview = str(self)
        if len(preview) > 40:
            preview = preview[:37] + "..."
        return f"<Message {preview!r}>"


CONNECTING, OPEN, CLOSED = "connecting", "open", "closed"


class WebSocket:
    """
    Envoltura sobre el par (receive, send) de ASGI.

    No hace falta llamar a `accept()`: el handshake se completa solo la primera
    vez que envias, recibes o iteras. Llamalo a mano solo si necesitas fijar un
    subprotocolo o headers, y llama a `close()` de entrada para rechazar.
    """

    def __init__(self, scope, receive, send, *, layer=None):
        self.scope = scope
        self._receive = receive
        self._send = send
        self._layer = layer
        self._state = CONNECTING
        self._groups: set[str] = set()
        self.group: str | None = None   # destino por defecto de broadcast()
        self._outbox: asyncio.Queue | None = None   # solo para difusion
        self._outbox_full: str = "close"
        self._writer: asyncio.Task | None = None
        self.close_code: int | None = None
        # Rellenados por el dispatcher antes de invocar el handler.
        self.user = None
        self.session = None
        self.path_params: dict[str, Any] = {}

    # ------------------------------------------------------------------ datos

    @property
    def path(self) -> str:
        return self.scope.get("path", "")

    @property
    def headers(self) -> dict[str, str]:
        if not hasattr(self, "_headers"):
            self._headers = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in self.scope.get("headers", [])
            }
        return self._headers

    @property
    def query_params(self) -> dict[str, str]:
        """Solo el primer valor de cada clave; usa `query_lists` si se repiten."""
        if not hasattr(self, "_qp"):
            raw = self.scope.get("query_string", b"").decode("utf-8", "replace")
            self._ql = parse_qs(raw, keep_blank_values=True)
            self._qp = {k: v[0] for k, v in self._ql.items()}
        return self._qp

    @property
    def query_lists(self) -> dict[str, list[str]]:
        self.query_params  # fuerza el parseo
        return self._ql

    @property
    def cookies(self) -> dict[str, str]:
        if not hasattr(self, "_cookies"):
            jar = SimpleCookie()
            jar.load(self.headers.get("cookie", ""))
            self._cookies = {k: v.value for k, v in jar.items()}
        return self._cookies

    @property
    def subprotocols(self) -> list[str]:
        return self.scope.get("subprotocols", [])

    @property
    def client(self) -> tuple[str, int] | None:
        c = self.scope.get("client")
        return tuple(c) if c else None

    @property
    def connected(self) -> bool:
        return self._state == OPEN

    @property
    def groups(self) -> frozenset[str]:
        """De que grupos eres miembro ahora mismo (vacio tras desconectar).

        Distinto de `sock.group`, que es el destino por defecto de broadcast()
        y sigue apuntando al mismo sitio despues de la desconexion.
        """
        return frozenset(self._groups)

    def __repr__(self) -> str:
        return f"<WebSocket {self.path} {self._state}>"

    # ------------------------------------------------------------- handshake

    async def accept(self, subprotocol: str | None = None, headers=None) -> None:
        if self._state != CONNECTING:
            return
        msg = {"type": "websocket.accept", "subprotocol": subprotocol}
        if headers:
            msg["headers"] = [
                (
                    k.encode() if isinstance(k, str) else k,
                    v.encode() if isinstance(v, str) else v,
                )
                for k, v in headers
            ]
        await self._send(msg)
        self._state = OPEN

    async def _ensure_open(self) -> None:
        if self._state == CONNECTING:
            await self.accept()
        elif self._state == CLOSED:
            raise WebSocketClosed("El socket ya esta cerrado.")

    async def close(self, code: int = 1000, reason: str = "") -> None:
        """
        Cierra entregando `code` y `reason` al cliente.

        Si el handshake aun no se completo lo completa primero: cerrar sin
        aceptar hace que el servidor conteste un HTTP 403 y el navegador reciba
        un `onclose` con code 1006 y sin motivo. Aceptando y cerrando acto
        seguido, el JS del cliente recibe tu codigo tal cual y puede distinguir
        "falta login" de "sala inexistente". Usa `deny()` si prefieres tumbar
        el handshake.
        """
        if self._state == CLOSED:
            return
        await self._vaciar_buzon()
        await self._leave_all()
        try:
            if self._state == CONNECTING:
                await self.accept()
            await self._send(
                {"type": "websocket.close", "code": code, "reason": reason}
            )
        except Exception:
            pass  # el cliente ya se fue
        self._state = CLOSED
        self.close_code = code

    async def deny(self, code: int = 403) -> None:
        """
        Rechaza el handshake sin aceptarlo: el cliente ve un HTTP 403 y nunca
        llega a existir un WebSocket. Mas seguro cuando la conexion no deberia
        haberse intentado siquiera (origen no permitido).
        """
        if self._state != CONNECTING:
            return await self.close(1008, "Denied")
        try:
            await self._send({"type": "websocket.close", "code": code})
        except Exception:
            pass
        self._state = CLOSED
        self.close_code = code

    # --------------------------------------------------------------- entrada

    async def receive(self) -> Message:
        """
        El siguiente mensaje. Lanza `WebSocketDisconnect` cuando ya no hay
        conexion, venga de donde venga.

        Ese "venga de donde venga" importa: el socket tambien puede cerrarse
        sin que el cliente se vaya -- porque le echamos por no consumir, o
        porque murio su escritor. Antes eso salia como `WebSocketClosed`, que
        no es `WebSocketDisconnect`, asi que reventaba el `async for` del
        handler en vez de terminarlo: traza en el log, cierre con 1011, y el
        codigo de limpieza posterior sin ejecutar. Aparecio con 6000
        conexiones, que es justo cuando empieza a haber expulsiones.
        """
        if self._state == CLOSED:
            raise WebSocketDisconnect(
                self.close_code if self.close_code is not None else 1006,
                "el socket ya estaba cerrado",
            )
        await self._ensure_open()
        event = await self._receive()
        if event["type"] == "websocket.disconnect":
            self._state = CLOSED
            self.close_code = event.get("code", 1005)
            await self._leave_all()
            raise WebSocketDisconnect(self.close_code, event.get("reason", ""))
        return Message(text=event.get("text"), data=event.get("bytes"))

    async def receive_text(self) -> str:
        msg = await self.receive()
        if msg.text is None:
            raise TypeError("Se esperaba un frame de texto y llego uno binario.")
        return msg.text

    async def receive_bytes(self) -> bytes:
        msg = await self.receive()
        if msg.bytes is None:
            raise TypeError("Se esperaba un frame binario y llego uno de texto.")
        return msg.bytes

    async def receive_json(self, **kwargs) -> Any:
        return (await self.receive()).json(**kwargs)

    def __aiter__(self):
        return self

    async def __anext__(self) -> Message:
        try:
            return await self.receive()
        except WebSocketDisconnect:
            raise StopAsyncIteration

    async def iter_text(self):
        async for msg in self:
            if msg.text is not None:
                yield msg.text

    async def iter_json(self):
        async for msg in self:
            yield msg.json()

    # ---------------------------------------------------------------- salida

    async def send(self, data: Any) -> None:
        """str -> texto, bytes -> binario, cualquier otra cosa -> JSON."""
        if isinstance(data, str):
            await self.send_text(data)
        elif isinstance(data, (bytes, bytearray, memoryview)):
            await self.send_bytes(bytes(data))
        else:
            await self.send_json(data)

    async def send_text(self, text: str) -> None:
        await self._ensure_open()
        await self._send({"type": "websocket.send", "text": text})

    async def send_bytes(self, data: bytes) -> None:
        await self._ensure_open()
        await self._send({"type": "websocket.send", "bytes": data})

    async def send_json(self, data: Any, **kwargs) -> None:
        """
        Serializa con el codificador de Django: `datetime` sale en ISO-8601,
        `Decimal` como cadena, `UUID` y las cadenas lazy tambien.

        Un objeto que no sepa serializar lanza un TypeError que dice cual es y
        que hacer, en vez de mandar `"Usuario object (3)"` al navegador y que
        te enteres en produccion.
        """
        if "default" not in kwargs:
            kwargs.setdefault("cls", _get_encoder())
        await self.send_text(json.dumps(data, **kwargs))

    # ---------------------------------------------------------------- grupos

    async def join(self, *groups: str) -> None:
        """El primer grupo al que entras pasa a ser el destino por defecto."""
        for g in groups:
            await self._layer.add(g, self)
            self._groups.add(g)
            if self.group is None:
                self.group = g

    async def leave(self, *groups: str) -> None:
        for g in groups:
            await self._layer.discard(g, self)
            self._groups.discard(g)
            if self.group == g:
                self.group = next(iter(self._groups), None)

    async def broadcast(
        self, data: Any, *, to: str | None = None, exclude_self: bool = False
    ) -> None:
        """
        Envia a todos los miembros de un grupo.

            await sock.broadcast(data)                  # al grupo por defecto
            await sock.broadcast(data, to="otro:grupo")
            await sock.broadcast(data, exclude_self=True)
        """
        target = to or self.group
        if target is None:
            raise ValueError(
                "sock.broadcast(data) sin grupo por defecto. Entra en uno con "
                "await sock.join('mi:grupo'), declaralo en la ruta con "
                "@ws(..., group='mi:{param}') o indica el destino con "
                "sock.broadcast(data, to='mi:grupo')."
            )
        await self._layer.send(target, data, exclude=self if exclude_self else None)

    # -------------------------------------------------------------- fan-out

    async def enqueue(self, data: Any) -> bool:
        """
        Encola un mensaje de difusion. `False` si este cliente va tan atrasado
        que hay que echarlo.

        No espera a que se escriba, y eso es el punto. `sock.send()` si espera
        --ahi el bloqueo es sano: si el cliente no puede seguirte, tu handler
        va mas despacio--. Pero en un broadcast esperar es ruinoso: uno solo
        que no lea deja colgado para siempre al que difunde, y con el su bucle
        de lectura y su limpieza. Medido: uvicorn frena a los ~24 MB, y a
        partir de ahi `send()` no vuelve.
        """
        if self._state == CLOSED:
            return False

        if self._outbox is None:
            maximo, self._outbox_full = _config_outbox()
            self._outbox = asyncio.Queue(maxsize=maximo)
        if self._writer is None or self._writer.done():
            self._writer = asyncio.create_task(self._drenar())

        try:
            self._outbox.put_nowait(data)
            return True
        except asyncio.QueueFull:
            if self._outbox_full == "drop_oldest":
                # Para flujos que toleran huecos (posiciones, telemetria):
                # mas vale perder lo viejo que echar al cliente.
                try:
                    self._outbox.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._outbox.put_nowait(data)
                return True
            return False

    async def _drenar(self) -> None:
        """Escribe el buzon en orden. Si el socket muere, se sale de los grupos."""
        try:
            while True:
                data = await self._outbox.get()
                try:
                    await self.send(data)
                except Exception:
                    self._outbox.task_done()
                    self._state = CLOSED
                    await self._leave_all()
                    return
                self._outbox.task_done()
        except asyncio.CancelledError:
            raise

    def evict(self, code: int = 1013, reason: str = "Client too slow") -> None:
        """
        Echa a un cliente que no consume, sin esperarle.

        No se puede `await close()`: escribir hacia el esta bloqueado, que es
        justo el motivo por el que lo estamos echando. Se cancela el escritor y
        el cierre sale en segundo plano con plazo.

        El handler de ese cliente sigue parado en `receive()` hasta que el
        servidor ASGI le entregue el `websocket.disconnect`; con uvicorn eso
        llega como mucho en ping_interval + ping_timeout (40 s por defecto).
        Mientras tanto no consume nada y ya esta fuera de todos los grupos.
        """
        if self._state == CLOSED:
            return
        self._state = CLOSED
        self.close_code = code
        if self._writer is not None:
            self._writer.cancel()
        asyncio.get_event_loop().create_task(self._cerrar_en_diferido(code, reason))

    async def _cerrar_en_diferido(self, code: int, reason: str) -> None:
        try:
            await asyncio.wait_for(
                self._send({"type": "websocket.close", "code": code, "reason": reason}),
                timeout=5,
            )
        except Exception:
            pass

    async def drain(self, timeout: float = 1.0) -> None:
        """
        Espera a que salga todo lo encolado para difusion.

        En produccion no hace falta: el escritor va solo y `broadcast` no
        espera a nadie a proposito. En un test si, para afirmar sobre lo que ya
        llego en vez de dormir a ciegas y cruzar los dedos.
        """
        if self._outbox is None:
            return
        try:
            await asyncio.wait_for(self._outbox.join(), timeout=timeout)
        except asyncio.TimeoutError:
            pass

    async def _vaciar_buzon(self, plazo: float = 1.0) -> None:
        """Da al escritor una ultima oportunidad antes de cerrar."""
        if self._outbox is None or self._writer is None or self._writer.done():
            return
        try:
            await asyncio.wait_for(self._outbox.join(), timeout=plazo)
        except Exception:
            pass

    def _parar_escritor(self) -> None:
        """
        Cancela la tarea escritora. Idempotente.

        Va en la limpieza comun y no solo en `close()`, porque el camino
        habitual es el otro: el cliente se desconecta, `receive()` marca el
        socket cerrado, y entonces `close()` sale de vuelta sin llegar a
        cancelar nada. Era una tarea huerfana por cada conexion que terminaba.
        """
        if self._writer is None or self._writer.done():
            return
        # No te canceles a ti mismo: _drenar() tambien pasa por aqui al fallar.
        try:
            if asyncio.current_task() is self._writer:
                return
        except RuntimeError:
            pass
        self._writer.cancel()

    async def _leave_all(self) -> None:
        """
        Saca el socket de sus grupos, pero NO borra `self.group`.

        `groups` es de que grupos eres miembro; `group` es a donde apunta
        broadcast() por defecto. Al desconectar dejas de ser miembro, pero el
        destino sigue siendo valido: si no, el patron mas comun que existe

            async for msg in sock:
                ...
            await sock.broadcast({"tipo": "sale"})   # <- ya sin grupo

        se quedaria sin destino justo en la linea en la que hace falta.
        """
        self._parar_escritor()
        if self._groups and self._layer is not None:
            for g in list(self._groups):
                await self._layer.discard(g, self)
            self._groups.clear()


def _config_outbox() -> tuple[int, str]:
    """
    Tamaño del buzon de difusion y que hacer cuando se llena.

    El 256 no es un numero redondo elegido a ojo: medido, un proceso publica
    ~1.500 broadcast/s contra Redis, asi que un buzon de 64 se llenaria en
    42 ms a maxima tasa -- menos de lo que dura un hipo de red movil o una
    pausa de GC, y echarias a clientes sanos. Con 256 el margen sube a ~170 ms
    en el peor caso, y a decenas de segundos al ritmo de un chat normal.

    El coste en memoria solo lo pagan los clientes atascados: uno que consume
    tiene el buzon a cero. Son `atascados x 256 x tamaño_del_mensaje`.
    """
    from django.conf import settings

    conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
    return int(conf.get("SEND_QUEUE_MAX", 256)), conf.get("SEND_QUEUE_FULL", "close")
