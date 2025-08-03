from django.urls import path
from . import views



urlpatterns = [
   path('interface/',views.interface,name="interface"),
   path('game/<int:game_room_id>/',views.game_table, name='game_table'),
]