from django.shortcuts import render

# Create your views here.

def interface(request):
    context={}
    return render(request, 'interface.html',context)

def table(request):
    context = {}
    return render(request, 'table.html', context)
