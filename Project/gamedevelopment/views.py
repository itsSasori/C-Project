from django.shortcuts import get_object_or_404
from django.contrib.auth.models import User
from django.shortcuts import redirect, render
from django.db.models import F
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.contrib.auth.decorators import login_required
from .models import GameTable, Player
# from .models import Game, Player, Bet
# from .serializers import GameSerializer, PlayerSerializer, BetSerializer
import random


# Create your views here.

def interface(request):
    """Ensure there's always a valid game_room_id before rendering the template."""
    game_room = GameTable.objects.filter(players__lt=F('max_players')).first()
    
    # If no game room found, create a new one
    if not game_room:
        game_room = GameTable.objects.create(created_by=request.user)

    return render(request, 'interface.html', {'game_room_id': game_room.id})


@login_required(login_url='/authentication/login/')
def game_table(request, game_room_id=None):
    # If no game_room_id is passed, find an existing game room with space or create a new one
    if not game_room_id:
        # Try to find an existing game room with available space (players < max_players)
        game_room = GameTable.objects.filter(players__lt=F('max_players')).first()
        
        # If no game room is found, create a new one
        if not game_room:
            game_room = GameTable.objects.create(created_by=request.user)

        # Redirect to the game room page with the new or existing game room ID
        return redirect('game_table', game_room_id=game_room.id)

    # If a game_room_id is provided, fetch the game room (or 404 if not found)
    game_room = get_object_or_404(GameTable, id=game_room_id)

    # Ensure the player is added to the game room
    player, created = Player.objects.get_or_create(user=request.user, table_id=game_room_id)
    
    # Add the player to the game room if not already added
    if not game_room.players.filter(user=request.user).exists():
        game_room.players.add(player)
        game_room.save()

    # Render the table with the current game_room_id
    return render(request, 'table.html', {'game_room_id': game_room_id})