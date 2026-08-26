"""Pruebas de humo contra un servidor en marcha: python test_sockets.py [puerto]"""

import asyncio
import json
import sys

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8077
BASE = f"ws://127.0.0.1:{PORT}"
ORIGIN = f"http://127.0.0.1:{PORT}"

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"  {'PASS' if ok else 'FALLA'}  {name}" + (f"  -> {detail}" if detail else ""),
          flush=True)


async def test_echo():
    async with websockets.connect(f"{BASE}/echo/", origin=ORIGIN) as w:
        await w.send("hola")
        check("echo devuelve el mensaje", await w.recv() == "echo: hola")


async def test_path_params():
    async with websockets.connect(f"{BASE}/counter/41/", origin=ORIGIN) as w:
        first = json.loads(await w.recv())
        check("<int:start> llega como int", first == {"recibido": 41, "tipo": "int"}, str(first))
        await w.send("+")
        check("estado entre mensajes", json.loads(await w.recv()) == {"contador": 42})


async def test_broadcast():
    async with websockets.connect(f"{BASE}/chat/general/", origin=ORIGIN) as a:
        async with websockets.connect(f"{BASE}/chat/general/", origin=ORIGIN) as b:
            entra = json.loads(await a.recv())
            check("A ve entrar a B", entra["tipo"] == "entra", str(entra))
            await b.send("buenas")
            ma = json.loads(await a.recv())
            mb = json.loads(await b.recv())
            check("broadcast llega a los dos", ma == mb == {"tipo": "mensaje", "quien": "anonimo", "texto": "buenas"}, str(ma))
        sale = json.loads(await a.recv())
        check("A ve salir a B", sale == {"tipo": "sale", "quien": "anonimo"}, str(sale))


async def test_aislamiento_de_salas():
    async with websockets.connect(f"{BASE}/chat/uno/", origin=ORIGIN) as a:
        async with websockets.connect(f"{BASE}/chat/dos/", origin=ORIGIN) as b:
            await b.send("no deberia verse en uno")
            try:
                leak = await asyncio.wait_for(a.recv(), timeout=0.6)
                check("las salas estan aisladas", False, f"fuga: {leak}")
            except asyncio.TimeoutError:
                check("las salas estan aisladas", True)


async def test_anonimo():
    async with websockets.connect(f"{BASE}/whoami/", origin=ORIGIN) as w:
        data = json.loads(await w.recv())
        check("usuario anonimo por defecto", data["autenticado"] is False and data["usuario"] == "AnonymousUser", str(data))
        check("query_params parseados", data["query"] == {}, str(data["query"]))


async def test_query_params():
    async with websockets.connect(f"{BASE}/whoami/?token=abc&n=3", origin=ORIGIN) as w:
        data = json.loads(await w.recv())
        check("query string disponible", data["query"] == {"token": "abc", "n": "3"}, str(data["query"]))


async def test_login_required_rechaza():
    async with websockets.connect(f"{BASE}/panel/", origin=ORIGIN) as w:
        try:
            await w.recv()
            check("login_required cierra con 4401", False, "no cerro")
        except ConnectionClosed as exc:
            check("login_required cierra con 4401", exc.rcvd.code == 4401, f"code={exc.rcvd.code}")


async def test_sesion_autenticada(session_cookie):
    headers = {"Cookie": f"sessionid={session_cookie}"}
    async with websockets.connect(f"{BASE}/panel/", origin=ORIGIN, additional_headers=headers) as w:
        data = json.loads(await w.recv())
        check("sesion de Django reconocida", data["hola"] == "ramon", str(data))


async def test_ruta_inexistente():
    async with websockets.connect(f"{BASE}/no-existe/", origin=ORIGIN) as w:
        try:
            await w.recv()
            check("ruta inexistente cierra con 4404", False, "no cerro")
        except ConnectionClosed as exc:
            check("ruta inexistente cierra con 4404", exc.rcvd.code == 4404, f"code={exc.rcvd.code}")


async def test_origen_malicioso():
    """Un origen ajeno no debe llegar ni a abrir el socket: 403 en el handshake."""
    try:
        async with websockets.connect(f"{BASE}/echo/", origin="http://atacante.example"):
            check("origen ajeno rechazado (anti-CSWSH)", False, "el handshake paso")
    except InvalidStatus as exc:
        check("origen ajeno rechazado (anti-CSWSH)", exc.response.status_code == 403,
              f"status={exc.response.status_code}")


