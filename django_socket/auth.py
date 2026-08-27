"""Compatibility. The implementation now lives in `authentication.py`.

Kept because `django_socket.auth` existed in 0.1.0 and somebody may have
imported it. The newer parts (pluggable authenticators, token) are in
`django_socket.authentication`.
"""

from .authentication import login_required, resolve  # noqa: F401

__all__ = ["resolve", "login_required"]
