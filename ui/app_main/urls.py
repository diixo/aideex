
from django.urls import path
from . import views

app_name = "app_main"

urlpatterns = [
    path("", views.main, name="main"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("confluence", views.confluence, name="confluence"),
    path("codebeamer", views.codebeamer, name="codebeamer"),
    path("diagramming", views.diagramming, name="diagramming"),
    path("aspice-pipeline-dataset", views.aspice_pipeline_dataset, name="aspice_pipeline_dataset"),
    path("aspice-pipeline-dataset-bp", views.aspice_pipeline_dataset_bp, name="aspice_pipeline_dataset_bp"),
]
