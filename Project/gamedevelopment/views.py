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
from django.db.models import Count  # Import Count for proper filtering
import random


# Create your views here.

@login_required(login_url='/authentication/login/')
def interface(request):
    """Ensure there's always a valid game_room_id before rendering the template."""
    game_room = GameTable.objects.annotate(player_count=Count('players')).filter(player_count__lt=F('max_players')).first()
    
    # If no game room found, create a new one
    if not game_room:
        game_room = GameTable.objects.create(created_by=request.user)
    print(game_room.id)

    return render(request, 'interface.html', {'game_room_id': game_room.id})


@login_required(login_url='/authentication/login/')
def game_table(request, game_room_id=None):
    # If no specific game_room_id is provided, find or create a game room
    if not game_room_id:
        # Find a game room that has available space
        game_room = GameTable.objects.annotate(player_count=Count('players')).filter(player_count=1).first()
        
        if not game_room:
            # Create a new game room if no available one exists
            game_room = GameTable.objects.create(created_by=request.user)

        # Store the game_room_id in session
        request.session['game_room_id'] = game_room.id

        # Redirect to the game table page with the assigned room ID
        return redirect('game_table', game_room_id=game_room.id)

    # Fetch the game room, or create a new one if it doesn't exist
    game_room, created = GameTable.objects.get_or_create(id=game_room_id, defaults={'created_by': request.user})


    # Check if the game room is full
    if game_room.players.count() >= game_room.max_players:
        # Create a new game table since the current one is full
        game_room = GameTable.objects.create(created_by=request.user)
        request.session['game_room_id'] = game_room.id  # Store the new game room ID
    else:
        # Ensure the player is added to the existing game room
        player, created = Player.objects.get_or_create(user=request.user, table=game_room)

        # Add the player to the game room if not already in it
        if not game_room.players.filter(user=request.user).exists():
            game_room.players.add(player)
            game_room.save()

        # Store the game_room_id in session
        request.session['game_room_id'] = game_room.id

    # Render the game room
    return render(request, 'table.html', {'game_room_id': game_room.id})
