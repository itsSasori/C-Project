# Generated by Django 5.1.5 on 2025-07-13 05:44

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gamedevelopment', '0026_alter_player_unique_together'),
    ]

    operations = [
        migrations.AddField(
            model_name='player',
            name='is_spectator',
            field=models.BooleanField(default=False),
        ),
    ]
