# Generated by Django 5.1.5 on 2025-02-12 07:02

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gamedevelopment', '0015_alter_gametable_max_players'),
    ]

    operations = [
        migrations.DeleteModel(
            name='Card',
        ),
        migrations.AlterField(
            model_name='bet',
            name='bet_action',
            field=models.CharField(choices=[('blind', 'Blind'), ('seen', 'Seen'), ('pack', 'Pack'), ('sideshow', 'Sideshow')], max_length=50),
        ),
    ]
