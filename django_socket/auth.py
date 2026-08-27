"""Compatibilidad. La implementacion vive ahora en `authentication.py`.

Se mantiene porque `django_socket.auth` existia en 0.1.0 y alguien pudo
importarlo. Lo nuevo (autenticadores conectables, token) esta en
`django_socket.authentication`.
"""

from .authentication import login_required, resolve  # noqa: F401

__all__ = ["resolve", "login_required"]
