"""Etiqueta para incluir el cliente JS.

    {% load django_socket %}
    {% ws_client %}

Deja disponible `djangoSocket(ruta, opciones)` en la pagina.
"""

from django import template
from django.templatetags.static import static
from django.utils.html import format_html

register = template.Library()


@register.simple_tag
def ws_client(defer: bool = False):
    return format_html(
        '<script src="{}"{}></script>',
        static("django_socket/client.js"),
        " defer" if defer else "",
    )


@register.simple_tag
def ws_client_url():
    """Solo la URL, por si la metes en tu propio bundle."""
    return static("django_socket/client.js")
