from django.shortcuts import render, redirect


def main(request):
    #return redirect(to="app_main:confluence")
    return render(request, "app_main/index.html", context={
        "title": "AIdelix",
        "description": "AIdelix description"})


def dashboard(request):
    return render(request, "app_main/dashboard.html", context={
        "title": "AIdelix - Dashboard",
        "description": "AIdelix dashboard description"})


def requirements(request):
    return render(request, "app_main/requirements.html", context={
        "title": "AIdelix - Requirements",
        "description": "AIdelix requirements description"})

