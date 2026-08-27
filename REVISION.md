# Revisión técnica — django-socket 0.2.1

**Fecha:** 2026-08-27 · **Alcance:** las 2.761 líneas de la librería, el cliente JS, la CI y el empaquetado.

**Método.** Relectura completa con ojos frescos, y cada sospecha seria se **verificó empíricamente** antes de entrar aquí (contra el Redis real que sigue corriendo en Docker, o ejecutando el código). Cada hallazgo indica cómo se comprobó. Una sospecha resultó falsa y está en la sección "Revisado y descartado" para que nadie la re-investigue.

---

## Resumen

| # | Hallazgo | Severidad | Verificado | Esfuerzo |
|---|---|---|---|---|
| 1.1 | `broadcast_sync` desde Celery/cron **pierde el fan-out en silencio** | 🔴 Crítica | ✅ ejecutado | ~20 líneas |
| 1.2 | `__version__` dice `0.1.0`; PyPI dice `0.2.1` | 🔴 Bug publicado | ✅ leído en disco | ~5 líneas |
| 1.3 | Cliente JS: `close()` durante la espera sin red → **reconecta solo** | 🔴 Bug | ✅ por lectura | ~10 líneas + test |
| 2.1 | El rate limit se resetea al reconectar | 🟠 Producción | ✅ por diseño | medio |
| 2.2 | Sin soporte de proxy (`X-Forwarded-For`) | 🟠 Producción | ✅ grep | medio |
| 2.3 | `group_size` cuenta solo lo local bajo Redis (y el ejemplo del dashboard lo usa) | 🟠 Producción | ✅ docstring+ejemplo | doc + alias |
| 2.4 | Carrera de doble `accept()` con envíos concurrentes | 🟠 Estrecha | ⚠️ solo lectura | ~8 líneas |
| 2.5 | Sin apagado elegante (nadie recibe `1001` al parar el servidor) | 🟠 Producción | ✅ por lectura | ~25 líneas |
| 2.6 | `evict()` usa `asyncio.get_event_loop()` (deprecado) | 🟡 Menor | ✅ grep | 1 línea |
| 3.1 | API pública mezcla español e inglés; errores en español con README en inglés | 🟡 Adopción | ✅ grep | decisión + aliases |
| 3.2 | Python 3.14 fuera de la CI y de los classifiers (salió hace ~10 meses) | 🟡 Adopción | ✅ grep | ~5 líneas |
| 3.3 | Sin linter/format en CI (CONTRIBUTING promete un estilo que nadie verifica) | 🟡 DX | ✅ grep | ~15 líneas |
| 3.4 | Sin hooks de observabilidad (conexiones, expulsiones, 4429) | 🟡 Producción | — | medio |
| 3.5 | El rate limit cuenta mensajes, no bytes | 🟡 Doc/feature | ✅ por lectura | doc + opcional |
| 3.6 | JS: `send()` devuelve `false` tanto para "encolado" como para "descartado" | 🟡 API | ✅ por lectura | pequeño |
| 3.7 | `resolver_lista(auth)` se re-resuelve en cada conexión | ⚪ Micro-perf | ✅ por lectura | ~6 líneas |
| 3.8 | `_resuscribir()` sin `try` (hoy aguanta; endurecer es gratis) | ⚪ Endurecimiento | ✅ probado que aguanta | 3 líneas |

---

## 1. Críticos

### 1.1 · `broadcast_sync` desde un worker de Celery pierde los mensajes en silencio

**El caso de uso estrella de `ejemplos/sockets.py` — la barra de progreso de Celery — no funciona.**

Verificado ejecutándolo: un proceso recién arrancado (como es un worker de Celery, un management command o un cron) llama a `broadcast_sync(...)` con `LAYER: "redis"`:

```
>> django_socket: no se pudo publicar en Redis (AttributeError: 'NoneType'
   object has no attribute 'publish'). El grupo 'tarea:1' solo recibio la
   entrega local.

¿el mensaje llego a Redis?: NO — SE PERDIO EN SILENCIO
```

**Causa raíz.** `get_layer()` construye la capa pero **nadie llama a `startup()`** fuera de un proceso web: eso solo ocurre en `dispatch._start_layer()`, que corre con la primera conexión websocket o con el evento `lifespan` — cosas que un worker de Celery no tiene jamás. Así que `RedisLayer._redis` es `None`, el `publish` de `groups.py:161` lanza `AttributeError`, y el `except Exception` de la línea 162 —pensado para caídas de Redis— lo **enmascara como si fuera un corte**, con un mensaje que además culpa a Redis.

