# Generated by Django 5.1.5 on 2025-02-07 02:51

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gamedevelopment', '0006_rename_hand_player_hand_cards_gametable_round_number'),
    ]

    operations = [
        migrations.AlterField(
            model_name='gametable',
            name='max_players',
            field=models.IntegerField(default=2),
        ),
    ]
