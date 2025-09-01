from datetime import timedelta
from django.db import models
from django.db import models
from django.utils.timezone import now
from django.core.validators import MinValueValidator
from django.conf import settings
import uuid

# Create your models here.


class GameTable(models.Model):
    name = models.CharField(max_length=255)
    max_players = models.IntegerField(default=3)
    table_limit = models.PositiveIntegerField(default=0)
    current_pot = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    is_private = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL,on_delete=models.SET_NULL, null=True, blank=True, related_name='created_games')
    is_active = models.BooleanField(default=False)
    current_turn = models.IntegerField(default=0)  # The player ID whose turn it is
    round_status = models.CharField(max_length=50, choices=[('waiting', 'Waiting'),('betting', 'Betting'),('distribution', 'Distribution'), ('showdown', 'Showdown'),('showdown_after_pack', 'Showdown After Pack')], default='waiting')
    round_number = models.IntegerField(default=1)
    room_code = models.CharField(max_length=10, unique=True, default="", blank=True)

    def save(self, *args, **kwargs):
        if not self.room_code:  # auto-generate code on first save
            self.room_code = str(uuid.uuid4().hex[:6]).upper()
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.name}Game-Table -{self.id}'
    

    
    



class Player(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    table = models.ForeignKey(GameTable, on_delete=models.CASCADE, related_name="players")
    is_blind = models.BooleanField(default=True)
    is_packed = models.BooleanField(default=False)  # If the player has folded
    current_bet = models.PositiveIntegerField(default=0)  # Coins they have bet
    hand_cards = models.JSONField(default=list)  # Stores card values
    is_ready = models.BooleanField(default=False)
    is_turn = models.BooleanField(default=False)
    is_spectator = models.BooleanField(default=False)  # New field for spectator status
    disconnected_at = models.DateTimeField(null=True, blank=True)  # Timestamp when the player disconnected

    class Meta:
        unique_together = ('user', 'table')

    def __str__(self):
        return f"{self.user.username}-Game-Table-{self.table.id}"
    

class Bet(models.Model):
    player = models.ForeignKey(Player, on_delete=models.CASCADE, related_name="bets")
    amount = models.PositiveIntegerField()
    bet_action = models.CharField(max_length=50, choices=[('blind', 'Blind'),('seen', 'Seen'), ('pack', 'Pack'), ('sideshow', 'Sideshow')])
    timestamp = models.DateTimeField(auto_now_add=True)

class Transaction(models.Model):
    user= models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    transaction_type = models.CharField(max_length=50, choices=[('purchase', 'Purchase'), ('redeem', 'Redeem')])
    amount = models.PositiveIntegerField()
    currency = models.CharField(max_length=20, choices=[('coins', 'Coins'), ('gold', 'Gold'), ('diamonds', 'Diamonds')])
    timestamp = models.DateTimeField(auto_now_add=True)

class PrivateTable(models.Model):
    host = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    table = models.OneToOneField(GameTable, on_delete=models.CASCADE)
    password = models.CharField(max_length=10, blank=True, null=True)
    is_active = models.BooleanField(default=True)

class SubscriptionPlan(models.Model):
    name = models.CharField(max_length=50, unique=True)
    price = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)])
    duration_days = models.PositiveIntegerField(help_text="Duration in days")
    description = models.TextField(blank=True, null=True)
   
    
    def __str__(self):
        return f"{self.name} - ${self.price}"  

class UserSubscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    
    # Remove ForeignKey to SubscriptionPlan or make it optional
    plan = models.ForeignKey('SubscriptionPlan', on_delete=models.SET_NULL, null=True, blank=True)
    
    amount = models.DecimalField(max_digits=10, decimal_places=2, validators=[MinValueValidator(0)],null=True)  # custom amount
    duration_days = models.PositiveIntegerField(null=True)  # store actual duration
    redemption_rate = models.DecimalField(max_digits=10, decimal_places=4,null=True)  # rate for coin redemption

    start_date = models.DateTimeField(default=now)
    end_date = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    auto_renew = models.BooleanField(default=False)

    def save(self, *args, **kwargs):
        if not self.end_date:
            self.end_date = self.start_date + timedelta(days=self.duration_days)
        super().save(*args, **kwargs)

    def is_valid(self):
        return self.is_active and self.start_date <= now() <= self.end_date

    
class Payment(models.Model):
    user= models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    subscription = models.ForeignKey(UserSubscription, on_delete=models.CASCADE, related_name='payments')
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_date = models.DateTimeField(default=now)
    transaction_id = models.CharField(max_length=100, unique=True)
    status = models.CharField(max_length=20, choices=[('Success', 'Success'), ('Pending', 'Pending'), ('Failed', 'Failed')], default='Pending')
    
    def __str__(self):
        return f"{self.user.username} - {self.transaction_id} - {self.status}"

# Redemption Model for converting game coins to real money
class Redemption(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    coins_converted = models.IntegerField()
    real_money_earned = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=20, default='pending')  # Pending, Approved, Rejected
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Redemption {self.id} - {self.user.username}"
    

class GameHistory(models.Model):
    # Keep FKs but don’t force cascade-delete
    game = models.ForeignKey(GameTable, on_delete=models.SET_NULL, null=True, blank=True)
    player = models.ForeignKey(Player, on_delete=models.SET_NULL, null=True, blank=True)

    # Snapshots (permanent)
    game_id_snapshot = models.IntegerField(null=True, blank=True)
    game_name_snapshot = models.CharField(max_length=255, null=True, blank=True)
    player_id_snapshot = models.IntegerField(null=True, blank=True)
    player_username_snapshot = models.CharField(max_length=150, null=True, blank=True)

    action = models.CharField(max_length=50,null=True, blank=True)  # e.g., "bet", "pack", "call"
    amount = models.PositiveIntegerField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"[{self.timestamp}] {self.player_username_snapshot or 'Unknown'} - {self.action} ({self.amount})"


class EarnedCoin(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    amount = models.PositiveIntegerField()
    earned_at = models.DateTimeField(default=now)
    redeemed = models.BooleanField(default=False)
    
    def __str__(self):
        return f"{self.user.username} - {self.amount} coins - {'Redeemed' if self.redeemed else 'Not Redeemed'}"

class Challenge(models.Model):
    CHALLENGE_TYPE_CHOICES = [
        ("games", "Games"),
        ("pair", "Pair"),
        ("high_card", "High Card"),
        # Add more types as needed
    ]

    name = models.CharField(max_length=255)
    type = models.CharField(max_length=50, choices=CHALLENGE_TYPE_CHOICES)
    goal = models.PositiveIntegerField()  # e.g., 6 games
    reward = models.PositiveIntegerField()  # coins reward
    is_vip_only = models.BooleanField(default=False)
    assign_to_all = models.BooleanField(default=True)  # Whether to assign to all users upon creation
    
    def save(self, *args, **kwargs):
        # Auto-generate name dynamically
        if self.type == "games":
            self.name = f"Win {self.goal} games"
        elif self.type == "high_card":
            self.name = f"Win {self.goal} with high card"
        elif self.type == "pair":
            self.name = f"Win {self.goal} with pair"
        else:
            # fallback if needed
            self.name = f"Win {self.goal}"

        super().save(*args, **kwargs)

    def __str__(self):
        return self.name
    
class PlayerChallenge(models.Model):
    player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    challenge = models.ForeignKey(Challenge, on_delete=models.CASCADE)
    progress = models.PositiveIntegerField(default=0)
    completed = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("player", "challenge")

    def __str__(self):
        return f"{self.player.username} - {self.challenge.name} - {'Completed' if self.completed else 'In Progress'}"
