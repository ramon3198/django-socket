# Security policy

## Reporting

Please report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/ramon3198/django-socket/security/advisories/new),
not as a public issue.

I'll acknowledge within a few days. This is a small project maintained in spare
time — I'd rather set that expectation than promise an SLA I can't keep.

## Supported versions

While below 1.0, only the latest release gets fixes.

## What this library does for you

- **Origin validation is on by default**, checked during the handshake against
  `ALLOWED_HOSTS` + `CSRF_TRUSTED_ORIGINS`. WebSockets are not subject to the
  same-origin policy, so without this any site could open an authenticated
  socket against yours with your users' cookies.
- **A missing `Origin` header is accepted** by default, because browsers always
  send it and only native clients omit it. Set `REQUIRE_ORIGIN: True` if your
  endpoint is browser-only.
- **Bounded per-socket outbox**, so one client that stops reading can't stall
  fan-out for a whole group.
- **`rate_limit`** caps incoming messages per socket.

## What it does not do

- **No protection against many connections from many sources.** The
  `max_conexiones_por_usuario` middleware counts per process, not globally.
  A distributed flood needs something in front of the application.
- **`?token=` ends up in access logs**, yours and every proxy's in between.
  Prefer `Sec-WebSocket-Protocol` from a browser.
- **uvicorn buffers around 24 MB per connection** below this library's outbox.
  With many stuck clients that's memory this library can't reclaim from
  application level; limit it at the server.
