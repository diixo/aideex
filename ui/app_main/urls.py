
from django.urls import path
from . import views

app_name = "app_main"

urlpatterns = [
    path("", views.main, name="main"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("confluence", views.confluence, name="confluence"),
    path("codebeamer", views.codebeamer, name="codebeamer"),
    path("diagramming", views.diagramming, name="diagramming"),
]
