from django.shortcuts import redirect, render
from .forms import SignupForm,UserForm
from django.contrib.auth import logout
from django.contrib.auth import get_user_model
from django.contrib.auth import login, authenticate, logout
from django.shortcuts import get_object_or_404
from django.db.models import F
from django.http import JsonResponse
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated



User = get_user_model()  # This ensures you're using the correct Custom User model

# Create your views here.
def register(request):

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            # save the user to db and redirect to home page
            user = form.save()
            login(request,user)
            return redirect('interface')
    else:
        form = SignupForm()

    context={'form':form}
    return render(request,'register.html',context)


def Logout(request):
    logout(request)
    return redirect('interface')


def profile(request,pk):
    user = User.objects.get(id=pk)
    context={'user':user}
    return render(request, 'profile.html',context)

# @login_required(login_url='login')
def edit_profile(request):
    user = request.user
    form = UserForm(instance=user)

    if request.method == 'POST':
        form = UserForm(request.POST, request.FILES, instance=user)
        if form.is_valid():
            form.save()
            return redirect('profile', pk=user.id)

    context={'form': form}
    return render(request, 'edit-profile.html',context)