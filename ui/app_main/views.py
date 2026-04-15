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


def confluence(request):
    return render(request, "app_main/confluence.html", context={
        "title": "AIdelix - Confluence",
        "description": "AIdelix confluence description"})


def codebeamer(request):
    return render(request, "app_main/codebeamer.html", context={
        "title": "AIdelix - Codebeamer",
        "description": "AIdelix codebeamer description"})


def diagramming(request):
    return render(request, "app_main/diagramming.html", context={
        "title": "AIdelix - Diagramming",
        "description": "AIdelix diagramming description"})
