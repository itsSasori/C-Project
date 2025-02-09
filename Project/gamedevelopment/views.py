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
    # Ensure there's always a valid game_room_id before rendering the template.
    game_room = GameTable.objects.annotate(player_count=Count('players')).filter(player_count__lt=F('max_players')).first()

    # If no game room found, create a new one
    if not game_room:
        game_room = GameTable.objects.create(created_by=request.user)
        print(f'Game-newrom{game_room.id}')

    return render(request, 'interface.html', {'game_room_id': game_room.id})


@login_required(login_url='/authentication/login/')
def game_table(request, game_room_id=None):
#    Ensure the URL always uses the correct game_room_id from session.

    session_game_room_id = request.session.get('game_room_id', None)

    #  If session has a game_room_id and the URL does not match, redirect
    if session_game_room_id and (game_room_id is None or int(game_room_id) != int(session_game_room_id)):
        return redirect(f'/gamedevelopment/game/{session_game_room_id}/')

    # If no game_room_id in session, try to use the URL value
    if game_room_id and not session_game_room_id:
        request.session['game_room_id'] = int(game_room_id)

    # Ensure we get a valid game room
    game_room_id = request.session.get('game_room_id')
    game_room = GameTable.objects.annotate(player_count=Count('players')).filter(id=game_room_id, player_count__lt=F('max_players')).first()

    #  If the game room is invalid, create/find a new one and redirect
    if not game_room:
        request.session.pop('game_room_id', None)  # Remove invalid session data
        game_room = GameTable.objects.annotate(player_count=Count('players')).filter(player_count__lt=F('max_players')).first()
        if not game_room:
            game_room = GameTable.objects.create(created_by=request.user)

        #  Store the correct game_room_id in session
        request.session['game_room_id'] = game_room.id

        # Redirect to the correct game room
        return redirect(f'/gamedevelopment/game/{game_room.id}/')

    return render(request, 'table.html', {'game_room_id': game_room.id})



