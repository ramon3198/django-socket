"""Sesion y usuario resueltos desde la cookie del handshake. Con BD real."""

import pytest
from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY
from django.contrib.auth.models import AnonymousUser, User
from django.contrib.sessions.backends.db import SessionStore

from django_socket import dispatch, login_required, ws

pytestmark = pytest.mark.django_db(transaction=True)


def sesion_de(user) -> str:
    """Una sesion autenticada de verdad, como la que dejaria un login."""
    s = SessionStore()
    s[SESSION_KEY] = str(user.pk)
    s[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    s[HASH_SESSION_KEY] = user.get_session_auth_hash()
    s.create()
    return s.session_key


async def correr(t):
    await dispatch.handle_websocket(t.scope, t.receive, t.send)
    return t


async def test_sin_cookie_el_usuario_es_anonimo(transporte):
    visto = {}

    @ws("quien/")
    async def handler(sock):
        visto["user"] = sock.user

    await correr(transporte(path="/quien/").cliente_conecta().cliente_cierra())
    assert isinstance(visto["user"], AnonymousUser)
    assert visto["user"].is_authenticated is False


async def test_cookie_de_sesion_resuelve_el_usuario(transporte, crear_usuario):
    user = await crear_usuario("ramon")
    from asgiref.sync import sync_to_async

    key = await sync_to_async(sesion_de)(user)

    visto = {}

    @ws("quien/")
    async def handler(sock):
        visto["user"] = sock.user

    await correr(
        transporte(path="/quien/", headers={"Cookie": f"sessionid={key}"})
        .cliente_conecta()
        .cliente_cierra()
    )
    assert visto["user"].username == "ramon"
    assert visto["user"].is_authenticated


async def test_cookie_basura_no_revienta(transporte):
    """Una sesion invalida debe dar AnonymousUser, no una excepcion."""
    visto = {}

    @ws("quien/")
    async def handler(sock):
        visto["user"] = sock.user

    await correr(
        transporte(path="/quien/", headers={"Cookie": "sessionid=no-existe"})
        .cliente_conecta()
        .cliente_cierra()
    )
    assert isinstance(visto["user"], AnonymousUser)


async def test_auth_false_no_resuelve_nada(transporte):
    visto = {}

    @ws("publico/", auth=False)
    async def handler(sock):
        visto["user"] = sock.user
        visto["session"] = sock.session

    await correr(transporte(path="/publico/").cliente_conecta().cliente_cierra())
    assert visto == {"user": None, "session": None}


async def test_login_required_cierra_con_4401(transporte):
    @ws("panel/")
    @login_required
    async def handler(sock):
        await sock.send_text("no deberia llegar")

    t = await correr(transporte(path="/panel/").cliente_conecta())
    assert t.cierre["code"] == 4401
    assert t.textos == []


async def test_login_required_deja_pasar_al_autenticado(transporte, crear_usuario):
    user = await crear_usuario("ramon")
    from asgiref.sync import sync_to_async

    key = await sync_to_async(sesion_de)(user)

    @ws("panel/")
    @login_required
    async def handler(sock):
        await sock.send_text(f"hola {sock.user.username}")

    t = await correr(
        transporte(path="/panel/", headers={"Cookie": f"sessionid={key}"})
        .cliente_conecta()
        .cliente_cierra()
    )
    assert t.textos == ["hola ramon"]


async def test_el_orm_funciona_dentro_del_handler(transporte, crear_usuario):
    await crear_usuario("uno")
    await crear_usuario("dos")

    @ws("cuenta/", auth=False)
    async def handler(sock):
        await sock.send_json({"total": await User.objects.acount()})

    t = await correr(transporte(path="/cuenta/").cliente_conecta().cliente_cierra())
    assert t.textos == ['{"total": 2}']


async def test_sync_to_async_dentro_del_handler(transporte, crear_usuario):
    """El dispatcher envuelve en ThreadSensitiveContext, asi que el ORM sincrono va."""
    await crear_usuario("ramon")

    @ws("primero/", auth=False)
    async def handler(sock):
        from asgiref.sync import sync_to_async

        nombre = await sync_to_async(lambda: User.objects.first().username)()
        await sock.send_text(nombre)

    t = await correr(transporte(path="/primero/").cliente_conecta().cliente_cierra())
    assert t.textos == ["ramon"]


# --------------------------------------------------------- caminos alternativos


async def test_fallback_para_django_menor_que_5(transporte, monkeypatch, crear_usuario):
    """
    `aget_user` llego en Django 5.0. Antes hay que pasar por un hilo.

    En 5.0+ forzamos ese camino con un ImportError para que no se pudra sin que
    nos enteremos. En 4.2 no hay nada que forzar: es el camino de verdad, y
    este mismo test lo ejecuta tal cual.
    """
    import builtins

    import django

    user = await crear_usuario("antiguo")
    from asgiref.sync import sync_to_async

    key = await sync_to_async(sesion_de)(user)

    real_import = builtins.__import__

    def sin_aget_user(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "django.contrib.auth" and fromlist and "aget_user" in fromlist:
            raise ImportError("aget_user no existe en Django 4.2")
        return real_import(name, globals, locals, fromlist, level)

    if django.VERSION >= (5, 0):
        monkeypatch.setattr(builtins, "__import__", sin_aget_user)

    visto = {}

    @ws("quien/")
    async def handler(sock):
        visto["user"] = sock.user

    await correr(
        transporte(path="/quien/", headers={"Cookie": f"sessionid={key}"})
        .cliente_conecta()
        .cliente_cierra()
    )
    assert visto["user"].username == "antiguo"


async def test_un_fallo_resolviendo_deja_anonimo_y_lo_loguea(transporte, monkeypatch, caplog):
    """Que la sesion explote no debe tumbar la conexion."""
    import django

    async def revienta_async(carrier):
        raise RuntimeError("la BD de sesiones no responde")

    def revienta_sync(carrier):
        raise RuntimeError("la BD de sesiones no responde")

    # Cada version resuelve el usuario por un sitio distinto: parchea el que toca.
    if django.VERSION >= (5, 0):
        monkeypatch.setattr("django.contrib.auth.aget_user", revienta_async)
    else:
        monkeypatch.setattr("django.contrib.auth.get_user", revienta_sync)

    visto = {}

    @ws("quien/")
    async def handler(sock):
        visto["user"] = sock.user

    await correr(transporte(path="/quien/").cliente_conecta().cliente_cierra())
    assert isinstance(visto["user"], AnonymousUser)
    assert "la BD de sesiones no responde" in caplog.text


async def test_con_auth_activo_user_nunca_es_none(transporte, monkeypatch):
    """
    Con `auth` activo, `sock.user` siempre es un objeto usuario.

    Si ningun autenticador reconoce a nadie -- sin cookie, sin las apps de auth
    instaladas, token invalido -- queda `AnonymousUser`, no `None`. Asi
    `sock.user.is_authenticated` funciona sin comprobar None antes, que era la
    trampa del comportamiento anterior.
    """
    from django.apps import apps

    monkeypatch.setattr(apps, "is_installed", lambda n: False)

    visto = {}

    @ws("quien/")
    async def handler(sock):
        visto["user"] = sock.user
        visto["session"] = sock.session

    await correr(transporte(path="/quien/").cliente_conecta().cliente_cierra())
    assert isinstance(visto["user"], AnonymousUser)
    assert visto["session"] is None       # sin app de sesiones, no hay sesion


async def test_auth_false_deja_user_en_none(transporte):
    """`auth=False` es lo unico que deja `sock.user` sin objeto."""
    visto = {}

    @ws("publico2/", auth=False)
    async def handler(sock):
        visto["user"] = sock.user

    await correr(transporte(path="/publico2/").cliente_conecta().cliente_cierra())
    assert visto["user"] is None
