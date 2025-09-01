from django.db import models
from django.contrib.auth.models import AbstractUser

#Create your models here.
class User(AbstractUser):
    coins = models.PositiveIntegerField(default=10000)
    gold = models.PositiveIntegerField(default=0)
    diamonds = models.PositiveIntegerField(default=0)
    avatar = models.ImageField(upload_to="avatars/", blank=True, null=True)
    is_bot = models.BooleanField(default=False)
    is_online = models.BooleanField(default=False)

    def __str__(self):
        return self.username