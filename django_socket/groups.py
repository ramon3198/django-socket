"""Capa de difusion: grupos y broadcast.

`MemoryLayer` sirve para un solo proceso (dev, o un unico worker).
`RedisLayer` reparte el fan-out entre procesos via pub/sub.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

logger = logging.getLogger("django_socket")


class BaseLayer:
    async def startup(self) -> None: ...

    async def shutdown(self) -> None: ...

    async def add(self, group: str, sock) -> None:
        raise NotImplementedError

    async def discard(self, group: str, sock) -> None:
        raise NotImplementedError

    async def send(self, group: str, data: Any, *, exclude=None) -> None:
        raise NotImplementedError

    async def size(self, group: str) -> int:
        raise NotImplementedError


class MemoryLayer(BaseLayer):
    """Grupos en memoria del proceso. Sin dependencias externas."""

    def __init__(self):
        self._groups: dict[str, set] = {}

    async def add(self, group: str, sock) -> None:
        self._groups.setdefault(group, set()).add(sock)

    async def discard(self, group: str, sock) -> None:
        members = self._groups.get(group)
        if members:
            members.discard(sock)
            if not members:
                del self._groups[group]

    async def size(self, group: str) -> int:
        return len(self._groups.get(group, ()))

    async def send(self, group: str, data: Any, *, exclude=None) -> None:
        await self._deliver_local(group, data, exclude=exclude)

    async def _deliver_local(self, group: str, data: Any, *, exclude=None) -> None:
        """
        Reparte encolando, sin esperar a que nadie lea.

        Esperar seria el bug: un solo miembro que no consume dejaria colgado
        para siempre al que difunde. Cada socket tiene un buzon acotado; si se
        llena, ese cliente va demasiado atrasado y se le echa en vez de dejar
        que arrastre a los demas.
        """
        miembros = [s for s in self._groups.get(group, ()) if s is not exclude]
        if not miembros:
            return

        lentos = []
        for sock in miembros:
            if not await sock.enqueue(data):
                lentos.append(sock)

        # Cede el turno una vez por difusion, no una por miembro: es donde
        # corren los escritores. Sin esto, un handler que difunde en bucle
        # (`for fila in lote: await sock.broadcast(fila)`) no soltaria nunca el
        # loop, los buzones se llenarian y acabaria echando a clientes sanos.
        await asyncio.sleep(0)

        for sock in lentos:
            logger.warning(
                "django_socket: %r no consume (buzon lleno); se le echa del "
                "grupo %r. Sube DJANGO_SOCKET['SEND_QUEUE_MAX'] si tu caso "
                "manda rafagas legitimas.",
                sock, group,
            )
            sock.evict()
            await self.discard(group, sock)


class RedisLayer(MemoryLayer):
    """
    Mantiene los miembros locales igual que MemoryLayer, pero publica cada
    broadcast en Redis para que los demas procesos entreguen a los suyos.

    Sobrevive a que Redis se caiga: la entrega local sigue funcionando, los
    sockets de los usuarios no se cierran, y al volver Redis el proceso se
    resuscribe solo. Lo que se publique mientras esta caido se pierde -- esto
    es pub/sub, no una cola.
    """

    ESPERA_MAX = 10.0          # tope del backoff al reconectar
    LATIDO = 15.0              # health check de redis-py, en segundos

    def __init__(self, url: str = "redis://localhost:6379/0", prefix: str = "djws"):
        super().__init__()
        self.url = url
        self.channel = f"{prefix}:broadcast"
        self._redis = None
        self._pubsub = None
        self._listener: asyncio.Task | None = None
        self._conectado = False
        # Identifica a este proceso para no entregar dos veces lo que ya
        # entregamos localmente.
        self._origin = f"{id(self)}"

    async def startup(self) -> None:
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "RedisLayer necesita el paquete 'redis'. Instalalo con: pip install redis"
            ) from exc
        self._redis = aioredis.from_url(
            self.url,
            health_check_interval=self.LATIDO,   # sin esto no detecta la caida
            retry_on_timeout=True,
        )
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self.channel)
        self._conectado = True
        self._listener = asyncio.create_task(self._listen())
        logger.info("django_socket: RedisLayer conectada a %s", self.url)

    async def shutdown(self) -> None:
        self._conectado = False
        if self._listener:
            self._listener.cancel()
            try:
                await self._listener
            except asyncio.CancelledError:
                pass
        if self._pubsub:
            try:
                await self._pubsub.unsubscribe(self.channel)
            except Exception:
                pass
            await self._pubsub.aclose()
        if self._redis:
            await self._redis.aclose()

    async def send(self, group: str, data: Any, *, exclude=None) -> None:
        # Entrega local inmediata (asi `exclude` funciona por identidad)...
        await self._deliver_local(group, data, exclude=exclude)
        # ...y avisa al resto de procesos.
        carga = json.dumps(
            {"group": group, "data": data, "origin": self._origin}, default=str
        )
        try:
            await self._redis.publish(self.channel, carga)
        except Exception as exc:
            # Que Redis falle no puede tumbar la conexion del usuario: la
            # entrega local ya se hizo y el handler debe seguir vivo. Se pierde
            # el fan-out a los demas procesos, y por eso se loguea como error.
            logger.error(
                "django_socket: no se pudo publicar en Redis (%s: %s). "
                "El grupo %r solo recibio la entrega local.",
                type(exc).__name__, exc, group,
            )

    async def _listen(self) -> None:
        """
        Bucle de escucha resistente.

        Se usa `get_message(timeout=...)` en vez de `listen()` para tener un
        despertar periodico: ahi es donde redis-py corre su health check y
        donde podemos detectar que la conexion se fue. Con `listen()` a secas
        el proceso se queda sordo para siempre tras una caida.
        """
        espera = 0.5
        while True:
            try:
                mensaje = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                espera = 0.5                      # todo bien, resetea el backoff
                if mensaje is not None and mensaje.get("type") == "message":
                    await self._entregar(mensaje)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._conectado:
                    return
                logger.warning(
                    "django_socket: se perdio la conexion con Redis (%s). "
                    "Reintentando en %.1fs.", type(exc).__name__, espera,
                )
                await asyncio.sleep(espera)
                espera = min(espera * 2, self.ESPERA_MAX)
                await self._resuscribir()

    async def _entregar(self, mensaje) -> None:
        try:
            payload = json.loads(mensaje["data"])
        except (ValueError, KeyError, TypeError):
            logger.warning("django_socket: mensaje ilegible en %s", self.channel)
            return
        if payload.get("origin") == self._origin:
            return  # ya lo entregamos localmente
        await self._deliver_local(payload["group"], payload["data"])

    async def _resuscribir(self) -> None:
        """Vuelve a suscribirse tras un corte. Si Redis sigue caido, lo dira el bucle."""
        try:
            await self._pubsub.aclose()
        except Exception:
            pass
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self.channel)
        logger.info("django_socket: resuscrito a %s", self.channel)


# --------------------------------------------------------------- layer global

_layer: BaseLayer | None = None


def get_layer() -> BaseLayer:
    global _layer
    if _layer is None:
        _layer = _build_layer()
    return _layer


def set_layer(layer: BaseLayer) -> None:
    global _layer
    _layer = layer


def _build_layer() -> BaseLayer:
    from django.conf import settings

    conf = getattr(settings, "DJANGO_SOCKET", {}) or {}
    backend = conf.get("LAYER", "memory")
    if backend == "memory":
        return MemoryLayer()
    if backend == "redis":
        return RedisLayer(
            url=conf.get("REDIS_URL", "redis://localhost:6379/0"),
            prefix=conf.get("PREFIX", "djws"),
        )
    if callable(backend):
        return backend()
    raise ValueError(
        f"DJANGO_SOCKET['LAYER'] invalido: {backend!r}. Usa 'memory', 'redis' "
        f"o un callable que devuelva una BaseLayer."
    )


# ------------------------------------------------------------- API de usuario


async def broadcast(data: Any, *, to: str) -> None:
    """
    Envia a todos los miembros de un grupo, desde fuera de un handler.

        await broadcast({"aviso": "mantenimiento"}, to="room:1")
    """
    await get_layer().send(to, data)


async def group_size(group: str) -> int:
    """Cuantos sockets locales hay en el grupo."""
    return await get_layer().size(group)


def broadcast_sync(data: Any, *, to: str) -> None:
    """
    Igual que `broadcast`, para vistas sincronas, señales o tareas Celery.

    Con la capa 'memory' solo alcanza a los sockets del mismo proceso; para
    llegar a todos los workers necesitas LAYER='redis'.
    """
    from asgiref.sync import async_to_sync

    async_to_sync(broadcast)(data, to=to)
