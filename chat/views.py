from django.shortcuts import render


def room(request, room_name="general"):
    return render(request, "chat/room.html", {"room": room_name})
