from django.shortcuts import render


def main(request):

    return render(request, "app_main/index.html", context={
        "title": "AIdeex",
        "description": "AIdeex description"})

def dashboard(request):

    return render(request, "app_main/dashboard.html", context={
        "title": "AIdeex - Dashboard",
        "description": "AIdeex dashboard description"})

# Create your views here.
