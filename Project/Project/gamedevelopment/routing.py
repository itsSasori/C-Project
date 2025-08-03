# routing.py

from django.urls import re_path
from gamedevelopment.consumers import GameConsumer

websocket_urlpatterns = [
    re_path(r'^ws/game/(?P<game_room_id>\d+)/$', GameConsumer.as_asgi())
]

