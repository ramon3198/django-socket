"""The fan-out layer: groups and broadcast.

`MemoryLayer` is for a single process (development, or one worker).
`RedisLayer` spreads the fan-out across processes via pub/sub.
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
    """In-process groups. No external dependencies."""

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
        Deliver by queueing, without waiting for anyone to read.

        Waiting would be the bug: a single member that stops consuming would
        hang the broadcaster forever. Every socket has a bounded outbox; if it
        fills up, that client is too far behind and gets evicted rather than
        being allowed to drag everyone else down.
        """
        members = [s for s in self._groups.get(group, ()) if s is not exclude]
        if not members:
            return

        slow = []
        for sock in members:
            if not await sock.enqueue(data):
                slow.append(sock)

        # Yield once per broadcast, not once per member: that is where the
        # writers run. Without it, a handler broadcasting in a loop
        # (`for row in batch: await sock.broadcast(row)`) would never release
        # the loop, outboxes would fill, and it would evict healthy clients.
        await asyncio.sleep(0)

        for sock in slow:
            logger.warning(
                "django_socket: %r is not consuming (outbox full); evicting from "
                "group %r. Raise DJANGO_SOCKET['SEND_QUEUE_MAX'] if your case "
                "sends legitimate bursts.",
                sock, group,
            )
            sock.evict()
            await self.discard(group, sock)


class RedisLayer(MemoryLayer):
    """
    Keeps local members just like MemoryLayer, but publishes every
    broadcast to Redis so the other processes deliver to theirs.

    It survives Redis going down: local delivery keeps working, user sockets
    are not closed, and the process resubscribes by itself when Redis comes
    back. Whatever is published while it is down is lost -- this is pub/sub,
    not a queue.
    """

    MAX_BACKOFF = 10.0          # backoff ceiling when reconnecting
    HEARTBEAT = 15.0              # health check de redis-py, en segundos

    def __init__(self, url: str = "redis://localhost:6379/0", prefix: str = "djws"):
        super().__init__()
        self.url = url
        self.channel = f"{prefix}:broadcast"
        self._redis = None
        self._pubsub = None
        self._listener: asyncio.Task | None = None
        self._running = False
        # Identifies this process so we don't deliver twice what we already
        # delivered locally.
        self._origin = f"{id(self)}"

    def _connect(self):
        """
        Create the Redis client. Idempotent.

        It lives apart from `startup()` because some processes only publish --
        a Celery worker, a cron, a management command -- and nothing calls
        `startup()` there: no websocket connections and no lifespan event to
        trigger it. Without this, `send()` found `_redis = None`, the publish
        raised AttributeError, the except masked it as a Redis outage, and the
        message was silently dropped. Exactly the Celery progress-bar case,
        which is the most sought-after one.
        """
        if self._redis is not None:
            return self._redis
        try:
            import redis.asyncio as aioredis
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "RedisLayer needs the 'redis' package. Install it with:\n"
                "  pip install redis"
            ) from exc
        self._redis = aioredis.from_url(
            self.url,
            health_check_interval=self.HEARTBEAT,   # without this it misses the outage
            retry_on_timeout=True,
        )
        return self._redis

    async def startup(self) -> None:
        self._connect()
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self.channel)
        self._running = True
        self._listener = asyncio.create_task(self._listen())
        logger.info("django_socket: RedisLayer connected to %s", self.url)

    async def shutdown(self) -> None:
        self._running = False
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
        # Immediate local delivery (so `exclude` works by identity)...
        await self._deliver_local(group, data, exclude=exclude)
        # ...and tell the other processes.
        payload = json.dumps(
            {"group": group, "data": data, "origin": self._origin},
            default=str,
        )
        try:
            # Lazy connection: a publisher-only process never went through
            # startup(), and that used to drop the message silently.
            redis = self._connect()
        except Exception as exc:
            logger.error(
                "django_socket: could not create the Redis client (%s: %s). "
                "Group %r only got local delivery. Check REDIS_URL and that "
                "the 'redis' package is installed.",
                type(exc).__name__, exc, group,
            )
            return

        try:
            await redis.publish(self.channel, payload)
        except Exception as exc:
            # Redis failing must not take the user's connection down: local
            # delivery already happened and the handler has to stay alive. The
            # fan-out to other processes is lost, hence the error log.
            logger.error(
                "django_socket: could not publish to Redis (%s: %s). "
                "Group %r only got local delivery.",
                type(exc).__name__, exc, group,
            )

    async def _listen(self) -> None:
        """
        A resilient listening loop.

        `get_message(timeout=...)` is used instead of `listen()` to get a
        periodic wake-up: that is where redis-py runs its health check and
        where we can notice the connection is gone. With a bare `listen()` the
        process goes deaf forever after an outage.
        """
        retry_after = 0.5
        while True:
            try:
                message = await self._pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=1.0
                )
                retry_after = 0.5                      # all good, reset the backoff
                if message is not None and message.get("type") == "message":
                    await self._dispatch_payload(message)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self._running:
                    return
                logger.warning(
                    "django_socket: lost the connection to Redis (%s). "
                    "Retrying in %.1fs.", type(exc).__name__, retry_after,
                )
                await asyncio.sleep(retry_after)
                retry_after = min(retry_after * 2, self.MAX_BACKOFF)
                await self._resubscribe()

    async def _dispatch_payload(self, message) -> None:
        try:
            payload = json.loads(message["data"])
        except (ValueError, KeyError, TypeError):
            logger.warning("django_socket: unreadable message on %s", self.channel)
            return
        if payload.get("origin") == self._origin:
            return  # we already delivered it locally
        await self._deliver_local(payload["group"], payload["data"])

    async def _resubscribe(self) -> None:
        """Subscribe again after an outage.

        If Redis is still down, the loop will say so on the next pass.
        """
        try:
            await self._pubsub.aclose()
        except Exception:
            pass
        self._pubsub = self._redis.pubsub()
        await self._pubsub.subscribe(self.channel)
        logger.info("django_socket: resubscribed to %s", self.channel)


# ---------------------------------------------------------------- global layer

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
        f"Invalid DJANGO_SOCKET['LAYER']: {backend!r}. Use 'memory', 'redis', "
        f"or a callable returning a BaseLayer."
    )


# ------------------------------------------------------------------ public API


async def broadcast(data: Any, *, to: str) -> None:
    """
    Send to every member of a group, from outside a handler.

        await broadcast({"notice": "maintenance"}, to="room:1")
    """
    await get_layer().send(to, data)


async def group_size(group: str) -> int:
    """How many sockets of *this process* are in the group.

    Under the Redis layer this does NOT count members on other workers.
    """
    return await get_layer().size(group)


def broadcast_sync(data: Any, *, to: str) -> None:
    """
    Same as `broadcast`, for sync views, signals or Celery tasks.

    With the 'memory' layer it only reaches sockets in the same process; to
    reach every worker you need LAYER='redis'.
    """
    from asgiref.sync import async_to_sync

    async_to_sync(broadcast)(data, to=to)
