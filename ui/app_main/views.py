from django.shortcuts import render, redirect


def main(request):
    return redirect(to="app_main:confluence")
    return render(request, "app_main/index.html", context={
        "title": "AIdeex",
        "description": "AIdeex description"})

def dashboard(request):

    return render(request, "app_main/dashboard.html", context={
        "title": "AIdeex - Dashboard",
        "description": "AIdeex dashboard description"})


def confluence(request):
    return render(request, "app_main/confluence.html", context={
        "title": "AIdeex - Confluence",
        "description": "AIdeex confluence description"})

# Create your views here.
