# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/).
Versioning: while below 1.0, **minor versions may change the API**. Patch
versions never do.

## [0.3.0] — 2026-08-27

### Changed — the public API is now entirely in English

The API had drifted into a mix: the core was English (`ws`, `broadcast`,
`Events`, `login_required`) while newer additions came out in Spanish
(`extraer_token`, `max_conexiones_por_usuario`, `registrar`), and **every error
message and log line was in Spanish** next to an English README. Those error
messages are among the better parts of this library — they tell you what to do
next — and half the intended audience could not read them.

Renamed:

| 0.2.x | 0.3.0 |
|---|---|
| `extraer_token` | `extract_token` |
| `middleware.max_conexiones_por_usuario` | `middleware.max_connections_per_user` |
| `middleware.registrar` | `middleware.log_connections` |
| `middleware.aplicar` | `middleware.apply` |
| `middleware.limpiar_cache` | `middleware.clear_cache` |
| `testing.TimeoutDelEsperado` | `testing.ReceiveTimeout` |
| `ratelimit.Cubo` | `ratelimit.TokenBucket` |
| `ratelimit.parsear` / `crear` | `ratelimit.parse_rate` / `make_bucket` |
| `authentication.resolver_lista` | `authentication.resolve_authenticators` |
| `events.CUALQUIERA` | `events.ANY` |
| `TokenBucket.consumir` / `.espera` | `.consume` / `.retry_after` |
| `InvalidJSON.crudo` | `InvalidJSON.raw` |
| `Events.tipos` | `Events.types` |
| `djangoSocket.esDefinitivo` (JS) | `djangoSocket.isFinal` |

`extraer_token`, `max_conexiones_por_usuario`, `registrar` and
`djangoSocket.esDefinitivo` — the ones 0.2.x exported and documented — keep
working as aliases so upgrading does not break anyone. They go away at 1.0.

Every error message, log line, docstring and comment in the library is now in
English, and so is the output of `manage.py ws` and `manage.py runserver`. The
README showed that command printing English headings it never actually printed.

## [0.2.2] — 2026-08-27

### Fixed

- **`broadcast_sync` from a Celery worker silently dropped the fan-out.** This
  is the pattern the examples lead with — a background task pushing progress —
  and it did not work. A process that only publishes (a Celery worker, a cron,
  a management command) never calls the layer's `startup()`: there is no
  websocket connection and no lifespan event to trigger it. So `_redis` was
  `None`, the `publish` raised `AttributeError`, and the `except` meant for
  Redis outages swallowed it — logging a message that blamed Redis, which was
  perfectly healthy. The publisher now connects lazily on first `send()`, and a
  failure to *create* the client is reported separately from a failure to
  publish. Regression test covers a publisher-only process.

- **`__version__` said `0.1.0` while PyPI said `0.2.1`.** The two were declared
  independently and drifted, so anyone reading it to file a bug reported the
  wrong version. The module is now the single source and `pyproject.toml` reads
  it via `dynamic`, which makes drift structurally impossible.

- **The JS client could revive a socket the application had closed.** If
  `close()` was called while the client was parked waiting for the browser's
  `online` event, nothing tore down that listener, and `conectar()` cleared the
  deliberate-close flag unconditionally. When the network came back, the socket
  reconnected — a user who logged out kept receiving messages. `close()` now
  dismantles the offline wait, and only `reconnect()` clears the flag.

- `evict()` used the deprecated `asyncio.get_event_loop()`.

### Added

- Python 3.14 in the CI matrix and the classifiers.
- `ruff check` in CI, with an explicit ruleset (`E`, `F`, `I`, 88 columns) in
  `pyproject.toml` rather than the installed version's defaults. CONTRIBUTING
  promised a style nothing verified; now it does. It found no real defects —
  only unused imports and formatting — which is itself worth knowing.

## [0.2.1] — 2026-08-27

### Fixed

- **Trove classifiers.** Package directories read these, not the modern
  metadata fields, and without the `Programming Language :: Python :: 3.x`
  lines the Django Packages comparison grid literally showed **"Python 3? No"**
  next to this package. Added those, plus `Development Status`,
  `Framework :: Django :: 6.0/6.1` and `Typing :: Typed`.

  The license classifier is deliberately *not* there: PEP 639 replaces it with
  the `license = "MIT"` field, and setuptools refuses to accept both. Some
  directories still read the old one and will show "UNKNOWN" until they catch
  up. PyPI itself shows it correctly.

### Added

- `ejemplos/` — four runnable patterns in the demo project: background task
  progress, per-user notifications, presence, and a live dashboard. Each one
  documents the mistake that's easy to make with it. Not shipped in the wheel;
  they're reference code in the repo.

## [0.2.0] — 2026-08-27

### Added

- **Pluggable authentication.** `sock.user` no longer comes only from the
  session cookie. `DJANGO_SOCKET["AUTH"]` takes a list tried in order, and
  `@ws(..., auth=...)` overrides it per route. An authenticator is
  `async(sock) -> user | None`, so your own is just a function.
- **Token auth** for SPAs and mobile clients, which have no session cookie.
  Read from `Sec-WebSocket-Protocol` (the only header-free way a browser can
  send one), `Authorization: Bearer`, or `?token=`. Validation is delegated to
  `TOKEN_RESOLVER`; `rest_framework.authtoken` is used if it's installed and no
  resolver is set.
- **Middleware.** `DJANGO_SOCKET["MIDDLEWARE"]` wraps every connection with
  `async(sock, next)` — for tracing, metrics, error reporting or gatekeeping.
  Ships with `max_conexiones_por_usuario()` and `registrar()`.
- **Rate limiting.** `@ws(..., rate_limit="60/m")` or a global
  `DJANGO_SOCKET["RATE_LIMIT"]`. Token bucket, so normal bursts pass and only a
  sustained excess closes the socket with 4429. `burst=` widens the spike
  allowance without raising the sustained rate.
- `py.typed`: the package ships its type information, so mypy and editors see
  it in your codebase.
- Docs: reverse proxy recipes (nginx, Caddy) and how to tell whether the proxy
  is eating the protocol upgrade — the single most common deployment failure.
- Spanish README alongside the English one.

### Changed

- With auth active, `sock.user` is now always a user object — `AnonymousUser`
  when nobody recognised the client, instead of sometimes `None`. It's `None`
  only with `auth=False`. This means `sock.user.is_authenticated` no longer
  needs a `None` check first.
- `django_socket.auth` still imports, but the implementation moved to
  `django_socket.authentication`.

## [0.1.0] — 2026-08-26

First release.

- `@ws` routing with `django.urls.path` syntax and converters
- Declarative groups: `group="room:{room}"` joins on connect, leaves on
  disconnect
- `sock.user` from Django's session, with nothing to wrap in `asgi.py`
- JSON via `DjangoJSONEncoder`; invalid client JSON closes with 4400, not 1011
- `Events` for routing JSON messages by type
- Redis layer for multiple workers, with automatic resubscribe after an outage
- Bounded per-socket outbox: one slow client can't hang everyone else's fan-out
- Origin validation on by default
- `WebSocketClient` for testing handlers without a server
- JavaScript client whose reconnect logic does *not* retry deliberate closes
