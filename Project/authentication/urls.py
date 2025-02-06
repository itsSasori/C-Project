from django.urls import path
from . import views
from django.contrib.auth import views as auth_views
from django.views.decorators.csrf import csrf_exempt
from .forms import LoginForm



urlpatterns = [
   path('profile/<int:pk>/',views.profile,name="profile"),
    path('register/',views.register,name='register'),
    path('logout/',views.Logout,name='logout'),
    path('edit_profile/',views.edit_profile,name='edit-profile'),
    path('login/', auth_views.LoginView.as_view(template_name='login.html', authentication_form=LoginForm), name='login'),  
]