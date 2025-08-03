from datetime import timedelta
from django.db import models
from django.db import models
from django.utils.timezone import now
from django.core.validators import MinValueValidator
from django.conf import settings
from django.utils import timezone

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
    is_active = models.BooleanField(default=True)
    
    def __str__(self):
        return f"{self.name} - ${self.price}"  

class UserSubscription(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    plan = models.ForeignKey(SubscriptionPlan, on_delete=models.CASCADE)
    start_date = models.DateTimeField(default=now)
    end_date = models.DateTimeField()
    is_active = models.BooleanField(default=True)
    auto_renew = models.BooleanField(default=False)
    
    def save(self, *args, **kwargs):
        if not self.end_date:
            self.end_date = self.start_date + timedelta(days=self.plan.duration_days)
        super().save(*args, **kwargs)
    
    def __str__(self):
        return f"{self.user.username} - {self.plan.name}"
    
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
    game = models.ForeignKey(GameTable, on_delete=models.CASCADE)
    player = models.ForeignKey(Player, on_delete=models.CASCADE)
    action = models.CharField(max_length=50)  # e.g., "bet", "fold", "call"
    amount = models.PositiveIntegerField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"GameHistory {self.id} - {self.player.user.username}"


# class PremiumTable(models.Model):
#     """Represents a premium Teen Patti table with exclusive features."""

#     name = models.CharField(max_length=255, unique=True, help_text="Name of the premium table (e.g., 'Diamond Lounge', 'High Rollers')")
#     min_buy_in = models.PositiveIntegerField(default=10000, help_text="Minimum chips required to join the table")
#     small_blind = models.PositiveIntegerField(default=100, help_text="Amount of the small blind")
#     big_blind = models.PositiveIntegerField(default=200, help_text="Amount of the big blind")
#     max_bet_limit = models.PositiveIntegerField(default=5000, help_text="Maximum bet allowed at the table")
#     # is_vip = models.BooleanField(default=True, help_text="Whether this table is a VIP table (can have special styling)")  # Optional for visual distinction
#     required_level = models.PositiveIntegerField(default=10, help_text="Minimum player level required to access the table")
#     entry_fee = models.PositiveIntegerField(blank=True, null=True, help_text="Optional entry fee (in chips) to join the table")
#     # Add a field for a background image or theme if you want to visually distinguish premium tables
#     # background_image = models.ImageField(upload_to='premium_table_backgrounds/', blank=True, null=True)

#     # Players are handled through a separate model (see below) or through a ManyToManyField (less recommended for real-time game state)
#     # players = models.ManyToManyField(settings.AUTH_USER_MODEL, through='PlayerAtPremiumTable', related_name='premium_tables')  # Less efficient for real-time updates

#     def __str__(self):
#         return self.name


# class PlayerAtPremiumTable(models.Model):  # For tracking players at specific tables (more efficient for real-time)
#     """Represents a player's presence at a premium table."""

#     player = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='premium_table_sessions') # Link to your settings.AUTH_USER_MODEL model
#     table = models.ForeignKey(PremiumTable, on_delete=models.CASCADE, related_name='players_at_table')
#     joined_at = models.DateTimeField(auto_now_add=True)
#     # Add other fields as needed, e.g., current chip balance at the table, etc.

#     class Meta:
#         unique_together = ('player', 'table')  # A player can only be at one table at a time

