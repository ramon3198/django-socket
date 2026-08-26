"""URLconf minimo, solo para comprobar que el HTTP sigue llegando a Django."""

from django.http import HttpResponse
from django.urls import path

urlpatterns = [path("", lambda request: HttpResponse("ok"))]