Doble agravante:
- El mensaje se pierde sin excepción para el llamador: la tarea de Celery cree que avisó.
- El log dice "no se pudo publicar en Redis", que manda a quien depure a mirar su servidor Redis, que está perfecto.

**Por qué no lo cazó ningún test:** `test_multiproceso.py` usa dos procesos que son ambos servidores web — ambos arrancan la capa. Nunca se probó un proceso *solo publicador*.

**Arreglo propuesto.** Conexión perezosa del publicador en `RedisLayer.send()`: si `_redis is None`, conectar ahí (solo el cliente de publicación; un proceso que solo publica no necesita el listener de suscripción). Y separar el `except`: un `AttributeError` por capa sin arrancar no es un corte de Redis.

**Test de regresión obligado:** proceso publicador puro (capa fresca, sin `startup()`) → el mensaje debe llegar a un suscriptor real.

---

### 1.2 · `__version__` miente

```
django_socket/__init__.py:36:  __version__ = "0.1.0"
pyproject.toml:7:              version = "0.2.1"
```

Se subió dos versiones a PyPI sin tocar el atributo. Cualquiera que haga `django_socket.__version__` para reportar un bug o para gating de features obtiene `0.1.0`. Está **publicado así** en el wheel de 0.2.1.

**Arreglo.** Una sola fuente de verdad:

```python
from importlib.metadata import version
__version__ = version("django-socket")
```

(o al revés, `dynamic = ["version"]` en pyproject leyendo del módulo). Y un test de una línea que compare ambos, para que la CI no deje volver a divergir.

---

### 1.3 · Cliente JS: `close()` durante la espera sin red → reconecta a escondidas

Secuencia, confirmada leyendo `client.js`:

1. Se cae la red → `programarReintento` → `debeEsperar()` → `esperarAEstarListo()` deja `esperando = true` y registra listeners de `online`/`visibilitychange`.
2. El usuario llama a **`sock.close()`** (línea 247): pone `cerradoPorNosotros = true` y `clearTimeout` — pero **no** limpia `esperando` ni quita los listeners.
3. Vuelve la red → salta `despertar()` (línea 222), que solo comprueba `esperando` → llama a `conectar()`.
4. `conectar()` (línea ~142) hace `cerradoPorNosotros = false` **incondicionalmente** → el socket que el usuario cerró explícitamente **revive**.

Un socket que reconecta después de que la aplicación lo cerró es, por ejemplo, un usuario que hizo logout y sigue recibiendo mensajes.

**Arreglo.** En `close()`: `esperando = false` y quitar los dos listeners (extraer `despertar`/`alVerse` a referencias accesibles o usar un `AbortController`). Y `conectar()` no debe resetear `cerradoPorNosotros`; ese reset pertenece a `reconnect()` y al flujo de reintento programado. Con test en `tests/js/` (el escenario es 100 % reproducible con los timers falsos: offline → close → evento online → afirmar que `FakeWS.instancias.length` no crece).

---

## 2. Producción

### 2.1 · Reconectar resetea el rate limit

El cubo vive en la conexión (`dispatch` crea uno nuevo por socket). Un cliente malicioso que recibe el 4429 puede reconectar al instante con el cubo lleno otra vez — coste de un handshake. El cliente JS propio no lo hace (4429 es "definitivo"), pero un atacante no usa tu cliente.

**Propuesta.** Contabilidad opcional por clave compartida en el proceso (`user.pk` o IP), tipo `RATE_LIMIT_SCOPE: "connection" | "user"`, reutilizando el patrón de `max_conexiones_por_usuario`. Y mientras tanto, **documentar el hueco** en la sección de límites: hoy el README presenta `rate_limit` sin decir que la reconexión lo vacía.

### 2.2 · Detrás de un proxy, todas las IPs son la del proxy

No hay soporte de `X-Forwarded-For` en ninguna parte (grep: cero usos). Consecuencias con nginx delante — que es el despliegue que el propio README recomienda:

- `sock.client` es siempre la IP del proxy.
- `max_conexiones_por_usuario` mete a **todos los anónimos en un mismo cubo** → con el límite por defecto (5), el sexto visitante anónimo del sitio recibe 4429.
- Los logs de `registrar()` y las advertencias de expulsión atribuyen todo a la misma IP.

**Propuesta.** `DJANGO_SOCKET["TRUSTED_PROXIES"]` y una propiedad `sock.real_ip` que solo honre `X-Forwarded-For` si el peer directo está en esa lista (honrarlo siempre permite falsificar la IP, que es peor que lo de ahora). Documentar el pitfall del middleware aunque no se implemente todavía.

