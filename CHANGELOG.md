# Changelog

Format based on [Keep a Changelog](https://keepachangelog.com/).
Versioning: while below 1.0, **minor versions may change the API**. Patch
versions never do.

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
