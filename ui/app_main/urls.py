
from django.urls import path
from . import views

app_name = "app_main"

urlpatterns = [
    path("", views.main, name="main"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("features-04", views.features_04, name="features_04"),
    path("features-04/<str:feature_id>/lifecycle", views.lifecycle_scenario, name="lifecycle_scenario"),
    path("features-04/<str:feature_id>/timeline", views.feature_timeline, name="feature_timeline"),
]
