# Contributing

## Getting set up

```bash
git clone https://github.com/ramon3198/django-socket.git && cd django-socket
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

```bash
pytest                              # ~10 s, starts no server
node --test tests/js/*.test.js      # the JS client, no npm install needed
```

Both suites run offline. The Python one talks ASGI straight to the dispatcher
through `WebSocketClient`; the JS one uses the test runner and fake timers that
ship with Node (≥20).

## What I'd like a patch to come with

**A test that fails before your change.** Not for ceremony — several real bugs
in this library were found by a test that was written to prove something else,
and every one of them is documented in the commit that fixed it.

**A reason in the code, not just the what.** The comments here explain *why* a
decision was made, usually with the measurement behind it. If you change one of
those decisions, change the comment too.

**Honest limits.** If something works only under conditions you tested, say so
in the docstring or the README. This project would rather say "measured on
localhost, not on a real network" than imply more than it earned.

## Running against real services

```bash
# Redis: the tests detect it and print which one they used
docker run -d --rm -p 6379:6379 redis:7-alpine
pytest tests/test_redis_layer.py -s

# Against a running server
python manage.py runserver 8000
python test_sockets.py 8000

# Fan-out across separate processes (with the memory layer, this MUST hang)
DJANGO_SOCKET_LAYER=redis python manage.py runserver 8091
DJANGO_SOCKET_LAYER=redis python manage.py runserver 8092
python test_multiproceso.py 8091 8092
```

## Benchmarks

```bash
python bench_redis.py      # layer cost and cross-process latency
python bench_carga.py      # concurrent connections, memory, fan-out
```

If you change anything in the delivery path, re-run `bench_carga.py` and put the
numbers in the PR. The README's performance figures should stay true.

## Style

- Line length 88.
- Comments and docstrings explain the *why*. The code already says the what.
- Error messages tell the reader what to do next, not just what went wrong.
  `"@ws espera 'async def', y X es una función normal"` followed by what to use
  instead — that's the bar.

## CI

Every push runs Django 4.2 → 6.1 across Python 3.10 → 3.13, the JS suite, and
integration against a real server with Redis. It has to be green before merge.

Note it only runs on Linux. If you develop on Windows or macOS, say so in the
PR — that's coverage the project doesn't have and it's useful to know.

## Releasing

Maintainer only:

```bash
# bump the version in pyproject.toml, write the CHANGELOG entry
git tag v0.X.0 && git push origin v0.X.0
gh release create v0.X.0 --title "v0.X.0" --notes-file notes.md
```

Publishing to PyPI happens through Trusted Publishing — no tokens anywhere.

**A published version can never be replaced**, only deleted, and the number
stays burned. Test on TestPyPI first with the manual `workflow_dispatch` run.
