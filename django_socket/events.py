"""Enrutado de mensajes JSON por tipo.

Casi toda app que habla JSON por el socket manda `{"type": "algo", ...}` y
acaba con un if/elif largo dentro del bucle. Esto lo convierte en funciones con
nombre, sin que dejes de ver el flujo:

    from django_socket import Events, ws

    chat = Events()

    @chat.on("mensaje")
    async def mensaje(sock, datos):
        await sock.broadcast({"type": "mensaje", "texto": datos["texto"]})

    @chat.on("escribiendo")
    async def escribiendo(sock):            # si no usas los datos, no los pidas
        await sock.broadcast({"type": "escribiendo"}, exclude_self=True)

    @ws("chat/<str:room>/", group="room:{room}")
    async def handler(sock, room):
        await chat.run(sock)

Es opcional del todo: `async for msg in sock` sigue estando ahi y no necesitas
saber que esto existe.
"""

from __future__ import annotations

import inspect
import logging

from .websocket import InvalidJSON

logger = logging.getLogger("django_socket")

CUALQUIERA = "*"


class Events:
    """
    Despacha mensajes JSON segun un campo (por defecto `"type"`).

    `key`     -- el campo que decide el tipo. Usa `Events(key="action")` si tu
                 protocolo se llama de otra forma.
    `strict`  -- que hacer con un tipo que nadie maneja. Por defecto se ignora
                 (y se anota en el log); con `strict=True` se cierra con 4400.
    """

    def __init__(self, key: str = "type", strict: bool = False):
        self.key = key
        self.strict = strict
        self._handlers: dict[str, tuple] = {}

    def on(self, *tipos: str):
        """
        Registra un handler para uno o varios tipos.

            @chat.on("mensaje")
            @chat.on("entrar", "salir")     # varios de golpe
            @chat.on("*")                   # lo que no case con nada mas

        El handler puede pedir los datos o no:

            async def handler(sock, datos)  -> datos = el mensaje sin el campo key
            async def handler(sock)         -> te basta con saber que llego
        """
        if not tipos:
            raise TypeError("@on() necesita al menos un tipo: @on('mensaje')")

        def decorator(fn):
            if not inspect.iscoroutinefunction(fn):
                raise TypeError(
                    f"@on espera 'async def', y {fn.__name__} es una funcion "
                    f"normal."
                )
            quiere_datos = _quiere_datos(fn)
            for tipo in tipos:
                if tipo in self._handlers:
                    anterior = self._handlers[tipo][0].__name__
                    raise ValueError(
                        f"El tipo {tipo!r} ya lo maneja {anterior}."
                    )
                self._handlers[tipo] = (fn, quiere_datos)
            return fn

        return decorator

    @property
    def tipos(self) -> list[str]:
        return sorted(self._handlers)

    async def run(self, sock) -> None:
        """Consume mensajes hasta que el cliente cierra."""
        async for msg in sock:
            await self.handle(sock, msg.json())

    async def handle(self, sock, datos) -> None:
        """Despacha un mensaje ya parseado. Util para testear un handler suelto."""
        if not isinstance(datos, dict):
            raise InvalidJSON(datos, f"se esperaba un objeto con {self.key!r}")

        tipo = datos.get(self.key)
        entrada = self._handlers.get(tipo) or self._handlers.get(CUALQUIERA)

        if entrada is None:
            if self.strict:
                raise InvalidJSON(
                    datos, f"tipo {tipo!r} desconocido; hay: {self.tipos}"
                )
            # A nivel WARNING a proposito: casi siempre es una errata en el
            # nombre del tipo, y en DEBUG no la ve nadie. Si tu protocolo manda
            # tipos que de verdad quieres ignorar, registra un @on("*") vacio.
            logger.warning(
                "django_socket: nadie maneja %s=%r en %s (registrados: %s)",
                self.key, tipo, sock.path, self.tipos or "ninguno",
            )
            return

        handler, quiere_datos = entrada
        if quiere_datos:
            await handler(sock, {k: v for k, v in datos.items() if k != self.key})
        else:
            await handler(sock)


def _quiere_datos(fn) -> bool:
    """
    Mira si el handler pide el payload ademas del socket.

    Se resuelve una vez al registrar, no en cada mensaje.
    """
    params = [
        p for p in inspect.signature(fn).parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
    ]
    if len(params) == 1:
        return False
    if len(params) == 2:
        return True
    raise TypeError(
        f"{fn.__name__} debe aceptar (sock) o (sock, datos), no "
        f"{len(params)} parametros posicionales."
    )
