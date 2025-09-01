from django.urls import path
from . import views



urlpatterns = [
   path("create/", views.create_private_table, name="create_private_table"),
   path("join/", views.join_private_table, name="join_private_table"),
   path('interface/',views.interface,name="interface"),
   path('game/<int:game_room_id>/',views.game_table, name='game_table'),
   path("plans/", views.subscription_plans, name="subscription_plans"),
   path("subscribe/<int:plan_id>/", views.subscribe, name="subscribe"),
   path("verify-khalti/<int:plan_id>/", views.verify_khalti, name="verify_khalti"),
   path("redeem_khalti/", views.redeem_khalti, name="redeem_khalti"),
   path("redeem/", views.redeem_page, name="redeem_page"),
   path("calculate-rate/", views.calculate_rate, name="calculate_rate"),
   path("purchase-subscription/", views.purchase_subscription, name="purchase_subscription"),
   path('challenges/', views.player_challenges_list, name='player_challenges'),
   path('challenge-page/', views.challenge_page, name='challenge_page'),
   path('claim-challenge/<int:challenge_id>/', views.claim_challenge, name='claim_challenge'),
]