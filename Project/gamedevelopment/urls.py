from django.urls import path
from . import views



urlpatterns = [
   path('interface/',views.interface,name="interface"),
]