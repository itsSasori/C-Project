from decimal import Decimal
import json
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
from .models import GameTable, Player,SubscriptionPlan,UserSubscription
from django.db.models import Count  # Import Count for proper filtering
from django.utils.timezone import now
import requests
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt

import random


# Create your views here.

@login_required(login_url='/authentication/login/')
def interface(request):
    from .models import UserSubscription
    # Ensure there's always a valid game_room_id before rendering the template.
    game_room = GameTable.objects.annotate(player_count=Count('players')).filter(player_count__lt=F('max_players')).first()
    usersubcription = UserSubscription.objects.filter(user=request.user, is_active=True).first()
    print(f'User Subscription: {usersubcription}')
    # If no game room found, create a new one
    if not game_room:
        game_room = GameTable.objects.create(created_by=request.user)
        print(f'Game-newrom{game_room.id}')

    return render(request, 'interface.html', {'game_room_id': game_room.id,'usersubscription': usersubcription})


@login_required(login_url='/authentication/login/')
def game_table(request, game_room_id=None):
#    Ensure the URL always uses the correct game_room_id from session.
    user=request.user
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

    if game_room and game_room.is_private and game_room.id != request.session.get('game_room_id'):
        return render(request, "error.html", {"error": "You cannot join this private table without a room code."})

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

    return render(request, 'table.html', {'game_room_id': game_room.id,'room_code': game_room.room_code,'is_private':game_room.is_private,'user':user})

@login_required(login_url='/authentication/login/')
def create_private_table(request):
    # Clear old session game_room_id so they don't get redirected back
    if 'game_room_id' in request.session:
        del request.session['game_room_id']

    # Create fresh private room
    game_room = GameTable.objects.create(created_by=request.user, is_private=True)

    # Store new room in session
    request.session['game_room_id'] = game_room.id

    return redirect(f'/gamedevelopment/game/{game_room.id}/')



@login_required(login_url='/authentication/login/')
def join_private_table(request):
    if request.method == "POST":
        room_code = request.POST.get("room_code", "").upper()
        game_room = GameTable.objects.filter(room_code=room_code).first()
        if not game_room:
            return render(request, "join_room.html", {"error": "Room not found!"})
        
        if game_room.player_count >= game_room.max_players:
            return render(request, "join_room.html", {"error": "Room is full!"})

        # Clear old session if exists
        if 'game_room_id' in request.session:
            del request.session['game_room_id']

        request.session['game_room_id'] = game_room.id
        return redirect(f'/gamedevelopment/game/{game_room.id}/')
    return render(request, "join_room.html")

@login_required(login_url='/authentication/login/')
def subscription_plans(request):
    plans = SubscriptionPlan.objects.all()
    return render(request, "subscriptions.html", {"plans": plans})


@login_required(login_url='/authentication/login/')
def subscribe(request, plan_id):
    plan = get_object_or_404(SubscriptionPlan, id=plan_id)
    subscription = UserSubscription.objects.create(
        user=request.user,
        plan=plan,
        start_date=now()
    )
    return redirect("subscription_success")

@csrf_exempt
def verify_khalti(request, plan_id):
    if request.method == "POST":
        import json
        data = json.loads(request.body)
        token = data.get("token")
        amount = data.get("amount")

        url = "https://khalti.com/api/v2/payment/verify/"
        payload = {
            "token": token,
            "amount": amount
        }
        headers = {
            "Authorization": "Key test_secret_key_xxxxxxxx"
        }
        response = requests.post(url, data=payload, headers=headers)

        if response.status_code == 200:
            # payment success → create subscription
            plan = SubscriptionPlan.objects.get(id=plan_id)
            UserSubscription.objects.filter(user=request.user, is_active=True).update(is_active=False)
            UserSubscription.objects.create(
                user=request.user,
                plan=plan,
                start_date=now()
            )
            return JsonResponse({"success": True})
        else:
            return JsonResponse({"success": False, "error": response.json()})
        
@login_required(login_url='/authentication/login/')        
def redeem_coins(user, amount):
    subscription = UserSubscription.objects.filter(user=user, is_active=True).order_by('-end_date').first()

    if not subscription or not subscription.is_valid():
        raise ValueError("No active subscription to redeem coins.")

    redemption_rate = subscription.redemption_rate
    # Apply redemption rate to the amount user is trying to redeem
    # Example: convert coins into money based on rate
    money_value = amount * redemption_rate

    # Your coin deduction logic stays similar
    # ...
    return money_value


