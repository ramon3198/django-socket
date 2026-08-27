# django_socket

[![PyPI](https://img.shields.io/pypi/v/django-socket)](https://pypi.org/project/django-socket/)
[![tests](https://github.com/ramon3198/django-socket/actions/workflows/tests.yml/badge.svg)](https://github.com/ramon3198/django-socket/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue)](https://github.com/ramon3198/django-socket)
[![django](https://img.shields.io/badge/django-4.2%20%E2%80%93%206.1-0C4B33)](https://github.com/ramon3198/django-socket)
[![license](https://img.shields.io/badge/license-MIT-green)](https://github.com/ramon3198/django-socket/blob/main/LICENSE)

[English](https://github.com/ramon3198/django-socket/blob/main/README.md) · **Español**

**WebSockets para Django.** Un decorador, una función `async`, y ya está
funcionando.

```python
# miapp/sockets.py
from django_socket import ws

@ws("chat/<str:room>/", group="room:{room}")
async def chat(sock, room):
    async for msg in sock:
        await sock.broadcast({"de": str(sock.user), "texto": msg.text})
```

Ese es el archivo entero. El estado de la conexión vive en variables locales,
la desconexión es el código que hay después del bucle, y el usuario de Django
está donde esperas encontrarlo.

---

## Índice

**Empezar** ·
[Instalación](#instalación) ·
[Tu primer socket en 5 minutos](#tu-primer-socket-en-5-minutos) ·
[Por qué no hace falta tocar `asgi.py`](#por-qué-no-hace-falta-tocar-asgipy)

**Guía** ·
[El decorador `@ws`](#el-decorador-ws) ·
[El objeto `sock`](#el-objeto-sock) ·
[Grupos y broadcast](#grupos-y-broadcast) ·
[JSON](#json) ·
[`Events`](#enrutar-por-tipo-con-events) ·
[Autenticación](#usuarios-y-autenticación) ·
[Cliente JavaScript](#cliente-javascript) ·
[Middleware](#middleware) ·
[Testear](#testear-tus-handlers)

**Operación** ·
[Producción](#producción) ·
[Detrás de un proxy](#detrás-de-un-proxy-inverso) ·
[Seguridad](#seguridad-validación-de-origen) ·
[Límites de tasa](#límites-de-tasa) ·
[Clientes lentos](#clientes-lentos) ·
[Conexiones zombis](#conexiones-zombis) ·
[Configuración](#configuración-completa) ·
[Códigos de cierre](#códigos-de-cierre)

**Referencia** ·
[Cómo funciona por dentro](#cómo-funciona-por-dentro) ·
[Rendimiento](#rendimiento) ·
[Límites conocidos](#límites-conocidos) ·
[Desarrollo](#desarrollo)

---

# Empezar

## Instalación

```bash
pip install django-socket
```

```python
# settings.py
INSTALLED_APPS = [
    "django_socket",
    ...
]
```

**Ya está. No hay tercer paso.**

No tocas `asgi.py`, no declaras `ASGI_APPLICATION`, no configuras Redis, no
cambias de servidor. El `asgi.py` que generó `startproject` sirve WebSockets tal
cual, y `manage.py runserver` los sirve en el mismo puerto que el HTTP.

Requisitos: Python 3.10+, Django 4.2+. En desarrollo hace falta
`pip install "uvicorn[standard]"`.

---

## Tu primer socket en 5 minutos

### 1. Escribe el handler

Crea `<tu_app>/sockets.py`. Se autodescubre solo, igual que `admin.py`:

```python
from django_socket import ws

@ws("eco/")
async def eco(sock):
    async for msg in sock:
        await sock.send(f"dijiste: {msg.text}")
```

### 2. Comprueba que está registrado

```bash
python manage.py ws
```

```
WebSocket routes
  ws:///eco/                    miapp.sockets.eco

Integration
  Broadcast layer       memory
  asgi.py               nothing to do there (ASGIHandler widened)
  Allowed origins       ALLOWED_HOSTS=[] (DEBUG)
  Missing Origin        accepted (native clients)
```

Este comando es lo primero que deberías ejecutar cuando algo no funcione: te
dice qué rutas hay, con qué grupo, y cómo está montada la integración.

### 3. Arranca y pruébalo

```bash
python manage.py runserver
```

Desde la consola del navegador, en cualquier página de tu sitio:

```js
const s = new WebSocket("ws://localhost:8000/eco/");
s.onmessage = (e) => console.log(e.data);
s.onopen = () => s.send("hola");
// -> dijiste: hola
```

### 4. Ahora una sala de chat

```python
@ws("chat/<str:room>/", group="room:{room}")
async def chat(sock, room):
    quien = sock.user.username if sock.user.is_authenticated else "anónimo"

    await sock.broadcast({"tipo": "entra", "quien": quien}, exclude_self=True)

    async for msg in sock:
        await sock.broadcast({"tipo": "mensaje", "quien": quien, "texto": msg.text})

    # Se llega aquí cuando el cliente cierra.
    await sock.broadcast({"tipo": "sale", "quien": quien})
```

Todo lo que necesitas saber está en esas líneas:

- **`<str:room>`** es la sintaxis de `django.urls.path`. Llega al handler ya
  convertido (`<int:pk>` te da un `int` de verdad).
- **`group="room:{room}"`** se rellena con ese parámetro. El socket entra al
  conectar y sale al desconectar, sin que llames a nada.
- **`sock.broadcast(...)`** va a ese grupo por defecto.
- **`sock.user`** es el usuario de Django, resuelto de la cookie de sesión.
- **El código después del `for`** se ejecuta cuando el cliente se va. Ese es tu
  `disconnect`.

---

## Por qué no hace falta tocar `asgi.py`

Django ya es ASGI desde la 3.0. Lo único que hace su handler con un WebSocket es
esto:

```python
if scope["type"] != "http":
    raise ValueError("Django can only handle ASGI/HTTP connections, not %s.")
    # el propio Django deja ahí un "FIXME: Allow to override this."
```

Django no es que *no pueda* hablar WebSocket — se niega a mirar ese scope. Y
como `django.setup()` ejecuta los `ready()` de las apps **antes** de instanciar
el handler, desde nuestro `AppConfig.ready()` llegamos a tiempo de ensanchar esa
puerta. De ahí que instalar sea solo `INSTALLED_APPS`.

Si prefieres que no se toque nada tuyo, apágalo y decláralo a mano:

```python
DJANGO_SOCKET = {"PATCH_ASGI": False}
```

```python
# asgi.py
from django_socket import ASGIApplication
application = ASGIApplication()
```

---

# Guía

## El decorador `@ws`

```python
@ws(ruta, *, group=None, auth=True, name=None)
```

| | |
|---|---|
| `ruta` | Sintaxis de `django.urls.path`, con sus conversores |
| `group` | Plantilla de grupo; entra al conectar y sale al desconectar |
| `auth` | `False` salta la resolución de sesión (una consulta menos) |
| `name` | Nombre para el listado de `manage.py ws` |

```python
@ws("partida/<int:pk>/jugador/<slug:nick>/")
async def partida(sock, pk, nick):      # pk es int de verdad
    ...
```

Si `group=` menciona un parámetro que la ruta no tiene, **falla al importar**
con el nombre del culpable, no en la primera conexión.

El handler debe ser `async def`. Si le pasas una función normal, el error te
explica por qué y qué usar en su lugar.

---

## El objeto `sock`

### Recibir

```python
async for msg in sock: ...          # termina cuando el cliente cierra
msg.text   msg.bytes   msg.json()
if msg == "ping": ...               # Message se compara con str directamente

await sock.receive_text()           # también receive_json / receive_bytes
async for txt in sock.iter_text(): ...      # y iter_json()
```

### Enviar

```python
await sock.send("hola")             # str    -> texto
await sock.send(b"\x00")            # bytes  -> binario
await sock.send({"a": 1})           # resto  -> JSON
await sock.send_json(obj)           # también send_text / send_bytes
```

### Contexto de la conexión

```python
sock.user            # User o AnonymousUser, de la cookie de sesión
sock.session         # SessionStore de Django
sock.path_params     # {"room": "general"}
sock.query_params    # {"token": "abc"}   (query_lists si se repiten claves)
sock.headers   sock.cookies   sock.client   sock.subprotocols   sock.scope
```

### Control

```python
await sock.accept(subprotocol="graphql-ws")   # solo si necesitas negociar
await sock.close(4001, "sala llena")          # el código llega al onclose
await sock.deny()                             # tumba el handshake con un 403
```

No hace falta llamar a `accept()`: el handshake se completa solo la primera vez
que envías, recibes o iteras.

---

## Grupos y broadcast

```python
await sock.join("global")            # el primero pasa a ser el destino por defecto
await sock.leave("global")

await sock.broadcast(data)                    # al grupo por defecto
await sock.broadcast(data, to="otro:grupo")
await sock.broadcast(data, exclude_self=True)

sock.group     # destino por defecto de broadcast()
sock.groups    # de qué grupos eres miembro ahora mismo
```

`sock.group` y `sock.groups` no son lo mismo, a propósito. `groups` es de qué
grupos eres **miembro** (se vacía al desconectar); `group` es a dónde apunta
`broadcast()` por **defecto**, y sobrevive a la desconexión para que este patrón
funcione:

```python
async for msg in sock:
    await sock.broadcast(msg.text)
await sock.broadcast("alguien salió")     # <- aquí ya no eres miembro
```

### Desde fuera de un handler

```python
from django_socket import broadcast, broadcast_sync

await broadcast({"aviso": "mantenimiento"}, to="room:1")   # vista o tarea async
broadcast_sync({"aviso": "mantenimiento"}, to="room:1")    # vista sync, signal, Celery
```

> Con la capa por defecto (`memory`) `broadcast_sync` solo alcanza al proceso
> actual. Con varios workers necesitas Redis — ver [Producción](#producción).

---

## JSON

Es el caso normal, así que va sin fricción en los dos sentidos:

```python
@ws("eco/")
async def eco(sock):
    async for datos in sock.iter_json():        # ya parseado
        await sock.send_json({"vino": datos})   # dict/list -> JSON
```

### Los tipos de Django se serializan solos

Se usa `DjangoJSONEncoder`:

```python
await sock.send_json({
    "cuando": timezone.now(),      # "2026-08-26T19:43:30.251Z"
    "precio": Decimal("9.99"),     # "9.99"  (cadena: sin perder precisión)
    "id": uuid4(),                 # "0d8f...-..."
    "aviso": _("Hola"),            # las cadenas lazy también
})
```

<details>
<summary><b>Por qué la fecha importa más de lo que parece</b></summary>

Con `str()` saldría `"2026-08-26 19:43:30.251057+00:00"`, y eso tiene dos
problemas:

1. **ISO-8601 es el único formato que la especificación de ECMAScript obliga a
   `Date` a parsear.** El resto es un *fallback* de cada motor: V8 es permisivo
   y lo acepta, otros no siempre.
2. **`str()` emite microsegundos** (6 dígitos), que `Date` no sabe representar.
   El encoder trunca a milisegundos, que es lo que JS entiende.

</details>

### Un objeto que no sabe serializar falla de forma ruidosa

```
TypeError: No se puede enviar un Usuario por el socket. Los tipos de Django
habituales (datetime, date, time, timedelta, Decimal, UUID, cadenas lazy) van
solos; el resto conviértelo tú: un modelo a dict, un QuerySet a lista.
Si prefieres el comportamiento antiguo: sock.send_json(dato, default=str).
```

Es a propósito. Callarse y mandar `"Usuario object (3)"` al navegador es peor:
te enteras en producción.

### JSON inválido es culpa del cliente, no tuya

Si el cliente manda basura, la conexión se cierra con **4400 "Invalid JSON"** y
queda un `WARNING` en el log. Ni un `1011 Internal error`, ni un traceback que
te haga buscar un bug tuyo que no existe.

```python
from django_socket import InvalidJSON     # es una ValueError

async for msg in sock:
    try:
        datos = msg.json()
    except InvalidJSON:
        await sock.send_json({"error": "eso no era JSON"})
```

---

## Enrutar por tipo con `Events`

Casi toda app manda `{"type": "algo", ...}` y acaba con un if/elif largo.
`Events` lo convierte en funciones con nombre:

```python
from django_socket import Events, ws

tablero = Events()

@tablero.on("dibujar")
async def dibujar(sock, datos):
    await sock.broadcast({"type": "dibujar", **datos}, exclude_self=True)

@tablero.on("borrar")
async def borrar(sock):                 # si no usas los datos, no los pidas
    await sock.broadcast({"type": "borrar"})

@tablero.on("entrar", "salir")          # varios tipos de golpe
async def mover(sock, datos): ...

@tablero.on("*")                        # lo que no case con nada más
async def resto(sock, datos): ...

@ws("tablero/<str:sala>/", group="tablero:{sala}")
async def handler(sock, sala):
    await tablero.run(sock)
```

- El campo `type` **no** llega dentro de `datos`.
- `"*"` es un **respaldo**, no un espía: solo corre cuando nadie más cogió el
  mensaje.
- Un tipo que nadie maneja se ignora y deja un `WARNING` que lista los
  registrados — casi siempre es una errata.
- `Events(strict=True)` cierra con 4400 en su lugar.
- `Events(key="action")` cambia el nombre del campo.

Es completamente opcional: `async for msg in sock` sigue ahí.

---

## Usuarios y autenticación

`sock.user` está siempre disponible, resuelto de la cookie de sesión del
handshake. No hay que envolver nada en `asgi.py`.

```python
from django_socket import login_required, ws

@ws("panel/")
@login_required                 # cierra con 4401 si no hay sesión
async def panel(sock):
    await sock.send_json({"hola": sock.user.username})
```

### Auth por token, para SPAs y apps móviles

La cookie de sesión solo funciona si el navegador la manda, o sea con el
frontend servido desde el mismo sitio. Un React en otro dominio, o un cliente
móvil, no tiene cookie. Para esos:

```python
DJANGO_SOCKET = {
    "AUTH": ["session", "token"],       # se prueban en orden, gana el primero
    "TOKEN_RESOLVER": "miapp.auth.desde_jwt",
}
```

```python
async def desde_jwt(token):
    datos = jwt.decode(token, KEY, algorithms=["HS256"])
    return await User.objects.filter(pk=datos["sub"]).afirst()
```

La librería transporta el token; validarlo es cosa tuya, porque puede ser un
JWT, el de DRF, o algo propio. Si ya usas `rest_framework.authtoken` y no
configuras resolver, se usa ese.

**Por dónde viaja el token importa.** Los navegadores **no pueden poner
cabeceras** en un WebSocket — la API `new WebSocket(url, protocols)` solo deja
tocar la URL y `Sec-WebSocket-Protocol`. Así que:

| Vía | Quién puede | Nota |
|---|---|---|
| `Sec-WebSocket-Protocol: bearer, <token>` | navegadores | **la recomendada**: no va en la URL, luego no acaba en tus logs |
| `Authorization: Bearer <token>` | clientes nativos | los navegadores no pueden |
| `?token=<token>` | todos | **queda escrito en los logs de acceso**, el tuyo y el de cada proxy |

```js
new WebSocket("wss://api.ejemplo.com/feed/", ["bearer", token]);
```

Se leen en ese orden, así que si llegan varias gana la más segura.

### Por ruta, y el tuyo propio

```python
@ws("feed/", auth="token")                 # aquí solo token
@ws("panel/", auth=["session", "token"])   # cualquiera de las dos
@ws("publico/", auth=False)                # ni lo intentes; sock.user es None
@ws("iot/", auth=mi_autenticador)          # async(sock) -> user | None
```

Un autenticador es una función y ya:

```python
async def por_api_key(sock):
    clave = sock.query_params.get("k")
    return await Cliente.objects.filter(api_key=clave).afirst()
```

Un autenticador que no existe falla **al importar**, no en la primera conexión
de alguien.

> Con auth activo, `sock.user` es siempre un objeto usuario — `AnonymousUser`
> cuando nadie lo reconoció. Solo es `None` con `auth=False`, así que
> `sock.user.is_authenticated` nunca necesita comprobar `None` antes.

### El ORM funciona con normalidad

```python
@ws("usuarios/")
async def usuarios(sock):
    total = await User.objects.acount()                        # API async
    nombres = [u.username async for u in User.objects.all()]
    x = await sync_to_async(lambda: User.objects.first())()     # ORM síncrono
```

El handler corre dentro de un `ThreadSensitiveContext`, así que
`sync_to_async(thread_sensitive=True)` —el modo por defecto— comparte hilo igual
que en una vista.

---

## Cliente JavaScript

Reconectar bien es de esas cosas que todo el mundo reescribe y casi nadie
acierta. Viene incluido:

```html
{% load django_socket %}
{% ws_client %}

<script>
  const sock = djangoSocket("/chat/general/");

  sock.on("mensaje", (d) => pintar(d.texto));
  sock.on("entra",   (d) => avisar(`${d.quien} entró`));
  sock.on("*",       (d) => console.log("sin handler:", d));

  sock.send({type: "mensaje", texto: "hola"});   // objeto -> JSON
</script>
```

`ws://` o `wss://` según el protocolo de la página, JSON en los dos sentidos, y
enrutado por `type` igual que el `Events` de Python.

### Qué hace distinto

**No reconecta cuando el servidor cerró a propósito.** Un 4401 (falta login) o
un 4404 (ruta inexistente) no se arreglan reintentando: reconectar ahí es un
bucle infinito que machaca tu servidor. Se consideran definitivos el 1000, el
1008 y todo el rango 4000–4999; el resto (1006, 1011, cortes de red) sí se
reintenta.

**Backoff exponencial con jitter.** 0,5 s, 1 s, 2 s… hasta 15 s, cada espera
multiplicada por un factor aleatorio. Sin el jitter, mil clientes que se caen a
la vez vuelven todos en el mismo milisegundo y tumban el servidor otra vez.

**Encola lo que escribas mientras no hay conexión** y lo suelta al reconectar.
`send()` devuelve `false` si tuvo que encolar:

```js
if (!sock.send(texto)) mostrar("sin conexión: se enviará al reconectar");
```

**Sin red, no gasta intentos**: espera al evento `online` del navegador. Estando
oculta la pestaña **sí sigue reintentando**, a propósito — un chat en segundo
plano que deja de reconectar en silencio está roto. Si tu caso tolera quedarse
atrás, `{pauseWhenHidden: true}`.

### Opciones

```js
djangoSocket("/ruta/", {
  key: "type",             // el campo que enruta
  reconnect: true,
  minDelay: 500, maxDelay: 15000, maxRetries: Infinity,
  queue: true, maxQueue: 100,
  pauseWhenHidden: false,
  protocols: ["graphql-ws"],
  shouldReconnect: (e) => e.code !== 4001,   // tu propia regla
  onOpen(esReconexion) {}, onClose(e, reintentara) {},
  onRetry(intento, esperaMs) {}, onError(e) {}, onMessage(datos) {},
});
```

Y `sock.connected`, `sock.pending`, `sock.close()`, `sock.reconnect()`,
`sock.on(tipo, fn)`, `sock.off(tipo, fn)`.

---

## Middleware

Para lo que hay que hacer en todas las conexiones: trazas, métricas, reportar
errores, limitar conexiones.

```python
# miapp/ws.py
import time, logging

log = logging.getLogger("miapp.sockets")

async def medir(sock, siguiente):
    inicio = time.monotonic()
    try:
        await siguiente()
    finally:
        log.info("%s duró %.1fs", sock.path, time.monotonic() - inicio)
```

```python
DJANGO_SOCKET = {"MIDDLEWARE": ["miapp.ws.medir"]}
```

Se aplican en orden: el primero de la lista es el más externo, igual que el
`MIDDLEWARE` de Django. Corre **después** de autenticar, así que `sock.user` ya
está resuelto. Para cortar una conexión, cierra y no llames a `siguiente()`:

```python
async def solo_de_pago(sock, siguiente):
    if not await es_de_pago(sock.user):
        await sock.close(4403, "Plan insuficiente")
        return
    await siguiente()
```

Vienen dos hechos:

```python
from django_socket.middleware import log_connections, max_connections_per_user

DJANGO_SOCKET = {"MIDDLEWARE": [
    max_connections_per_user(10),   # cierra la 11ª con 4429
    log_connections(),              # una línea de log por conexión
]}
```

`max_connections_per_user` cuenta por proceso: con N workers el tope real es
`límite × N`. Uno global necesitaría contadores en Redis, y eso solo compensa
si de verdad necesitas esa precisión.

---

## Testear tus handlers

Sin levantar servidor, sin puertos, en milisegundos:

```python
from django_socket.testing import WebSocketClient

async def test_el_chat_reparte():
    async with WebSocketClient("/chat/general/") as a, \
               WebSocketClient("/chat/general/") as b:
        await b.send_json({"type": "mensaje", "texto": "hola"})
        assert (await a.receive_json())["texto"] == "hola"
```

Pasa por el mismo camino que una conexión real —ruta, conversores, validación de
origen, sesión, grupos— pero hablando ASGI directamente contra el dispatcher.

```python
# Usuario autenticado sin montar cookies a mano
async with WebSocketClient("/panel/", user=mi_user) as c:
    assert (await c.receive_json())["quien"] == "ramon"

# Rechazos
async with WebSocketClient("/panel/") as c:
    assert await c.wait_closed() == 4401
    assert not c.connected

# Aislamiento entre salas
async with WebSocketClient("/sala/uno/") as a, WebSocketClient("/sala/dos/") as b:
    await b.send("privado")
    assert await a.receive_nothing()
```

| | |
|---|---|
| `send` / `send_json` / `send_text` / `send_bytes` | `str`→texto, `bytes`→binario, resto→JSON |
| `receive` / `receive_json` / `receive_text` / `receive_bytes` | con timeout, fallan rápido |
| `receive_all()` | todo lo pendiente ahora mismo |
| `receive_nothing()` | `True` si no llega nada — para afirmar aislamiento |
| `wait_closed()` | el código de cierre |
| `connected` `accepted` `close_code` `close_reason` `subprotocol` | estado |

`WebSocketClient(path, user=, headers=, cookies=, query=, subprotocols=, origin=)`

> `connected` y `accepted` no son lo mismo: `accepted` dice si llegó un
> `websocket.accept`, y un rechazo con código se ve en el protocolo como accept
> + close (para que el código llegue al navegador). Para «¿me dejó entrar?» usa
> `connected`.

Todos los `receive` llevan timeout de 1 s: un test que espera algo que no llega
falla en un segundo con el path en el mensaje, en vez de colgar la suite.

---

# Operación

## Producción

El `asgi.py` de tu proyecto ya sirve, sin cambios:

```bash
uvicorn miproyecto.asgi:application --host 0.0.0.0 --port 8000 --workers 4
```

```bash
gunicorn miproyecto.asgi:application -k uvicorn.workers.UvicornWorker -w 4
```

### Detrás de un proxy inverso

Aquí es donde se rompe la mayoría de despliegues con WebSockets, y el síntoma
—un 400 o un 502— parece un bug de la librería sin serlo. Hay que decirle al
proxy que deje pasar el cambio de protocolo:

```nginx
location /ws/ {
    proxy_pass http://app;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;      # <- estas dos
    proxy_set_header Connection "upgrade";       # <- son todo el asunto
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_read_timeout 3600s;                    # o corta los sockets a los 60s
}
```

Caddy no necesita nada, hace de proxy para WebSockets por defecto:

```
ejemplo.com {
    reverse_proxy app:8000
}
```

**Cómo saber si tu proxy se está comiendo el upgrade.** Un handshake correcto
es un `HTTP/1.1 101 Switching Protocols`. Compruébalo sin navegador de por
medio:

```bash
curl -i -N -o -   -H "Connection: Upgrade" -H "Upgrade: websocket"   -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ=="   -H "Sec-WebSocket-Version: 13"   -H "Origin: https://ejemplo.com"   https://ejemplo.com/ws/echo/
```

- `101` — el proxy está bien, el problema está en otro sitio.
- `400` — llegó a Django, pero rechazó el origen o la ruta. Mira `manage.py ws`
  y tu `ALLOWED_HOSTS`.
- `200` o `502` — el proxy se tragó el upgrade. Faltan esas dos cabeceras.

Si vas detrás de un balanceador, ten en cuenta que un WebSocket es una conexión
larga: sube el timeout de inactividad (el ALB viene con 60 s) o tus sockets se
caerán cada minuto sin motivo aparente.

### Con más de un worker, necesitas Redis

La capa de memoria solo conoce los sockets de su propio proceso, así que un
broadcast no cruza de un worker a otro.

```python
DJANGO_SOCKET = {"LAYER": "redis", "REDIS_URL": "redis://localhost:6379/0"}
```

```bash
pip install "django-socket[redis]"
```

Cada proceso publica sus broadcasts en un canal pub/sub y entrega a sus miembros
locales lo que recibe del resto. Tus handlers no cambian: `sock.broadcast(...)`
sigue siendo la misma llamada.

Verificado con dos workers reales contra Redis 7.4: el mensaje cruza en ambos
sentidos, las salas siguen aisladas, y el proceso que publica no se entrega su
propio eco dos veces.

### Si Redis se cae

Un corte de Redis **no tumba las conexiones de tus usuarios**:

- la entrega dentro del proceso sigue funcionando con normalidad
- `broadcast()` no lanza; registra un `ERROR` diciendo que el grupo solo recibió
  la entrega local
- el proceso reintenta suscribirse con backoff exponencial (0,5 s → 10 s)
- cuando Redis vuelve, se resuscribe solo y el fan-out se reanuda sin reiniciar

Lo que se publique mientras Redis está caído **se pierde**: esto es pub/sub, no
una cola. Si necesitas garantía de entrega hay que persistir el mensaje tú, con
la base de datos o una cola de verdad.

---

## Seguridad: validación de origen

**Los WebSockets no están sujetos a la política de mismo origen.** Sin validar
el header `Origin`, cualquier web puede abrir un socket contra la tuya y el
navegador adjuntará las cookies de sesión de la víctima
(*cross-site WebSocket hijacking*). Por eso la comprobación no es opcional.

**Aquí va activado por defecto.** Se valida contra `ALLOWED_HOSTS` +
`CSRF_TRUSTED_ORIGINS`, y un origen ajeno se rechaza con un 403 **en el
handshake**, antes de aceptar nada.

Un `Origin` **ausente** se acepta, porque los navegadores siempre lo mandan:
solo lo omiten clientes nativos (una app móvil, un script). Si tu endpoint es
solo para navegadores, ponlo estricto:

```python
DJANGO_SOCKET = {
    "REQUIRE_ORIGIN": True,
    # o una lista explícita, que ignora ALLOWED_HOSTS:
    "ALLOWED_ORIGINS": ["https://miapp.com", "https://admin.miapp.com"],
}
```

`manage.py check` avisa si dejas `"*"` con `DEBUG=False`.

---

## Límites de tasa

Antes de exponer un socket al público, acota lo rápido que puede mandar un
cliente:

```python
@ws("chat/", rate_limit="60/m")            # por ruta
DJANGO_SOCKET = {"RATE_LIMIT": "60/m"}     # o para todas
```

Formatos: `"10/s"`, `"60/m"`, `"100/5m"`, `"1000/h"`. Pasarse cierra con
**4429**, y el motivo dice cuánto esperar.

Es un **token bucket, no un contador por ventana**, y esa diferencia es lo que
lo hace usable: un contador rechaza el mensaje 11 aunque los 10 anteriores
fueran de hace 59 segundos. El cubo se rellena de forma continua, así que
aguanta la ráfaga normal de alguien escribiendo rápido y solo corta cuando el
ritmo *sostenido* se pasa.

Para flujos que legítimamente dan picos, sube el burst sin subir el ritmo
sostenido:

```python
@ws("cursor/", rate_limit="30/s", burst=100)
```

El límite es por socket. **No** es una defensa contra una botnet abriendo miles
de conexiones: para eso están `max_connections_per_user` y algo por delante
de la aplicación.

---

## Clientes lentos

Un cliente que deja de leer —mala red, móvil que se duerme, o alguien con mala
idea— no puede arrastrar a los demás.

<details>
<summary><b>El problema, medido</b></summary>

uvicorn sí aplica contrapresión: acumula unos **24 MB** hacia un cliente que no
lee y a partir de ahí `send()` deja de volver. Con un reparto que esperaba a
cada miembro, eso significaba que `broadcast()` **no volvía nunca**: el handler
que difunde se quedaba colgado, dejaba de leer su propio socket y jamás
ejecutaba su limpieza. Un solo cliente en mala red mataba la sala entera.

</details>

**Cómo se resuelve.** Cada socket tiene un buzón acotado y una tarea que lo
escribe. `broadcast()` encola y vuelve; no espera a nadie. Si el buzón de alguien
se llena, ese cliente va demasiado atrasado y se le echa con un **1013 (Try
Again Later)** en vez de dejar que frene al grupo.

```python
DJANGO_SOCKET = {
    "SEND_QUEUE_MAX": 256,       # mensajes en cola por socket
    "SEND_QUEUE_FULL": "close",  # "close" | "drop_oldest"
}
```

`drop_oldest` es para flujos que toleran huecos —posiciones de cursor,
telemetría, un contador en vivo—: mejor perder un valor viejo que echar al
cliente. Para un chat quieres `close`.

**Cómo dimensionarlo.** El buzón se mide en mensajes, pero lo que importa es
*tiempo*: `buzón ÷ mensajes_por_segundo = segundos de tolerancia`. Medido, un
proceso publica ~1.500 broadcast/s contra Redis, así que 256 son ~165 ms en el
peor caso y decenas de segundos al ritmo de un chat normal. La memoria solo la
pagan los clientes atascados —uno que consume tiene el buzón a cero—, así que el
coste es `atascados × 256 × tamaño_del_mensaje`.

**`sock.send()` sigue esperando, a propósito.** Ahí el bloqueo es sano: en un
flujo uno-a-uno, si el cliente no puede seguirte lo correcto es que tu handler
vaya más despacio. El buzón es solo para la difusión, que es donde esperar hace
daño.

**En un test, `await sock.drain()`** espera a que salga lo encolado, para
afirmar sin dormir a ciegas. En producción no hace falta.

---

## Conexiones zombis

Un portátil al que le cierran la tapa deja una conexión TCP "abierta" con nadie
al otro lado: ni `close`, ni error. Sin detección, ese socket se queda en su
grupo para siempre y le difundes al vacío.

**No hace falta que hagas nada, ni que esta librería añada un heartbeat.**
uvicorn ya manda pings de protocolo y cierra lo que no responde. Medido con un
cliente que completa el handshake y luego se queda absolutamente mudo:

```
t=  10s  en el grupo: 1
t=  20s  en el grupo: 1
t=  30s  en el grupo: 1
detectado y limpiado a los 39s
```

39 segundos: `ws_ping_interval` (20 s) + `ws_ping_timeout` (20 s). El handler
sale por su `finally`, el socket abandona sus grupos y todo se limpia solo.

Si necesitas detectarlo antes:

```bash
uvicorn proyecto.asgi:application --ws-ping-interval 5 --ws-ping-timeout 5
```

> Esto es comportamiento de uvicorn, no del protocolo. Con otro servidor ASGI,
> compruébalo.

---

## Configuración completa

Todo opcional. Estos son los valores por defecto:

```python
DJANGO_SOCKET = {
    "LAYER": "memory",            # "memory" | "redis" | callable -> BaseLayer
    "REDIS_URL": "redis://localhost:6379/0",
    "PREFIX": "djws",             # prefijo del canal de Redis
    "ALLOWED_ORIGINS": None,      # None = ALLOWED_HOSTS + CSRF_TRUSTED_ORIGINS
    "REQUIRE_ORIGIN": False,      # True = rechaza también sin Origin
    "PATCH_ASGI": True,           # False = declara ASGIApplication() tú mismo
    "SEND_QUEUE_MAX": 256,        # mensajes encolados por socket, para difusión
    "SEND_QUEUE_FULL": "close",   # "close" (echa al lento) | "drop_oldest"

    "AUTH": ["session"],          # autenticadores, se prueban en orden
    "TOKEN_RESOLVER": None,       # async(token) -> user | None
    "MIDDLEWARE": [],             # envoltorios async(sock, siguiente)
    "RATE_LIMIT": None,           # "60/m" para todas las rutas
    "RATE_LIMIT_BURST": None,     # margen por encima del ritmo sostenido
}
```

Una clave mal escrita la caza `manage.py check` (`django_socket.E001`).

---

## Códigos de cierre

| Código | Significado |
|---|---|
| `4400` | El cliente mandó algo que no es JSON válido |
| `4401` | `@login_required` y no hay sesión |
| `4404` | Ninguna ruta casa con ese path |
| `4429` | Demasiado rápido: límite de tasa, o demasiadas conexiones a la vez |
| `1013` | El cliente no consume: buzón lleno, se le echa del grupo |
| `1011` | Excepción sin capturar en el handler (queda en el log) |
| HTTP 403 | Origin no permitido — el handshake ni llega a completarse |

`sock.close(code, reason)` acepta el handshake antes de cerrar si aún no estaba
aceptado, precisamente para que tu código llegue al `onclose` del cliente.
Cerrar sin aceptar produce un HTTP 403 y el navegador solo ve un `1006` sin
motivo, que no le sirve a nadie para depurar.

---

# Referencia

## Cómo funciona por dentro

Cuatro decisiones explican casi todo el comportamiento de la librería.

### El estado de una conexión vive en el stack de una corrutina

Un WebSocket es una conversación con principio y fin. Modelarlo como una
función `async` que corre de principio a fin significa que el estado va en
variables locales y el flujo se lee de arriba abajo:

```python
@ws("partida/<int:pk>/", group="partida:{pk}")
async def partida(sock, pk):
    mano = repartir()                     # estado: una variable local
    turnos = 0

    async for jugada in sock.iter_json():
        turnos += 1
        mano = aplicar(mano, jugada)
        await sock.broadcast({"turnos": turnos})

    await registrar_abandono(pk, turnos)  # la desconexión es el final
```

No hay atributos de instancia que sincronizar entre callbacks, ni un objeto que
sobreviva a la conexión. Cuando la corrutina termina, no queda nada que limpiar
salvo lo que el propio `finally` ya hace.

### Ampliar la puerta de Django en lugar de montar otra

Django ya es ASGI. Su handler simplemente se niega a mirar el scope
`websocket`, y lo hace con un `FIXME` en el código. Como `django.setup()`
ejecuta los `ready()` de las apps antes de instanciar ese handler, el
`AppConfig` llega a tiempo de ampliarlo.

De ahí sale la propiedad más visible: **instalar es añadir una línea a
`INSTALLED_APPS`**. Sin `asgi.py` que reescribir, sin envoltorios anidados, sin
un segundo árbol de rutas paralelo al de Django. Y el tráfico HTTP no pasa por
aquí en ningún momento.

### La difusión no espera a nadie

Cada socket tiene un buzón acotado y una tarea que lo escribe. `broadcast()`
encola y vuelve.

La alternativa —esperar a que cada miembro haya recibido— parece más simple
hasta que la mides: un cliente que deja de leer hace que `send()` deje de
volver, y con eso el handler que difunde se queda colgado para siempre. Deja de
leer su propio socket y nunca ejecuta su limpieza. Un móvil con mala cobertura
tumba la sala entera. Con buzón, ese cliente se queda atrás solo y acaba
expulsado sin arrastrar a nadie.

`sock.send()` sí espera, y eso es deliberado: en un flujo uno-a-uno la
contrapresión es sana. Solo la difusión necesita desacoplarse.

### La capa de difusión es reemplazable

`sock.broadcast(...)` es la misma llamada tanto si estás en un proceso como en
doce. Lo único que cambia es qué capa hay debajo: en memoria para un proceso,
Redis pub/sub para varios, o la tuya implementando `BaseLayer`.

Los handlers no se enteran. No hay que reescribir código para escalar, ni
mantener dos caminos según el despliegue.

---

---

## Rendimiento

Medido en un portátil, Redis 7.4 en Docker, todo contra `localhost`. Repetible:

```bash
python bench_redis.py      # coste de la capa
python bench_carga.py      # conexiones concurrentes
```

### Coste de la capa

| | |
|---|---|
| broadcast, memoria, 1 miembro | 0,003 ms |
| broadcast, memoria, 1000 miembros | 0,104 ms |
| broadcast, Redis | ~0,65 ms → **~1.500/s por proceso** |
| latencia de cruce entre procesos | mediana 0,59 ms · p95 0,80 ms · p99 0,90 ms |

Con Redis el coste es casi plano con el número de miembros: domina el viaje a
Redis, no el reparto local. Los ~1.500 broadcast/s son por proceso, así que
escalan con los workers.

### Conexiones concurrentes

Un proceso, capa memoria, un servidor recién arrancado por medida:

| conexiones | memoria | fan-out p50 | p95 | entrega |
|---|---|---|---|---|
| 1.000 | 125 MB · 122 KB/conn | 28 ms | 40 ms | 100 % |
| 3.000 | 371 MB · 121 KB/conn | 157 ms | 200 ms | 100 % |
| 6.000 | 741 MB · 121 KB/conn | 130 ms | 216 ms | 100 % |

A 6.000 conexiones: todas abren (1.600/s), y 120.000 mensajes difundidos llegan
**sin perder ninguno**, a ~24.500 msg/s. Con la capa Redis a 1.000 conexiones el
fan-out sube de 28 a 58 ms de mediana —el viaje a Redis— con la misma memoria y
la misma entrega íntegra.

**La memoria es el límite que manda**: ~121 KB por conexión, constante desde las
1.000 hasta las 6.000. Son unas **8.000 conexiones por GB**, y ese número no lo
pone esta librería sino uvicorn y los búferes de socket.

### Lo que estas cifras NO dicen

- El cliente del benchmark es **un solo proceso Python** leyendo N websockets,
  así que las latencias incluyen su propio planificador. Con clientes reales
  repartidos serían mejores.
- Todo es `localhost`: sin latencia de red, sin pérdida, sin TLS.
- No se ha probado con mensajes grandes, ni con Redis remoto, ni durante horas.

Vuelve a correr los benchmarks en tu entorno antes de dimensionar nada.

---

## Límites conocidos

- **Todo lo medido es `localhost`**: hay números hasta 6.000 conexiones y con
  Redis, pero no contra red real, TLS, mensajes grandes ni sesiones largas.
- **Django 4.2 → 6.1 y Python 3.10 → 3.13 pasan en CI**, incluido el camino
  alternativo para el `aget_user` que no existe antes de Django 5.0: en 4.2 se
  ejecuta de verdad, no forzado con un monkeypatch. Lo que sigue sin cubrir la
  CI es Windows y macOS — solo corre en Linux.
- **Los ~24 MB que uvicorn buferea por conexión** están debajo de esta capa: el
  buzón acota lo que se acumula encima, no lo de abajo. Con 100 clientes
  atascados a la vez son 2,4 GB que no se pueden evitar desde el nivel de
  aplicación; eso se limita en el servidor.
- **Sin ejecutor de tareas en segundo plano.** Para trabajo diferido usa Celery
  o el runner que ya tengas, y avisa por el socket con `broadcast_sync`.
- **Handlers síncronos no soportados**, a propósito: `@ws` exige `async def` y
  lo explica al fallar. Un `def` normal ocuparía un hilo por conexión abierta.
- **La resolución de rutas es lineal y gana la primera que casa**, igual que en
  Django. Con cientos de rutas convendría indexar.
- `broadcast_sync` con `LAYER="memory"` solo alcanza al proceso actual — es lo
  esperable, pero es fácil tropezar en desarrollo y no verlo hasta producción.

---

## Desarrollo

```bash
git clone https://github.com/ramon3198/django-socket.git && cd django-socket
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### Tests

```bash
pytest                              # 261 tests, ~10 s, sin levantar nada
node --test tests/js/*.test.js      # 35 tests del cliente JS, sin npm install
```

La suite de Python no necesita servidor: usa el mismo `WebSocketClient` que
documentamos arriba, más un transporte falso para lo de más bajo nivel.
Cobertura: **94 %**.

El cliente JS usa el runner y los timers falsos que trae Node (≥20), así que no
hay que instalar nada. Cubre el backoff con jitter, qué códigos no se
reintentan, la cola durante el corte, la pausa sin red y el enrutado por `type`.

### Contra servicios reales

```bash
# Redis de verdad: los tests lo detectan solos y te dicen cuál usan
docker run -d --rm -p 6379:6379 redis:7-alpine
pytest tests/test_redis_layer.py -s

# Integración contra un servidor en marcha
python manage.py runserver 8000
python test_sockets.py 8000                 # 24 tests

# Fan-out entre procesos separados (con memory, este DEBE colgarse)
DJANGO_SOCKET_LAYER=redis python manage.py runserver 8091
DJANGO_SOCKET_LAYER=redis python manage.py runserver 8092
python test_multiproceso.py 8091 8092
```

### Demo

```bash
python manage.py migrate
python manage.py runserver
```

- `http://127.0.0.1:8000/ejemplos/` — **cuatro patrones que la gente construye
  de verdad**, cada uno ejecutable en la página: progreso de una tarea en
  segundo plano, notificaciones por usuario, presencia y un dashboard en vivo.
  El código comentado está en [`ejemplos/sockets.py`](ejemplos/sockets.py).
- `http://127.0.0.1:8000/sala/general/` — chat, ábrelo en dos pestañas. Para ver
  la reconexión, para el servidor y vuelve a arrancarlo.
- `chat/sockets.py` — todos los ejemplos en un archivo.

---

## Licencia

MIT.
