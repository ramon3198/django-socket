"""Template tag that includes the JS client.

    {% load django_socket %}
    {% ws_client %}

Makes `djangoSocket(path, options)` available on the page.
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
    """Just the URL, in case you bundle it yourself."""
    return static("django_socket/client.js")
