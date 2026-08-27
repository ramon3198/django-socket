"""
django_socket -- WebSockets in Django without ceremony.

Installing is: `pip install django-socket` and adding it to INSTALLED_APPS.
That is all.

    # myapp/sockets.py
    from django_socket import ws

    @ws("chat/<str:room>/", group="room:{room}")
    async def chat(sock, room):
        async for msg in sock:
            await sock.broadcast({"from": str(sock.user), "text": msg.text})
"""

from .asgi import ASGIApplication
from .authentication import (
    extract_token,
    extraer_token,  # noqa: F401  0.2.x alias, removed at 1.0
    login_required,
)
from .events import Events
from .groups import (
    BaseLayer,
    MemoryLayer,
    RedisLayer,
    broadcast,
    broadcast_sync,
    group_size,
    set_layer,
)
from .routing import get_routes, ws
from .websocket import (
    InvalidJSON,
    Message,
    WebSocket,
    WebSocketClosed,
    WebSocketDisconnect,
)

# Single source of truth for the version. `pyproject.toml` reads it from here
# via `dynamic = ["version"]`, so the two cannot drift: they used to be
# declared separately and the module stayed at 0.1.0 while PyPI was on 0.2.1.
__version__ = "0.3.0"

__all__ = [
    # What you will use 99% of the time
    "ws",
    "Events",
    "broadcast",
    "broadcast_sync",
    "login_required",
    "extract_token",
    # Types, for annotations
    "WebSocket",
    "Message",
    "WebSocketDisconnect",
    "WebSocketClosed",
    "InvalidJSON",
    # Extension points
    "ASGIApplication",
    "BaseLayer",
    "MemoryLayer",
    "RedisLayer",
    "set_layer",
    "group_size",
    "get_routes",
    "__version__",
]