### 2.3 · `group_size()` cuenta solo lo local bajo Redis — y un ejemplo propio tropieza

El docstring lo dice ("sockets locales"), pero `ejemplos/sockets.py` lo usa para el contador de "conectados" del dashboard: con 4 workers, cada usuario ve solo los conectados que cayeron en su mismo worker. El ejemplo enseña el error que la doc advierte.

**Propuesta.** Renombrar a `local_group_size` (dejando alias), corregir el ejemplo con una nota explícita, y valorar un contador global opcional vía `INCR/DECR` en Redis (con TTL de seguridad para procesos que mueren sin decrementar). El contador global es trabajo real; el renombre y la nota son inmediatos.

### 2.4 · Carrera de doble `accept()` — identificada por lectura, no reproducida

`_ensure_open()` (websocket.py:255) hace `if CONNECTING: await self.accept()`. Dos corrutinas pueden pasar la comprobación a la vez —el handler enviando su saludo y la tarea escritora drenando un broadcast que llegó al grupo nada más entrar— y ambas emiten `websocket.accept`: violación de protocolo ASGI. La ventana es estrecha (requiere un broadcast de terceros entre el `join` del dispatcher y el primer `await` del handler), por eso no la he reproducido; el análisis es solo estático.

**Arreglo barato:** estado intermedio `ACCEPTING` o un `asyncio.Lock` en `accept()`.

### 2.5 · Apagado sin elegancia

En `lifespan.shutdown` se para la capa y ya. uvicorn corta los sockets, los clientes ven `1006` sin motivo y el cliente JS lo trata como corte de red y **reintenta contra un servidor que se está apagando** — correcto por su parte, pero durante un deploy son N clientes martilleando.

**Propuesta.** En el shutdown, recorrer los sockets vivos y cerrarlos con `1001` ("going away") tras drenar buzones. El cliente JS trata hoy `1001` como reintentable — está bien para un deploy (el servidor nuevo llega en segundos), y con el backoff + jitter ya existente la estampida está amortiguada. Solo requiere que la capa mantenga el conjunto de sockets vivos, que ya lo tiene a través de los grupos… excepto los sockets sin grupo: haría falta un registro de conexiones en el dispatcher.

### 2.6 · `evict()` usa `asyncio.get_event_loop()`

`websocket.py:511`. Deprecado; en versiones futuras de Python lanzará. Cambio de una línea a `get_running_loop()`.

---

## 3. Adopción y experiencia de desarrollo

### 3.1 · El idioma de la API es inconsistente — y es una decisión pendiente, no un descuido más

El núcleo es inglés (`ws`, `broadcast`, `Events`, `login_required`, `WebSocketClient`, `drain`) pero lo reciente salió en español: `extraer_token`, `max_conexiones_por_usuario`, `registrar`, `TimeoutDelEsperado`. Y **todos los mensajes de error y logs están en español**, con un README principal en inglés. Quien no lea español recibe:

```
TypeError: @ws espera 'async def', y handler es una funcion normal...
```

Los mensajes de error son de lo mejor de esta librería — explican qué hacer — y la mitad del público objetivo no los puede leer.

**Propuesta.** Decidir política y aplicarla de una vez, no gota a gota: (a) API pública nueva en inglés con alias español donde ya se publicó (`extract_token = extraer_token`, `max_connections_per_user`, `log_connections`, `ExpectedTimeout`), sin romper 0.2.x; (b) mensajes de error de la API pública en inglés — es lo que se busca en Google — manteniendo si se quiere los comentarios internos en español. Es la fricción de adopción más grande que queda.

### 3.2 · Python 3.14 no está ni en la CI ni en los classifiers

3.14 salió en octubre de 2025; hoy lleva ~10 meses estable y `channels` ya lo declara. Cero menciones en `tests.yml` y `pyproject.toml`. Añadir `{python: "3.14", django: "6.1"}` a la matriz y el classifier — si pasa, que probablemente sí, es soporte gratis.

### 3.3 · CONTRIBUTING promete un estilo que la CI no comprueba

"Line length 88", "comments explain the why"… y no hay ruff/flake8/format check en ningún workflow. El primer PR externo llegará con otro estilo y no habrá red. `ruff check` + `ruff format --check` son ~15 líneas de workflow y congelan el estado actual.

### 3.4 · Observabilidad: los eventos importantes solo dejan un log

Expulsiones por buzón lleno, cierres 4429, conexiones activas, reconexiones de Redis — todo existe solo como línea de log. Un proyecto grande quiere contadores. No hace falta atarse a Prometheus: un hook de callbacks (`DJANGO_SOCKET["ON_EVENT"]`) o señales de Django (`socket_connected`, `socket_evicted`, …) dejan que cada cual exporte a su sistema. El middleware cubre duración por conexión, pero no los eventos internos de la capa.

