"""Todo el codigo WebSocket de la app. Comparalo con un consumer de Channels."""

from asgiref.sync import sync_to_async
from django.utils import timezone
from django.contrib.auth.models import User

from django_socket import login_required, ws


@ws("echo/")
async def echo(sock):
    """Lo minimo posible: sin accept(), sin connect(), sin disconnect()."""
    async for msg in sock:
        await sock.send(f"echo: {msg.text}")


@ws("chat/<str:room>/", group="room:{room}")
async def chat(sock, room):
    """
    `group=` se rellena con los parametros de la ruta: el socket entra al
    conectar, sale al desconectar, y broadcast() va ahi por defecto.
    """
    who = sock.user.username if sock.user.is_authenticated else "anonimo"

    await sock.broadcast({"tipo": "entra", "quien": who}, exclude_self=True)

    async for msg in sock:
        await sock.broadcast({"tipo": "mensaje", "quien": who, "texto": msg.text})

    # Se llega aqui cuando el cliente cierra. El `leave` del grupo es
    # automatico, pero avisar al resto no lo es.
    await sock.broadcast({"tipo": "sale", "quien": who})


@ws("counter/<int:start>/")
async def counter(sock, start):
    """`<int:start>` llega como int de verdad, no como str."""
    await sock.send_json({"recibido": start, "tipo": type(start).__name__})
    async for msg in sock:
        if msg == "reset":          # Message se compara con str directamente
            start = 0
        else:
            start += 1
        await sock.send_json({"contador": start})


@ws("panel/")
@login_required
async def panel(sock):
    """Cierra con 4401 si no hay sesion."""
    await sock.send_json({"hola": sock.user.username, "id": sock.user.pk})
    async for _ in sock:
        pass


@ws("whoami/", auth=True)
async def whoami(sock):
    await sock.send_json(
        {
            "usuario": str(sock.user),
            "autenticado": sock.user.is_authenticated,
            "query": sock.query_params,
            "path": sock.path,
        }
    )


@ws("usuarios/", auth=False)
async def usuarios(sock):
    """Prueba que el ORM funciona dentro de un handler."""
    # API async del ORM (Django 4.1+): sin sync_to_async ni threads.
    total = await User.objects.acount()
    nombres = [u.username async for u in User.objects.all()[:5]]
    await sock.send_json({"total": total, "nombres": nombres})

    # Y el ORM sincrono tambien, gracias al ThreadSensitiveContext del dispatcher.
    primero = await sync_to_async(lambda: User.objects.first().username)()
    await sock.send_json({"sync_to_async": primero})


@ws("grupo/<str:sala>/", group="auto:{sala}", auth=False)
async def grupo(sock, sala):
    """Sin un solo join(): el grupo lo declara la ruta."""
    await sock.send_json({"group": sock.group, "groups": sorted(sock.groups)})
    async for _ in sock:
        pass


# --------------------------------------------------------------------- Events
# Protocolo JSON con varios tipos de mensaje, sin el if/elif de siempre.

from django_socket import Events

tablero = Events()


@tablero.on("dibujar")
async def dibujar(sock, datos):
    await sock.broadcast({"type": "dibujar", **datos}, exclude_self=True)


@tablero.on("borrar")
async def borrar(sock):
    """Sin datos que usar: no los pidas y no te los pasan."""
    await sock.broadcast({"type": "borrar"})


@tablero.on("ping")
async def ping(sock):
    await sock.send_json({"type": "pong", "cuando": timezone.now()})


@ws("tablero/<str:sala>/", group="tablero:{sala}", auth=False)
async def tablero_handler(sock, sala):
    await tablero.run(sock)
