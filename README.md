# django_socket

[![PyPI](https://img.shields.io/pypi/v/django-socket)](https://pypi.org/project/django-socket/)
[![tests](https://github.com/ramon3198/django-socket/actions/workflows/tests.yml/badge.svg)](https://github.com/ramon3198/django-socket/actions/workflows/tests.yml)
[![python](https://img.shields.io/badge/python-3.10%20%E2%80%93%203.13-blue)](https://github.com/ramon3198/django-socket)
[![django](https://img.shields.io/badge/django-4.2%20%E2%80%93%206.1-0C4B33)](https://github.com/ramon3198/django-socket)
[![license](https://img.shields.io/badge/license-MIT-green)](https://github.com/ramon3198/django-socket/blob/main/LICENSE)

**English** · [Español](https://github.com/ramon3198/django-socket/blob/main/README.es.md)

**WebSockets for Django.** One decorator, one `async` function, and it runs.

```python
# myapp/sockets.py
from django_socket import ws

@ws("chat/<str:room>/", group="room:{room}")
async def chat(sock, room):
    async for msg in sock:
        await sock.broadcast({"from": str(sock.user), "text": msg.text})
```

That's the whole file. Connection state lives in local variables, disconnect is
the code after the loop, and Django's user is right where you'd expect it.

---

## Contents

**Getting started** ·
[Install](#install) ·
[Your first socket in 5 minutes](#your-first-socket-in-5-minutes) ·
[Why you don't touch `asgi.py`](#why-you-dont-touch-asgipy)

**Guide** ·
[The `@ws` decorator](#the-ws-decorator) ·
[The `sock` object](#the-sock-object) ·
[Groups and broadcast](#groups-and-broadcast) ·
[JSON](#json) ·
[`Events`](#routing-by-message-type-with-events) ·
[Authentication](#users-and-authentication) ·
[JavaScript client](#javascript-client) ·
[Testing](#testing-your-handlers)

**Operations** ·
[Production](#production) ·
[Security](#security-origin-validation) ·
[Slow clients](#slow-clients) ·
[Zombie connections](#zombie-connections) ·
[Settings](#all-settings) ·
[Close codes](#close-codes)

**Reference** ·
[How it works inside](#how-it-works-inside) ·
[Performance](#performance) ·
[Known limits](#known-limits) ·
[Development](#development)

---

# Getting started

## Install

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

**That's it. There is no third step.**

You don't touch `asgi.py`, you don't declare `ASGI_APPLICATION`, you don't set
up Redis, you don't switch servers. The `asgi.py` that `startproject` generated
serves WebSockets as-is, and `manage.py runserver` serves them on the same port
as HTTP.

Requires Python 3.10+ and Django 4.2+. For development you also need
`pip install "uvicorn[standard]"`.

---

## Your first socket in 5 minutes

### 1. Write the handler

Create `<your_app>/sockets.py`. It is auto-discovered, the same way `admin.py`
is:

```python
from django_socket import ws

@ws("echo/")
async def echo(sock):
    async for msg in sock:
        await sock.send(f"you said: {msg.text}")
```

### 2. Check it registered

```bash
python manage.py ws
```

```
Rutas WebSocket
  ws:///echo/                   myapp.sockets.echo

Integracion
  Capa de difusion      memory
  asgi.py               no hace falta tocarlo (ASGIHandler ampliado)
  Origenes permitidos   ALLOWED_HOSTS=[] (DEBUG: localhost)
  Origin ausente        aceptado (clientes nativos)
```

Run this first whenever something doesn't work: it tells you which routes exist,
with which group, and how the integration is wired.

### 3. Start it and try it

```bash
python manage.py runserver
```

From the browser console, on any page of your site:

```js
const s = new WebSocket("ws://localhost:8000/echo/");
s.onmessage = (e) => console.log(e.data);
s.onopen = () => s.send("hi");
// -> you said: hi
```

### 4. Now a chat room

```python
@ws("chat/<str:room>/", group="room:{room}")
async def chat(sock, room):
    who = sock.user.username if sock.user.is_authenticated else "anonymous"

    await sock.broadcast({"kind": "join", "who": who}, exclude_self=True)

    async for msg in sock:
        await sock.broadcast({"kind": "message", "who": who, "text": msg.text})

    # Reached when the client goes away.
    await sock.broadcast({"kind": "leave", "who": who})
```

Everything you need to know is in those lines:

- **`<str:room>`** is `django.urls.path` syntax. It reaches the handler already
  converted (`<int:pk>` gives you a real `int`).
- **`group="room:{room}"`** is filled from that parameter. The socket joins on
  connect and leaves on disconnect, without you calling anything.
- **`sock.broadcast(...)`** goes to that group by default.
- **`sock.user`** is the Django user, resolved from the session cookie.
- **The code after the `for`** runs when the client leaves. That's your
  `disconnect`.

---

## Why you don't touch `asgi.py`

Django has been ASGI since 3.0. All its handler does with a WebSocket is this:

```python
if scope["type"] != "http":
    raise ValueError("Django can only handle ASGI/HTTP connections, not %s.")
    # Django itself leaves a "FIXME: Allow to override this." right there.
```

It isn't that Django *can't* speak WebSocket — it refuses to look at that scope.
And since `django.setup()` runs every app's `ready()` **before** instantiating
the handler, our `AppConfig.ready()` gets there in time to widen that door. That
is why installing is just `INSTALLED_APPS`.

If you'd rather nothing of yours be touched, turn it off and declare it
yourself:

```python
DJANGO_SOCKET = {"PATCH_ASGI": False}
```

```python
# asgi.py
from django_socket import ASGIApplication
application = ASGIApplication()
```

---

# Guide

## The `@ws` decorator

```python
@ws(route, *, group=None, auth=True, name=None)
```

| | |
|---|---|
| `route` | `django.urls.path` syntax, with its converters |
| `group` | Group template; joins on connect, leaves on disconnect |
| `auth` | `False` skips session resolution (one query less) |
| `name` | Label for the `manage.py ws` listing |

```python
@ws("game/<int:pk>/player/<slug:nick>/")
async def game(sock, pk, nick):      # pk really is an int
    ...
```

If `group=` mentions a parameter the route doesn't have, it **fails at import
time** naming the culprit, not on the first connection.

The handler must be `async def`. Pass a regular function and the error explains
why, and what to use instead.

---

## The `sock` object

### Receiving

```python
async for msg in sock: ...          # ends when the client closes
msg.text   msg.bytes   msg.json()
if msg == "ping": ...               # Message compares against str directly

await sock.receive_text()           # also receive_json / receive_bytes
async for txt in sock.iter_text(): ...      # and iter_json()
```

### Sending

```python
await sock.send("hi")               # str    -> text frame
await sock.send(b"\x00")            # bytes  -> binary frame
await sock.send({"a": 1})           # rest   -> JSON
await sock.send_json(obj)           # also send_text / send_bytes
```

### Connection context

```python
sock.user            # User or AnonymousUser, from the session cookie
sock.session         # Django's SessionStore
sock.path_params     # {"room": "general"}
sock.query_params    # {"token": "abc"}   (query_lists for repeated keys)
sock.headers   sock.cookies   sock.client   sock.subprotocols   sock.scope
```

### Control

```python
await sock.accept(subprotocol="graphql-ws")   # only if you need to negotiate
await sock.close(4001, "room full")           # the code reaches onclose
await sock.deny()                             # kill the handshake with a 403
```

You don't need to call `accept()`: the handshake completes on its own the first
time you send, receive or iterate.

---

## Groups and broadcast

```python
await sock.join("global")            # the first one becomes the default target
await sock.leave("global")

await sock.broadcast(data)                    # to the default group
await sock.broadcast(data, to="other:group")
await sock.broadcast(data, exclude_self=True)

sock.group     # default target for broadcast()
sock.groups    # which groups you're a member of right now
```

`sock.group` and `sock.groups` are deliberately different. `groups` is which
groups you're a **member** of (it empties on disconnect); `group` is where
`broadcast()` points by **default**, and it survives disconnection so this
pattern works:

```python
async for msg in sock:
    await sock.broadcast(msg.text)
await sock.broadcast("someone left")     # <- you're no longer a member here
```

### From outside a handler

```python
from django_socket import broadcast, broadcast_sync

await broadcast({"notice": "maintenance"}, to="room:1")   # async view or task
broadcast_sync({"notice": "maintenance"}, to="room:1")    # sync view, signal, Celery
```

> With the default layer (`memory`), `broadcast_sync` only reaches the current
> process. With several workers you need Redis — see [Production](#production).

---

## JSON

It's the common case, so it's frictionless both ways:

```python
@ws("echo/")
async def echo(sock):
    async for data in sock.iter_json():         # already parsed
        await sock.send_json({"got": data})     # dict/list -> JSON
```

### Django types serialize themselves

`DjangoJSONEncoder` is used, so this just works:

```python
await sock.send_json({
    "when": timezone.now(),        # "2026-08-26T19:43:30.251Z"
    "price": Decimal("9.99"),      # "9.99"  (string: no precision loss)
    "id": uuid4(),                 # "0d8f...-..."
    "notice": _("Hello"),          # lazy translation strings too
})
```

<details>
<summary><b>Why the date matters more than it looks</b></summary>

With `str()` you'd get `"2026-08-26 19:43:30.251057+00:00"`, which has two
problems:

1. **ISO-8601 is the only format the ECMAScript spec requires `Date` to
   parse.** Anything else is each engine's fallback: V8 is lenient and accepts
   it, others historically aren't.
2. **`str()` emits microseconds** (6 digits), which `Date` cannot represent. The
   encoder truncates to milliseconds, which is what JS understands.

</details>

### An object it can't serialize fails loudly

```
TypeError: No se puede enviar un Usuario por el socket. Los tipos de Django
habituales (datetime, date, time, timedelta, Decimal, UUID, cadenas lazy) van
solos; el resto conviértelo tú: un modelo a dict, un QuerySet a lista.
Si prefieres el comportamiento antiguo: sock.send_json(dato, default=str).
```

That's on purpose. Staying quiet and shipping `"User object (3)"` to the browser
is worse: you find out in production.

### Invalid JSON is the client's fault, not yours

If a client sends garbage, the connection closes with **4400 "Invalid JSON"**
and leaves a `WARNING` in the log. Not a `1011 Internal error`, and not a
traceback that sends you hunting for a bug of yours that doesn't exist.

```python
from django_socket import InvalidJSON     # it's a ValueError

async for msg in sock:
    try:
        data = msg.json()
    except InvalidJSON:
        await sock.send_json({"error": "that wasn't JSON"})
```

---

## Routing by message type with `Events`

Most apps send `{"type": "something", ...}` and end up with a long if/elif.
`Events` turns that into named functions:

```python
from django_socket import Events, ws

board = Events()

@board.on("draw")
async def draw(sock, data):
    await sock.broadcast({"type": "draw", **data}, exclude_self=True)

@board.on("clear")
async def clear(sock):              # if you don't use the data, don't ask for it
    await sock.broadcast({"type": "clear"})

@board.on("join", "leave")          # several types at once
async def move(sock, data): ...

@board.on("*")                      # whatever didn't match anything else
async def rest(sock, data): ...

@ws("board/<str:room>/", group="board:{room}")
async def handler(sock, room):
    await board.run(sock)
```

- The `type` field does **not** arrive inside `data`.
- `"*"` is a **fallback**, not a spy: it only runs when nobody else took the
  message.
- An unhandled type is ignored and leaves a `WARNING` listing the registered
  ones — it's almost always a typo.
- `Events(strict=True)` closes with 4400 instead.
- `Events(key="action")` changes the field name.

It's entirely optional: `async for msg in sock` is still there.

---

## Users and authentication

`sock.user` is always available, resolved from the handshake's session cookie.
Nothing to wrap in `asgi.py`.

```python
from django_socket import login_required, ws

@ws("dashboard/")
@login_required                 # closes with 4401 when there's no session
async def dashboard(sock):
    await sock.send_json({"hello": sock.user.username})
```

With `@ws(..., auth=False)` you skip the session query on public endpoints.

### The ORM works normally

```python
@ws("users/")
async def users(sock):
    total = await User.objects.acount()                        # async API
    names = [u.username async for u in User.objects.all()]
    x = await sync_to_async(lambda: User.objects.first())()     # sync ORM
```

The handler runs inside a `ThreadSensitiveContext`, so
`sync_to_async(thread_sensitive=True)` — the default — shares a thread just like
it does in a view.

---

## JavaScript client

Reconnecting properly is one of those things everyone rewrites and almost
nobody gets right. It ships included:

```html
{% load django_socket %}
{% ws_client %}

<script>
  const sock = djangoSocket("/chat/general/");

  sock.on("message", (d) => render(d.text));
  sock.on("join",    (d) => notify(`${d.who} joined`));
  sock.on("*",       (d) => console.log("no handler:", d));

  sock.send({type: "message", text: "hi"});   // object -> JSON
</script>
```

`ws://` or `wss://` depending on the page protocol, JSON both ways, and routing
by `type` just like `Events` on the Python side.

### What sets it apart

**It does not reconnect when the server closed on purpose.** A 4401 (login
required) or a 4404 (no such route) don't get fixed by retrying: reconnecting
there is an infinite loop hammering your server. 1000, 1008 and the whole
4000–4999 range are treated as final; everything else (1006, 1011, network
drops) is retried.

**Exponential backoff with jitter.** 0.5 s, 1 s, 2 s… up to 15 s, each wait
multiplied by a random factor. Without jitter, a thousand clients that drop
together all come back in the same millisecond and take the server down again.

**It queues what you write while offline** and flushes on reconnect. `send()`
returns `false` when it had to queue:

```js
if (!sock.send(text)) show("offline: will send on reconnect");
```

**With no network it doesn't burn attempts**: it waits for the browser's
`online` event. With the tab hidden it **keeps retrying**, on purpose — a chat
in a background tab that silently stops reconnecting is broken. If your case
tolerates falling behind, `{pauseWhenHidden: true}`.

### Options

```js
djangoSocket("/route/", {
  key: "type",             // the routing field
  reconnect: true,
  minDelay: 500, maxDelay: 15000, maxRetries: Infinity,
  queue: true, maxQueue: 100,
  pauseWhenHidden: false,
  protocols: ["graphql-ws"],
  shouldReconnect: (e) => e.code !== 4001,   // your own rule
  onOpen(isReconnect) {}, onClose(e, willRetry) {},
  onRetry(attempt, delayMs) {}, onError(e) {}, onMessage(data) {},
});
```

Plus `sock.connected`, `sock.pending`, `sock.close()`, `sock.reconnect()`,
`sock.on(type, fn)`, `sock.off(type, fn)`.

---

## Testing your handlers

No server, no ports, milliseconds:

```python
from django_socket.testing import WebSocketClient

async def test_chat_fans_out():
    async with WebSocketClient("/chat/general/") as a, \
               WebSocketClient("/chat/general/") as b:
        await b.send_json({"type": "message", "text": "hi"})
        assert (await a.receive_json())["text"] == "hi"
```

It goes through the same path a real connection does — route, converters, origin
validation, session, groups — but speaking ASGI straight to the dispatcher.

```python
# Authenticated user without hand-rolling cookies
async with WebSocketClient("/dashboard/", user=my_user) as c:
    assert (await c.receive_json())["who"] == "ramon"

# Rejections
async with WebSocketClient("/dashboard/") as c:
    assert await c.wait_closed() == 4401
    assert not c.connected

# Room isolation
async with WebSocketClient("/room/one/") as a, WebSocketClient("/room/two/") as b:
    await b.send("private")
    assert await a.receive_nothing()
```

| | |
|---|---|
| `send` / `send_json` / `send_text` / `send_bytes` | `str`→text, `bytes`→binary, rest→JSON |
| `receive` / `receive_json` / `receive_text` / `receive_bytes` | with timeout, fail fast |
| `receive_all()` | everything pending right now |
| `receive_nothing()` | `True` if nothing arrives — to assert isolation |
| `wait_closed()` | the close code |
| `connected` `accepted` `close_code` `close_reason` `subprotocol` | state |

`WebSocketClient(path, user=, headers=, cookies=, query=, subprotocols=, origin=)`

> `connected` and `accepted` are not the same: `accepted` says whether a
> `websocket.accept` arrived, and a coded rejection looks like accept + close on
> the wire (so the code reaches the browser). For "did it let me in?" use
> `connected`.

Every `receive` has a 1 s timeout: a test waiting for something that never
arrives fails in one second, with the path in the message, instead of hanging
the suite.

---

# Operations

## Production

Your project's `asgi.py` already works, unchanged:

```bash
uvicorn myproject.asgi:application --host 0.0.0.0 --port 8000 --workers 4
```

```bash
gunicorn myproject.asgi:application -k uvicorn.workers.UvicornWorker -w 4
```

### With more than one worker you need Redis

The memory layer only knows the sockets in its own process, so a broadcast
doesn't cross from one worker to another.

```python
DJANGO_SOCKET = {"LAYER": "redis", "REDIS_URL": "redis://localhost:6379/0"}
```

```bash
pip install "django-socket[redis]"
```

Each process publishes its broadcasts to a pub/sub channel and delivers to its
local members whatever it receives from the rest. Your handlers don't change:
`sock.broadcast(...)` is the same call.

Verified with two real workers against Redis 7.4: the message crosses both ways,
rooms stay isolated, and the publishing process doesn't deliver its own echo
twice.

### When Redis goes down

A Redis outage **does not take your users' connections down**:

- in-process delivery keeps working normally
- `broadcast()` doesn't raise; it logs an `ERROR` saying the group only got
  local delivery
- the process retries subscribing with exponential backoff (0.5 s → 10 s)
- when Redis returns, it resubscribes on its own and fan-out resumes without a
  restart

Anything published while Redis is down **is lost**: this is pub/sub, not a
queue. If you need delivery guarantees you have to persist the message yourself,
with the database or a real queue.

---

## Security: origin validation

**WebSockets are not subject to the same-origin policy.** Without validating the
`Origin` header, any website can open a socket against yours and the browser
will attach the victim's session cookies (*cross-site WebSocket hijacking*).
That's why the check isn't optional.

**It's on by default.** Validation runs against `ALLOWED_HOSTS` +
`CSRF_TRUSTED_ORIGINS`, and a foreign origin is rejected with a 403 **during the
handshake**, before anything is accepted.

A **missing** `Origin` is accepted, because browsers always send it: only native
clients omit it (a mobile app, a script). If your endpoint is browser-only, make
it strict:

```python
DJANGO_SOCKET = {
    "REQUIRE_ORIGIN": True,
    # or an explicit list, which ignores ALLOWED_HOSTS:
    "ALLOWED_ORIGINS": ["https://myapp.com", "https://admin.myapp.com"],
}
```

`manage.py check` warns if you leave `"*"` with `DEBUG=False`.

---

## Slow clients

A client that stops reading — bad network, a phone going to sleep, or someone
acting in bad faith — cannot drag the rest down with it.

<details>
<summary><b>The problem, measured</b></summary>

uvicorn does apply backpressure: it buffers about **24 MB** toward a client
that isn't reading, and past that `send()` stops returning. With delivery that
awaited every member, that meant `broadcast()` **never returned**: the
broadcasting handler hung, stopped reading its own socket, and never ran its
cleanup. One client on a bad network killed the whole room.

</details>

**How it's solved.** Each socket has a bounded outbox and a task that writes it.
`broadcast()` enqueues and returns; it waits for nobody. If someone's outbox
fills up, that client is too far behind and gets evicted with a **1013 (Try
Again Later)** instead of being allowed to slow the group down.

```python
DJANGO_SOCKET = {
    "SEND_QUEUE_MAX": 256,       # queued messages per socket
    "SEND_QUEUE_FULL": "close",  # "close" | "drop_oldest"
}
```

`drop_oldest` is for streams that tolerate gaps — cursor positions, telemetry, a
live counter: better to lose an old value than to evict the client. For a chat
you want `close`.

**How to size it.** The outbox is measured in messages, but what matters is
*time*: `outbox ÷ messages_per_second = seconds of tolerance`. Measured, one
process publishes ~1,500 broadcasts/s against Redis, so 256 is ~165 ms in the
worst case and tens of seconds at normal chat rates. Only stuck clients pay the
memory — one that keeps up has an empty outbox — so the cost is
`stuck × 256 × message_size`.

**`sock.send()` still waits, on purpose.** There, blocking is healthy: in a
one-to-one stream, if the client can't keep up with you, your handler slowing
down is the correct outcome. The outbox is only for fan-out, which is where
waiting does damage.

**In a test, `await sock.drain()`** waits for what's queued to go out, so you
can assert without sleeping blindly. In production you don't need it.

---

## Zombie connections

A laptop whose lid gets closed leaves a TCP connection "open" with nobody on the
other end: no `close`, no error. Without detection that socket stays in its
group forever and you broadcast into the void.

**You don't have to do anything, and this library doesn't need to add a
heartbeat.** uvicorn already sends protocol pings and closes what doesn't
answer. Measured with a client that completes the handshake and then goes
completely silent:

```
t=  10s  in the group: 1
t=  20s  in the group: 1
t=  30s  in the group: 1
detected and cleaned up after 39s
```

39 seconds: `ws_ping_interval` (20 s) + `ws_ping_timeout` (20 s). The handler
exits through its `finally`, the socket leaves its groups, everything cleans up
by itself.

If you need it detected sooner:

```bash
uvicorn project.asgi:application --ws-ping-interval 5 --ws-ping-timeout 5
```

> This is uvicorn behaviour, not protocol behaviour. With a different ASGI
> server, check it.

---

## All settings

Everything is optional. These are the defaults:

```python
DJANGO_SOCKET = {
    "LAYER": "memory",            # "memory" | "redis" | callable -> BaseLayer
    "REDIS_URL": "redis://localhost:6379/0",
    "PREFIX": "djws",             # Redis channel prefix
    "ALLOWED_ORIGINS": None,      # None = ALLOWED_HOSTS + CSRF_TRUSTED_ORIGINS
    "REQUIRE_ORIGIN": False,      # True = also reject requests without Origin
    "PATCH_ASGI": True,           # False = declare ASGIApplication() yourself
    "SEND_QUEUE_MAX": 256,        # messages queued per socket, for fan-out
    "SEND_QUEUE_FULL": "close",   # "close" (evict the slow one) | "drop_oldest"
}
```

A misspelled key is caught by `manage.py check` (`django_socket.E001`).

---

## Close codes

| Code | Meaning |
|---|---|
| `4400` | The client sent something that isn't valid JSON |
| `4401` | `@login_required` and there's no session |
| `4404` | No route matches that path |
| `1013` | The client isn't consuming: outbox full, evicted from the group |
| `1011` | Uncaught exception in the handler (it's in the log) |
| HTTP 403 | Origin not allowed — the handshake never completes |

`sock.close(code, reason)` accepts the handshake before closing if it hadn't
been accepted yet, precisely so your code reaches the client's `onclose`.
Closing without accepting produces an HTTP 403 and the browser only sees a
reasonless `1006`, which helps nobody debug.

---

# Reference

## How it works inside

Four decisions explain almost all of the library's behaviour.

### Connection state lives on a coroutine's stack

A WebSocket is a conversation with a beginning and an end. Modelling it as an
`async` function that runs start to finish means state goes in local variables
and the flow reads top to bottom:

```python
@ws("game/<int:pk>/", group="game:{pk}")
async def game(sock, pk):
    hand = deal()                      # state: a local variable
    turns = 0

    async for move in sock.iter_json():
        turns += 1
        hand = apply(hand, move)
        await sock.broadcast({"turns": turns})

    await record_abandon(pk, turns)    # disconnect is just the end
```

There are no instance attributes to keep in sync across callbacks, and no object
that outlives the connection. When the coroutine ends, there's nothing left to
clean up beyond what its own `finally` already does.

### Widening Django's door instead of building another one

Django is already ASGI. Its handler simply refuses to look at the `websocket`
scope, and it does so with a `FIXME` in the code. Since `django.setup()` runs
every app's `ready()` before instantiating that handler, the `AppConfig` gets
there in time to widen it.

That's where the most visible property comes from: **installing is adding one
line to `INSTALLED_APPS`**. No `asgi.py` to rewrite, no nested wrappers, no
second routing tree running parallel to Django's. And HTTP traffic never passes
through here at all.

### Fan-out waits for nobody

Each socket has a bounded outbox and a task that writes it. `broadcast()`
enqueues and returns.

The alternative — awaiting every member's delivery — looks simpler until you
measure it: a client that stops reading makes `send()` stop returning, and with
that the broadcasting handler hangs forever. It stops reading its own socket and
never runs its cleanup. One phone with bad reception takes the whole room down.
With an outbox, that client falls behind alone and is eventually evicted without
dragging anyone with it.

`sock.send()` does wait, and that's deliberate: in a one-to-one stream,
backpressure is healthy. Only fan-out needs to be decoupled.

### The fan-out layer is replaceable

`sock.broadcast(...)` is the same call whether you're on one process or twelve.
The only thing that changes is which layer sits underneath: in-memory for one
process, Redis pub/sub for several, or your own implementing `BaseLayer`.

Handlers never find out. There's no code to rewrite in order to scale, and no
two paths to maintain depending on the deployment.

---

## Performance

Measured on a laptop, Redis 7.4 in Docker, everything against `localhost`.
Reproducible:

```bash
python bench_redis.py      # layer cost
python bench_carga.py      # concurrent connections
```

### Layer cost

| | |
|---|---|
| broadcast, memory, 1 member | 0.003 ms |
| broadcast, memory, 1000 members | 0.104 ms |
| broadcast, Redis | ~0.65 ms → **~1,500/s per process** |
| cross-process latency | median 0.59 ms · p95 0.80 ms · p99 0.90 ms |

With Redis the cost is nearly flat in the number of members: the round trip to
Redis dominates, not local delivery. Those ~1,500 broadcasts/s are per process,
so they scale with workers.

### Concurrent connections

One process, memory layer, a freshly started server per measurement:

| connections | memory | fan-out p50 | p95 | delivered |
|---|---|---|---|---|
| 1,000 | 125 MB · 122 KB/conn | 28 ms | 40 ms | 100 % |
| 3,000 | 371 MB · 121 KB/conn | 157 ms | 200 ms | 100 % |
| 6,000 | 741 MB · 121 KB/conn | 130 ms | 216 ms | 100 % |

At 6,000 connections: all of them open (1,600/s), and 120,000 broadcast messages
arrive **without losing a single one**, at ~24,500 msg/s. With the Redis layer at
1,000 connections, fan-out goes from 28 to 58 ms median — the Redis round trip —
with the same memory and the same complete delivery.

**Memory is the binding constraint**: ~121 KB per connection, constant from
1,000 to 6,000. That's roughly **8,000 connections per GB**, and that number
comes from uvicorn and socket buffers, not from this library.

### What these figures do NOT say

- The benchmark client is **a single Python process** reading N websockets, so
  the latencies include its own scheduler. With real, distributed clients they'd
  be better.
- Everything is `localhost`: no network latency, no loss, no TLS.
- Not tested with large messages, a remote Redis, or over hours.

Re-run the benchmarks in your own environment before sizing anything.

---

## Known limits

- **Everything measured is `localhost`**: there are numbers up to 6,000
  connections and with Redis, but not against a real network, TLS, large
  messages or long sessions.
- **Django 4.2 → 6.1 and Python 3.10 → 3.13 pass in CI**, including the
  alternative path for the `aget_user` that doesn't exist before Django 5.0: on
  4.2 it really executes, not forced with a monkeypatch. What CI doesn't cover
  is Windows and macOS — it only runs on Linux.
- **The ~24 MB uvicorn buffers per connection** sit below this layer: the outbox
  bounds what piles up on top, not what's underneath. With 100 stuck clients at
  once that's 2.4 GB that can't be avoided from application level; that gets
  limited at the server.
- **No background task runner.** For deferred work use Celery or whatever runner
  you already have, and notify over the socket with `broadcast_sync`.
- **Synchronous handlers are not supported**, on purpose: `@ws` requires
  `async def` and says so when it fails. A plain `def` would occupy a thread per
  open connection.
- **Route resolution is linear and first match wins**, same as Django. With
  hundreds of routes it would want indexing.
- `broadcast_sync` with `LAYER="memory"` only reaches the current process — it's
  what you'd expect, but it's easy to trip over in development and not notice
  until production.

---

## Development

```bash
git clone https://github.com/ramon3198/django-socket.git && cd django-socket
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

### Tests

```bash
pytest                              # 207 tests, ~7 s, nothing to start
node --test tests/js/*.test.js      # 35 tests for the JS client, no npm install
```

The Python suite needs no server: it uses the same `WebSocketClient` documented
above, plus a fake transport for the lower-level bits. Coverage: **94 %**.

The JS client uses the test runner and fake timers that ship with Node (≥20), so
there's nothing to install. It covers backoff with jitter, which codes are not
retried, the offline queue, the no-network pause, and `type` routing.

### Against real services

```bash
# A real Redis: the tests detect it and tell you which one they used
docker run -d --rm -p 6379:6379 redis:7-alpine
pytest tests/test_redis_layer.py -s

# Integration against a running server
python manage.py runserver 8000
python test_sockets.py 8000                 # 24 tests

# Fan-out across separate processes (with memory, this one MUST hang)
DJANGO_SOCKET_LAYER=redis python manage.py runserver 8091
DJANGO_SOCKET_LAYER=redis python manage.py runserver 8092
python test_multiproceso.py 8091 8092
```

### Demo

```bash
python manage.py migrate
python manage.py runserver
```

- `http://127.0.0.1:8000/sala/general/` — chat, open it in two tabs. To see the
  reconnect, stop the server and start it again.
- `chat/sockets.py` — every example in one file.

---

## License

MIT.
