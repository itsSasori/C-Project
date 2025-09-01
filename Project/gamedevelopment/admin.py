from django.contrib import admin
from .models import *
from rest_framework.authtoken.models import Token
# Register your models here.
admin.site.register(Token)
admin.site.register(GameTable)
admin.site.register(Player)
admin.site.register(Bet)
admin.site.register(Transaction)
admin.site.register(PrivateTable)
admin.site.register(SubscriptionPlan)
admin.site.register(UserSubscription)
admin.site.register(Payment)
admin.site.register(Redemption)
admin.site.register(GameHistory)
admin.site.register(EarnedCoin)
admin.site.register(Challenge)
admin.site.register(PlayerChallenge)
