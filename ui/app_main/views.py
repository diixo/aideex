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


def codebeamer(request):
    return render(request, "app_main/codebeamer.html", context={
        "title": "AIdeex - Codebeamer",
        "description": "AIdeex codebeamer description"})


def diagramming(request):
    return render(request, "app_main/diagramming.html", context={
        "title": "AIdeex - Diagramming",
        "description": "AIdeex diagramming description"})
