# Generated by Django 5.1.5 on 2025-07-22 07:25

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('gamedevelopment', '0028_alter_gametable_round_status'),
    ]

    operations = [
        migrations.AlterField(
            model_name='gametable',
            name='table_limit',
            field=models.PositiveIntegerField(default=0),
        ),
    ]
