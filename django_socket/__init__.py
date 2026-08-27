"""
django_socket -- WebSockets en Django sin ceremonia.

Instalar es: `pip install django-socket` y añadirlo a INSTALLED_APPS. Ya esta.

    # miapp/sockets.py
    from django_socket import ws

    @ws("chat/<str:room>/", group="room:{room}")
    async def chat(sock, room):
        async for msg in sock:
            await sock.broadcast({"de": str(sock.user), "texto": msg.text})
"""

from .asgi import ASGIApplication
from .authentication import extraer_token, login_required
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

__version__ = "0.1.0"

__all__ = [
    # Lo que usaras el 99% del tiempo
    "ws",
    "Events",
    "broadcast",
    "broadcast_sync",
    "login_required",
    "extraer_token",
    # Tipos, para anotar
    "WebSocket",
    "Message",
    "WebSocketDisconnect",
    "WebSocketClosed",
    "InvalidJSON",
    # Puntos de extension
    "ASGIApplication",
    "BaseLayer",
    "MemoryLayer",
    "RedisLayer",
    "set_layer",
    "group_size",
    "get_routes",
    "__version__",
]
