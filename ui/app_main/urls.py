
from django.urls import path
from . import views

app_name = "app_main"

urlpatterns = [
    path("", views.main, name="main"),
    path("dashboard", views.dashboard, name="dashboard"),
    path("aispice", views.aispice, name="aispice"),
    path("aispice/process-grid", views.aspice_process_grid, name="aspice_process_grid"),
    path("aispice/<str:feature_id>/lifecycle", views.lifecycle_scenario, name="lifecycle_scenario"),
    path("aispice/<str:feature_id>/timeline", views.feature_timeline, name="feature_timeline"),
    path("codebeamer", views.codebeamer, name="codebeamer"),
    path("codebeamer/select-project", views.codebeamer_select_project, name="codebeamer_select_project"),
    path("codebeamer/evaluate", views.codebeamer_evaluate, name="codebeamer_evaluate"),
    path("codebeamer/pull", views.codebeamer_pull_view, name="codebeamer_pull"),
    path("api/codebeamer/status/<int:project_id>", views.codebeamer_status_proxy, name="codebeamer_status"),
    path("api/codebeamer/local-projects", views.codebeamer_local_projects_proxy, name="codebeamer_local_projects"),
    path("api/codebeamer/server-projects", views.codebeamer_server_projects_proxy, name="codebeamer_server_projects"),
    path("codebeamer/tracker/<int:tracker_id>", views.codebeamer_tracker, name="codebeamer_tracker"),
    path("codebeamer/tracker/<int:tracker_id>/create-item", views.codebeamer_create_item, name="codebeamer_create_item"),
    path("codebeamer/tree", views.codebeamer_tree, name="codebeamer_tree"),
    path("codebeamer/item/<int:item_id>", views.codebeamer_item, name="codebeamer_item"),
    path("codebeamer/save", views.codebeamer_save, name="codebeamer_save"),
]
