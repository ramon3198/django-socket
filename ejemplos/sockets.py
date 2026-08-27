"""Cuatro patrones completos. Cada uno resuelve algo que la gente construye.

Todos corren en la demo: `python manage.py runserver` y entra en `/ejemplos/`.
"""

import asyncio

from django.utils import timezone

from django_socket import Events, broadcast, group_size, login_required, ws

# ============================================================================
# 1. PROGRESO DE UNA TAREA EN SEGUNDO PLANO
# ============================================================================
# El patron que mas se busca y el que peor documentado esta.
#
# Lo importante: el handler del socket NO hace el trabajo. Solo entra en un
# grupo y espera. Quien empuja es la tarea, desde su propio proceso.
#
#     # tasks.py
#     from celery import shared_task
#     from django_socket import broadcast_sync
#
#     @shared_task
#     def exportar(informe_id):
#         grupo = f"tarea:{informe_id}"
#         filas = Venta.objects.all()
#         total = filas.count()
#         for i, fila in enumerate(filas.iterator(), 1):
#             escribir(fila)
#             if i % 100 == 0:                      # no en cada fila: satura
#                 broadcast_sync(
#                     {"type": "progreso", "hecho": i, "total": total},
#                     to=grupo,
#                 )
#         broadcast_sync({"type": "listo", "url": url_descarga}, to=grupo)
#
# ┌─ LA TRAMPA ───────────────────────────────────────────────────────────────┐
# │ Un worker de Celery es OTRO PROCESO. Con la capa por defecto ("memory")   │
# │ cada proceso solo conoce sus propios sockets, asi que ese broadcast_sync  │
# │ no llega a nadie y no da ningun error: simplemente no pasa nada.          │
# │                                                                           │
# │ Este patron REQUIERE Redis:                                               │
# │     DJANGO_SOCKET = {"LAYER": "redis"}                                    │
# └───────────────────────────────────────────────────────────────────────────┘


@ws("ej/tarea/<str:tarea_id>/", group="tarea:{tarea_id}", auth=False)
async def seguir_tarea(sock, tarea_id):
    """Se queda escuchando el progreso. No hace el trabajo, lo mira."""
    await sock.send_json({"type": "conectado", "tarea": tarea_id})

    async for _ in sock:
        pass  # el cliente no manda nada; solo recibe


@ws("ej/tarea-demo/<str:tarea_id>/", group="tarea:{tarea_id}", auth=False)
async def tarea_demo(sock, tarea_id):
    """
    Lo mismo pero simulando el trabajo aqui dentro, para que la demo funcione
    sin instalar Celery ni levantar Redis.

    En tu proyecto esto NO va asi: el bucle vive en la tarea, y el handler es
    el de arriba. Si haces el trabajo dentro del handler, bloqueas esa
    conexion y pierdes todo el sentido de tener una cola.
    """
    total = 40
    for i in range(1, total + 1):
        await asyncio.sleep(0.05)
        await sock.broadcast({"type": "progreso", "hecho": i, "total": total})
    await sock.broadcast({"type": "listo", "url": "/informe.csv"})


# ============================================================================
# 2. NOTIFICACIONES EN VIVO
# ============================================================================
# Avisar a un usuario concreto desde donde sea: una vista, una signal, el admin.
#
#     # signals.py
#     @receiver(post_save, sender=Pedido)
#     def avisar(sender, instance, created, **kwargs):
#         if created:
#             broadcast_sync(
#                 {"type": "aviso", "texto": f"Pedido #{instance.pk} recibido"},
#                 to=f"usuario:{instance.cliente_id}",
#             )
#
# El grupo por usuario es la clave: cada uno entra en el suyo, y desde
# cualquier punto del proyecto le hablas por su id sin saber si esta conectado.


@ws("ej/avisos/")
@login_required
async def avisos(sock):
    # Aqui el grupo NO puede venir de `group=` en la ruta: sale del usuario,
    # que solo se conoce despues de autenticar. Se entra a mano.
    await sock.join(f"usuario:{sock.user.pk}")
    await sock.send_json({"type": "listo", "para": sock.user.username})
    async for _ in sock:
        pass


@ws("ej/avisos-demo/<int:pk>/", group="usuario:{pk}", auth=False)
async def avisos_demo(sock, pk):
    """Version sin login, para poder probarlo en la demo."""
    await sock.send_json({"type": "listo", "para": f"usuario {pk}"})
    async for msg in sock:
        # En tu proyecto esto lo dispara una signal, no el propio cliente.
        await sock.broadcast({"type": "aviso", "texto": msg.text,
                              "cuando": timezone.now()})


# ============================================================================
# 3. PRESENCIA: QUIEN ESTA CONECTADO
# ============================================================================
# El truco esta en el `finally`: si alguien se va por las malas -- se le cae la
# red, cierra el portatil -- el codigo de despues del bucle igual se ejecuta,
# porque `async for` termina cuando llega la desconexion. Sin eso, la lista de
# conectados solo crece.

CONECTADOS: dict[str, set[str]] = {}


@ws("ej/presencia/<str:sala>/", group="presencia:{sala}", auth=False)
async def presencia(sock, sala):
    quien = sock.query_params.get("como") or f"anon-{id(sock) % 1000}"
    gente = CONECTADOS.setdefault(sala, set())

    gente.add(quien)
    try:
        await sock.broadcast({"type": "lista", "gente": sorted(gente)})
        async for _ in sock:
            pass
    finally:
        # Pase lo que pase: cierre limpio, red caida o excepcion.
        gente.discard(quien)
        await sock.broadcast({"type": "lista", "gente": sorted(gente)})


# ============================================================================
# 4. DASHBOARD EN VIVO
# ============================================================================
# Metricas que se refrescan solas. Dos cosas que se hacen mal:
#
#   - Un `while True` con `sleep` por conexion: con 500 pestañas abiertas son
#     500 bucles consultando lo mismo. Mejor una tarea periodica que difunda
#     al grupo, y aqui solo escuchar. Abajo esta el bucle porque es una demo.
#
#   - Enviar cada cambio: si el dato cambia 50 veces por segundo no hace falta
#     mandarlo 50 veces. `drop_oldest` esta pensado justo para esto.

panel = Events()


@panel.on("intervalo")
async def cambiar_intervalo(sock, datos):
    sock.intervalo = max(0.2, min(5.0, float(datos.get("segundos", 1))))
    await sock.send_json({"type": "ok", "intervalo": sock.intervalo})


@ws("ej/panel/", group="panel", auth=False)
async def panel_handler(sock):
    sock.intervalo = 1.0

    async def emitir():
        while sock.connected:
            await sock.send_json({
                "type": "metrica",
                "conectados": await group_size("panel"),
                "cuando": timezone.now(),
            })
            await asyncio.sleep(sock.intervalo)

    tarea = asyncio.create_task(emitir())
    try:
        await panel.run(sock)          # escucha cambios de intervalo
    finally:
        tarea.cancel()                 # o la tarea sobrevive al socket


# ============================================================================
# Enviar desde una vista sincrona (lo que hace una signal o el admin)
# ============================================================================
#
#     from django_socket import broadcast_sync
#     broadcast_sync({"type": "aviso", "texto": "..."}, to="usuario:7")
#
# Desde codigo async (una vista async, una tarea de asyncio):
#
#     from django_socket import broadcast
#     await broadcast({"type": "aviso"}, to="usuario:7")


async def avisar_a(user_id: int, texto: str) -> None:
    """Ayuda de ejemplo: avisar a un usuario este donde este conectado."""
    await broadcast({"type": "aviso", "texto": texto}, to=f"usuario:{user_id}")
