from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth import get_user_model
from .models import Challenge, PlayerChallenge
from django.conf import settings
from rest_framework.authtoken.models import Token

User = get_user_model()

# --- Assign new challenge to all existing players ---
@receiver(post_save, sender=Challenge)
def assign_challenge_to_players(sender, instance, created, **kwargs):
    if created and getattr(instance, 'assign_to_all', True):
        users = User.objects.filter(is_bot = False)
        PlayerChallenge.objects.bulk_create([
            PlayerChallenge(player=user, challenge=instance)
            for user in users
        ])

# --- Assign all existing challenges to a newly created user ---
@receiver(post_save, sender=User)
def assign_existing_challenges_to_new_user(sender, instance, created, **kwargs):
    if created:
        challenges = Challenge.objects.all()
        PlayerChallenge.objects.bulk_create([
            PlayerChallenge(player=instance, challenge=ch)
            for ch in challenges
        ])


@receiver(post_save, sender=settings.AUTH_USER_MODEL)
def create_auth_token(sender, instance=None, created=False, **kwargs):
    if created:
        Token.objects.create(user=instance)