@login_required(login_url='/authentication/login/')
def redeem_page(request):
    from django.db.models import Sum
    from .models import EarnedCoin, UserSubscription
    # Suppose you calculate redeemable coins → convert to Rs.
    user = request.user
    subscription = UserSubscription.objects.filter(user=user, is_active=True).order_by('-end_date').first()
    if not subscription or not subscription.is_valid():
        print(f"[REDEEM] No active subscription found for user {user.id}")
        return render(request, "redeem.html", {"redeemable_money":0,
                                               "redeemable_coins":0,
                                       "error": "No active subscription found."})
    coins = EarnedCoin.objects.filter(
        user=user,
        redeemed=False,
        earned_at__gte=subscription.start_date,
        earned_at__lte=subscription.end_date
    )
    print(f"[REDEEM] User {user.id} - Coins in period (before aggregation): {[c.amount for c in coins]}")
    redeemable_coins = coins.aggregate(total = Sum('amount'))['total'] or 0
    redeemable_money = redeemable_coins * 0.1  # Example conversion Rs 0.1 per coin
    print(f"[REDEEM] User {user.id} - Total redeemable coins: {redeemable_coins}, Money: {redeemable_money}")


    return render(request, "redeem.html", {
        "redeemable_money": redeemable_money,
        "redeemable_coins": redeemable_coins,
    })


@login_required(login_url='/authentication/login/')
def redeem_khalti(request):
    # This will eventually connect with Khalti checkout
    return render(request, "redeem_khalti.html")

# Redemption rate calculation
def calculate_redemption_rate(amount, duration_days):
    base_rate = amount / duration_days
    if duration_days >= 365:
        multiplier = Decimal('1.2')
    elif duration_days >= 180:
        multiplier = Decimal('1.15')
    elif duration_days >= 90:
        multiplier = Decimal('1.10')
    else:
        multiplier = Decimal('1.0')
    return round(base_rate * multiplier, 4)

@csrf_exempt
def calculate_rate(request):
    if request.method == "POST":
        import json
        data = json.loads(request.body)
        amount = Decimal(data.get("amount", 0))
        duration_days = int(data.get("duration_days", 0))

        # Validation
        MIN_AMOUNT = Decimal('500')
        ALLOWED_DURATIONS = [30, 90, 180, 365]
        if amount < MIN_AMOUNT or duration_days not in ALLOWED_DURATIONS:
            return JsonResponse({"success": False, "error": "Invalid input."})

        

        rate = calculate_redemption_rate(amount, duration_days)
        return JsonResponse({"success": True, "rate": rate})

    return JsonResponse({"success": False, "error": "Invalid method."})


@csrf_exempt
def purchase_subscription(request):
    if request.method != "POST":
        return JsonResponse({"success": False, "error": "Invalid method"})
    
    try:
        data = json.loads(request.body)
        print(data)
        amount = Decimal(data.get("amount"))
        duration_days = int(data.get("duration_days"))
        token = data.get("token")
    except Exception:
        return JsonResponse({"success": False, "error": "Invalid request data"})

    # Input validation
    MIN_AMOUNT = Decimal('500')
    ALLOWED_DURATIONS = [30, 90, 180, 365]
    if amount < MIN_AMOUNT or duration_days not in ALLOWED_DURATIONS:
        return JsonResponse({"success": False, "error": "Invalid amount or duration"})
    
    # Verify Khalti payment
    khalti_amount = int(amount * 100)
    url = "https://khalti.com/api/v2/payment/verify/"
    payload = {"token": token, "amount": khalti_amount}
    headers = {"Authorization": "xxxxxxxx"}

    try:
        response = requests.post(url, json=payload, headers=headers)
        result = response.json()
    except Exception:
        return JsonResponse({"success": False, "error": "Failed to verify payment with Khalti"})

    if response.status_code == 200:
        redemption_rate = calculate_redemption_rate(amount, duration_days)
        # Deactivate previous subscriptions
        UserSubscription.objects.filter(user=request.user, is_active=True).update(is_active=False)
        # Create new subscription
        UserSubscription.objects.create(
            user=request.user,
            amount=amount,
            duration_days=duration_days,
            redemption_rate=redemption_rate,
            start_date=now()
        )
        return JsonResponse({"success": True, "rate": redemption_rate})
    
    return JsonResponse({"success": False, "error": result})

