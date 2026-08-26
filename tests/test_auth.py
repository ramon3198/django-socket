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


async def test_cookie_de_sesion_resuelve_el_usuario(transporte, django_user_model):
    user = await django_user_model.objects.acreate_user("ramon", password="x")
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


async def test_login_required_deja_pasar_al_autenticado(transporte, django_user_model):
    user = await django_user_model.objects.acreate_user("ramon", password="x")
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


async def test_el_orm_funciona_dentro_del_handler(transporte, django_user_model):
    await django_user_model.objects.acreate_user("uno", password="x")
    await django_user_model.objects.acreate_user("dos", password="x")

    @ws("cuenta/", auth=False)
    async def handler(sock):
        await sock.send_json({"total": await User.objects.acount()})

    t = await correr(transporte(path="/cuenta/").cliente_conecta().cliente_cierra())
    assert t.textos == ['{"total": 2}']


async def test_sync_to_async_dentro_del_handler(transporte, django_user_model):
    """El dispatcher envuelve en ThreadSensitiveContext, asi que el ORM sincrono va."""
    await django_user_model.objects.acreate_user("ramon", password="x")

    @ws("primero/", auth=False)
    async def handler(sock):
        from asgiref.sync import sync_to_async

        nombre = await sync_to_async(lambda: User.objects.first().username)()
        await sock.send_text(nombre)

    t = await correr(transporte(path="/primero/").cliente_conecta().cliente_cierra())
    assert t.textos == ["ramon"]


# --------------------------------------------------------- caminos alternativos


async def test_fallback_para_django_menor_que_5(transporte, monkeypatch, django_user_model):
    """
    aget_user llego en Django 5.0. En 4.2 hay que pasar por un hilo; aqui
    forzamos ese camino para que no se pudra sin que nos enteremos.
    """
    import builtins

    user = await django_user_model.objects.acreate_user("antiguo", password="x")
    from asgiref.sync import sync_to_async

    key = await sync_to_async(sesion_de)(user)

    real_import = builtins.__import__

    def sin_aget_user(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "django.contrib.auth" and fromlist and "aget_user" in fromlist:
            raise ImportError("aget_user no existe en Django 4.2")
        return real_import(name, globals, locals, fromlist, level)

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
    from django_socket import auth as auth_mod

    async def revienta(carrier):
        raise RuntimeError("la BD de sesiones no responde")

    monkeypatch.setattr("django.contrib.auth.aget_user", revienta)

    visto = {}

    @ws("quien/")
    async def handler(sock):
        visto["user"] = sock.user

    await correr(transporte(path="/quien/").cliente_conecta().cliente_cierra())
    assert isinstance(visto["user"], AnonymousUser)
    assert "la BD de sesiones no responde" in caplog.text


async def test_sin_app_de_auth_no_se_resuelve_nada(transporte, monkeypatch):
    from django_socket import auth as auth_mod

    monkeypatch.setattr(auth_mod, "_auth_installed", lambda: False)

    visto = {}

    @ws("quien/")
    async def handler(sock):
        visto["user"] = sock.user
        visto["session"] = sock.session

    await correr(transporte(path="/quien/").cliente_conecta().cliente_cierra())
    assert visto == {"user": None, "session": None}