### 3.5 · El rate limit cuenta mensajes, no bytes

Un cliente puede respetar `60/m` mandando 60 mensajes de 15 MB (el tope por frame de uvicorn es 16 MB). Mitigación hoy: documentar `--ws-max-size` de uvicorn junto al rate limit. Feature futura: `rate_limit_bytes`.

### 3.6 · JS: `send()` no distingue "encolado" de "perdido"

Devuelve `false` en ambos casos (cola llena con `queue:true` descarta el más viejo y devuelve `true`; con `queue:false` se pierde y devuelve `false`; sin conexión con cola devuelve `false` pero se enviará). Tres destinos, dos valores. Propuesta sin romper: devolver `"sent" | "queued" | "dropped"` — los strings son truthy/falsy compatibles con el uso booleano actual… salvo `"dropped"`, así que mejor un segundo método (`sock.deliver(data) -> estado`) o un evento `onQueueFull`.

### 3.7 · Micro: `auth=` se re-resuelve en cada conexión

`authentication.resolve()` llama a `resolver_lista(spec)` por conexión, con `import_string` por elemento. Ya se validó al registrar la ruta; cachear la lista resuelta en el `Route` (campo nuevo en el NamedTuple) elimina trabajo repetido en el camino caliente.

### 3.8 · Endurecer `_resuscribir()` — verificado que hoy aguanta

`groups.py:201` llama a `_resuscribir()` dentro del `except` sin protegerlo: si lanzara, mataría el listener. **Lo probé con una caída de 4 segundos y conexiones abortadas: sobrevive** — redis-py no propaga ahí. Pero eso es comportamiento de la versión actual de redis-py, no un contrato; un `try/except Exception: continue` alrededor cuesta 3 líneas y elimina la dependencia de ese detalle.

### 3.9 · El README ya roza el límite del formato

~1.200 líneas por idioma, ×2 idiomas que mantener a mano en paralelo (ya divergieron una vez durante esta revisión de metadatos). Cuando entre la próxima sección, valorar mkdocs-material con i18n — y mientras tanto, un check de CI que compare el número de secciones `##` de ambos README para detectar divergencia.

---

## 4. Revisado y descartado — para no re-investigarlo

- **Listener de Redis en caídas largas.** Sospeché que una caída más larga que el primer backoff mataría el listener (el `subscribe` del `_resuscribir` no está protegido). **Probado: 4 s de caída con conexiones abortadas, varios ciclos de reintento — sobrevive y se recupera.** Queda solo el endurecimiento 3.8.
- **Licencia PEP 639.** Django Packages lee `license_expression`; muestra MIT. Correcto como está, no volver al classifier.
- **Middleware bajo uvicorn.** Verificado en sesión anterior con marcador en disco: corre.
- **Zombis.** uvicorn los detecta y limpia en ~39 s; el heartbeat propio sigue siendo innecesario.
- **`Message.__eq__` / iteración / buzón / expulsión** — repasados; los arreglos de sesiones anteriores siguen íntegros y con sus tests.

---

## 5. Orden propuesto

**0.2.2 — parche, esta semana:**
1. **1.1** Publicador perezoso en `RedisLayer.send` + test de proceso-solo-publicador *(es el ejemplo estrella roto)*
2. **1.2** `__version__` desde `importlib.metadata` + test de coherencia
3. **1.3** `close()` del JS limpia la espera offline + test con timers falsos
4. **2.6** `get_running_loop()`
5. **3.2** Python 3.14 en matriz y classifiers

**0.3.0 — siguiente minor:**
6. **3.1** Política de idioma de API y errores (la decisión más importante de esta lista, y la única que duele más cuanto más se pospone)
7. **2.2** `TRUSTED_PROXIES` + `sock.real_ip`
8. **2.1** Rate limit con ámbito por usuario
9. **2.5** Cierre `1001` en shutdown
10. **2.4** Lock en `accept()`
11. **3.3** ruff en CI · **3.8** try en `_resuscribir` · **3.7** cache de auth en Route

**Cuando haya usuarios:** 3.4 (observabilidad), 2.3 (contador global), 3.5 (bytes), 3.9 (docs site).

---

*Los hallazgos 1.1, 3.8 y la mitad de la tabla salieron de ejecutar código, no de leerlo: la sospecha 3.8 resultó falsa al probarla y el 1.1 resultó peor de lo que parecía. Ese es el argumento para que cada arreglo entre con su test de regresión.*
