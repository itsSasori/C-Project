# serializers.py
from rest_framework import serializers
from .models import Challenge, PlayerChallenge

class PlayerChallengeSerializer(serializers.ModelSerializer):
    challenge_name = serializers.CharField(source="challenge.name", read_only=True)
    reward = serializers.IntegerField(source="challenge.reward", read_only=True)
    goal = serializers.IntegerField(source="challenge.goal", read_only=True)

    class Meta:
        model = PlayerChallenge
        fields = ["id", "challenge_name", "progress", "goal", "reward", "completed"]
