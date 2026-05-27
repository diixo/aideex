
from django.urls import path
from . import views

app_name = "app_main"

urlpatterns = [
    path("", views.main, name="main"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("aispice", views.aispice, name="aispice"),
    path("aispice/<str:feature_id>/lifecycle", views.lifecycle_scenario, name="lifecycle_scenario"),
    path("aispice/<str:feature_id>/timeline", views.feature_timeline, name="feature_timeline"),
    path("codebeamer", views.codebeamer, name="codebeamer"),
]
