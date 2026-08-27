from django.contrib import admin
from django.urls import path

from chat import views
from ejemplos import views as ejemplos_views

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", views.room, name="home"),
    path("sala/<str:room_name>/", views.room, name="room"),
    path("ejemplos/", ejemplos_views.index, name="ejemplos"),
]
