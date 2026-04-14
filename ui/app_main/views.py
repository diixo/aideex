from django.shortcuts import render


def main(request):

    return render(request, "app_main/index.html", context={
        "title": "AIdeex",
        "description": "AIdeex description"})

# Create your views here.