async def test_http_sigue_vivo():
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/admin/login/") as r:
        body = r.read().decode("utf-8", "replace")
    check("HTTP normal de Django intacto", r.status == 200 and "csrfmiddlewaretoken" in body, f"status={r.status}")


async def test_message_igual_a_str():
    """Message se compara con str sin sacar .text a mano."""
    async with websockets.connect(f"{BASE}/counter/10/", origin=ORIGIN) as w:
        await w.recv()
        await w.send("sube")
        check("msg != 'reset' incrementa", json.loads(await w.recv()) == {"contador": 11})
        await w.send("reset")
        check("msg == 'reset' compara con str", json.loads(await w.recv()) == {"contador": 0})


async def test_group_declarativo():
    """group='auto:{sala}' debe dejar sock.group puesto sin llamar a join()."""
    async with websockets.connect(f"{BASE}/grupo/siete/", origin=ORIGIN) as w:
        data = json.loads(await w.recv())
        check("group= rellena el grupo desde la ruta",
              data == {"group": "auto:siete", "groups": ["auto:siete"]}, str(data))


async def test_broadcast_sin_grupo_avisa():
    """broadcast(data) sin grupo debe fallar con un mensaje util, no con AttributeError."""
    import django_socket
    sock = django_socket.WebSocket({"type": "websocket"}, None, None)
    try:
        await sock.broadcast({"x": 1})
        check("broadcast sin grupo explica que hacer", False, "no lanzo nada")
    except ValueError as exc:
        util = "sock.join" in str(exc) and "to=" in str(exc)
        check("broadcast sin grupo explica que hacer", util, str(exc)[:70])


async def test_orm_en_handler():
    async with websockets.connect(f"{BASE}/usuarios/", origin=ORIGIN) as w:
        uno = json.loads(await w.recv())
        dos = json.loads(await w.recv())
        check("ORM async dentro del handler", uno["nombres"] == ["ramon"], str(uno))
        check("sync_to_async dentro del handler", dos == {"sync_to_async": "ramon"}, str(dos))


async def test_asgi_sin_configurar():
    """El asgi.py de startproject sirve WebSockets sin editarlo."""
    from django.conf import settings
    import demo.asgi

    fuente = open(demo.asgi.__file__, encoding="utf-8").read()
    importa = any(
        linea.strip().startswith(("import django_socket", "from django_socket"))
        for linea in fuente.splitlines()
    )
    check("asgi.py del proyecto no importa la libreria", not importa)
    check("ASGI_APPLICATION no hace falta",
          not getattr(settings, "ASGI_APPLICATION", None))
    check("solo esta en INSTALLED_APPS", "django_socket" in settings.INSTALLED_APPS)


def crear_sesion():
    """Genera una sesion autenticada real, como si el usuario hubiera hecho login."""
    import os, django
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")
    django.setup()
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
    from django.contrib.auth.models import User
    from django.contrib.sessions.backends.db import SessionStore

    user = User.objects.get(username="ramon")
    s = SessionStore()
    s[SESSION_KEY] = str(user.pk)
    s[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    s[HASH_SESSION_KEY] = user.get_session_auth_hash()
    s.create()
    return s.session_key


async def main(cookie):
    print(f"\nProbando {BASE}\n")
    for test in (
        test_echo, test_path_params, test_broadcast, test_aislamiento_de_salas,
        test_anonimo, test_query_params, test_login_required_rechaza,
        test_ruta_inexistente, test_origen_malicioso, test_http_sigue_vivo,
        test_message_igual_a_str, test_group_declarativo, test_orm_en_handler,
        test_asgi_sin_configurar, test_broadcast_sin_grupo_avisa,
    ):
        try:
            await asyncio.wait_for(test(), timeout=10)
        except asyncio.TimeoutError:
            check(test.__name__, False, "COLGADO (>10s)")
        except Exception as exc:
            check(test.__name__, False, f"{type(exc).__name__}: {exc}")
    try:
        await asyncio.wait_for(test_sesion_autenticada(cookie), timeout=10)
    except Exception as exc:
        check("test_sesion_autenticada", False, f"{type(exc).__name__}: {exc}")

    fallos = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(fallos)}/{len(results)} OK")
    if fallos:
        print("Fallan: " + ", ".join(fallos))
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(crear_sesion())))
