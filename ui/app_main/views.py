import json
import os
from pathlib import Path
from typing import Any

from django.conf import settings
from django.shortcuts import render, redirect

from .api import make_get_request, make_post_request, make_put_request, make_delete_request
from .api import backend_get, backend_post

from .aspice_compliance_checker import inspect_case
from .aspice_validate_dataset import validate_dataset, summarize_issues

from pybeamer import Codebeamer
from pybeamer.render import STATUS_COLORS, PRIORITY_COLORS, TRACKER_ICONS
from pybeamer.tracker import Tracker


# Material Icons name per tracker type_id. Types that don't have an
# explicit assignment fall back to "label" via the .get() in the view.
_WORK_ITEM_TYPES = {"requirement", "bug", "epic", "issue", "user story", "task", "document", "testrun", "risk", "change request", "work log"}


def _to_cb_date(d):
    return f"{d}T00:00:00.000Z" if d else None


def main(request):
    #return redirect(to="app_main:confluence")
    return render(request, "app_main/index.html", context={
        "title": "AI-delix",
        "description": "AI-delix description"})


def dashboard(request):
    return render(request, "app_main/dashboard.html", context={
        "title": "AI-delix - Dashboard",
        "description": "AI-delix dashboard description"})


_FEATURES_04_DATASET = os.path.join(
    settings.BASE_DIR, "..", "data", "aspice-new",
    "aspice_compliance_checker_test_dataset_v04.json",
)

_CORPUS_ROOT = (Path(settings.BASE_DIR).parent / "corpus").resolve()


def _collect_aspice_counts(dataset):
    cases = dataset.get("inspection_cases", [])
    unique_features = {
        case.get("feature", {}).get("id") or case.get("target_feature_id")
        for case in cases
        if case.get("feature", {}).get("id") or case.get("target_feature_id")
    }
    counts = {
        "inspection_cases": len(cases),
        "features": len(unique_features),
        "inspection_dimensions": len(dataset.get("inspection_dimensions", [])),
        "rules": len(dataset.get("rule_catalog", [])),
        "requirements": 0,
        "trace_links": 0,
        "test_cases": 0,
        "expected_findings": 0,
    }
    for case in cases:
        counts["requirements"] += len(case.get("requirements", []))
        counts["trace_links"] += len(case.get("trace_links", []))
        counts["test_cases"] += len(case.get("test_cases", []))
        counts["expected_findings"] += len(case.get("expected_findings", []))
    return counts


def _cb_save_credentials(cb_url, username, password):
    backend_post("/codebeamer/pswd/save", {
        "codebeamer_host": cb_url,
        "username": username,
        "pswd": password,
    })


def _cb_set_project(project_id):
    backend_post("/codebeamer/project", {"project_id": project_id})


def _cb_load_credentials():
    try:
        resp = backend_get("/codebeamer/pswd")
        if resp.get("ok"):
            return {
                "cb_url":     resp.get("codebeamer_host", ""),
                "username":   resp.get("username", ""),
                "password":   resp.get("pswd", ""),
                "project_id": resp.get("project_id"),
            }
    except Exception:
        pass
    return {}


def _cb_load_tracker_context(project_id, projects):
    """Returns context dict or None if corpus not available."""
    resp = backend_get("/codebeamer/trackers", params={"project_id": project_id})
    if not resp.get("ok"):
        return None
    project_name = next((p["name"] for p in projects if p["id"] == project_id), str(project_id))
    all_trackers = []
    for t in resp.get("trackers", []):
        t_type = t.get("type") or {}
        type_id = t_type.get("id", -1)
        all_trackers.append({
            "id":        t.get("id"),
            "name":      t.get("name", ""),
            "type_id":   type_id,
            "type_name": t_type.get("name"),
            "color":     t.get("color") or "#6c757d",
            "icon":      TRACKER_ICONS.get(type_id, "label"),
            "key_name":  t.get("keyName"),
        })
    return {
        "selected_project_id":   project_id,
        "selected_project_name": project_name,
        "work_items":   [t for t in all_trackers if (t["type_name"] or "").lower() in _WORK_ITEM_TYPES],
        "config_items": [t for t in all_trackers if (t["type_name"] or "").lower() not in _WORK_ITEM_TYPES],
        "trackers":     all_trackers,
        "has_project_json": backend_get("/codebeamer/item-map", params={"project_id": project_id}).get("ok", False),
    }


def _cb_load_projects(_cb=None):
    from .api import backend_get
    resp = backend_get("/codebeamer/projects")
    if not resp.get("ok"):
        raise ValueError(resp.get("error", "Failed to load projects"))
    return [{"id": p["id"], "name": p["name"]} for p in resp.get("projects", [])]


def codebeamer_status_proxy(request, project_id):
    from django.http import JsonResponse
    try:
        data = backend_get("/codebeamer/status", params={"project_id": project_id})
    except Exception as e:
        data = {"stage": "idle", "error": str(e)}
    return JsonResponse(data)


def codebeamer_local_projects_proxy(request):
    from django.http import JsonResponse
    try:
        data = backend_get("/codebeamer/local-projects")
    except Exception as e:
        data = {"ok": False, "projects": [], "error": str(e)}
    return JsonResponse(data)


def codebeamer_server_projects_proxy(request):
    from django.http import JsonResponse
    try:
        data = backend_get("/codebeamer/projects")
    except Exception as e:
        data = {"ok": False, "projects": [], "error": str(e)}
    return JsonResponse(data)


def codebeamer_pull_view(request):
    if request.method == "POST":
        project_id = request.POST.get("project_id")
        if project_id:
            project_id = int(project_id)
            _cb_set_project(project_id)
        else:
            saved = _cb_load_credentials()
            project_id = saved.get("project_id")
        if project_id:
            try:
                backend_post("/codebeamer/pull", {"project_id": project_id, "workers": 4})
            except Exception:
                pass
    from django.shortcuts import redirect as _redirect
    return _redirect("app_main:codebeamer")


def codebeamer_evaluate(request):
    if request.method == "POST":
        project_id = request.POST.get("project_id")
        if project_id:
            project_id = int(project_id)
        else:
            saved = _cb_load_credentials()
            project_id = saved.get("project_id")
        if project_id:
            try:
                backend_post("/codebeamer/evaluate", {"project_id": project_id, "workers": 4})
            except Exception:
                pass
    from django.shortcuts import redirect as _redirect
    return _redirect("app_main:codebeamer")


def codebeamer_select_project(request):
    from django.http import JsonResponse
    saved = _cb_load_credentials()
    context = {"title": "AI-delix - Select Project", "selected_project_id": saved.get("project_id")}

    if request.method == "POST":
        project_id = int(request.POST.get("project_id", 0))
        if project_id:
            _cb_set_project(project_id)
            from django.shortcuts import redirect as _redirect
            return _redirect("app_main:codebeamer")

    # Local projects from corpus
    resp = backend_get("/codebeamer/local-projects")
    context["local_projects"] = resp.get("projects", [])
    return render(request, "app_main/codebeamer_select_project.html", context)


def codebeamer(request):
    context = {
        "title": "AI-delix - Codebeamer",
        "description": "Codebeamer integration",
    }

    credentials = _cb_load_credentials()

    if request.method == "GET" and credentials:
        try:
            context["projects"] = _cb_load_projects()
            if credentials.get("project_id"):
                tracker_ctx = _cb_load_tracker_context(credentials["project_id"], context["projects"])
                if tracker_ctx is None:
                    from django.shortcuts import redirect as _redirect
                    return _redirect("app_main:codebeamer_select_project")
                context.update(tracker_ctx)
        except Exception as e:
            context["fetch_error"] = str(e)
        context.update({"cb_url": credentials.get("cb_url", ""), "username": credentials.get("username", ""), "password": credentials.get("password", "")})
        return render(request, "app_main/codebeamer.html", context)

    if request.method == "POST":
        action   = request.POST.get("action", "")
        cb_url   = request.POST.get("cb_url", "").strip() or credentials.get("cb_url", "")
        username = request.POST.get("username", "").strip() or credentials.get("username", "")
        password = request.POST.get("password", "").strip() or credentials.get("password", "")

        if action == "change_project":
            from django.shortcuts import redirect as _redirect
            return _redirect("app_main:codebeamer_select_project")

        if action in ("load_projects", "load_trackers"):
            try:
                cb = Codebeamer(cb_url, username, password, api_root='api/v3')

                if action == "load_projects":
                    _cb_save_credentials(cb_url, username, password)
                    context["projects"] = _cb_load_projects()

                elif action == "load_trackers":
                    project_id = int(request.POST.get("project_id", 0))
                    context["projects"] = _cb_load_projects()
                    _cb_set_project(project_id)
                    tracker_ctx = _cb_load_tracker_context(project_id, context["projects"])
                    if tracker_ctx:
                        context.update(tracker_ctx)

            except Exception as e:
                context["fetch_error"] = str(e)

        context.update({"cb_url": cb_url, "username": username, "password": password})

    return render(request, "app_main/codebeamer.html", context)


def _serialize_fields(tracker: Tracker, project_id: int = None):
    pid = project_id or (tracker.project.id if tracker.project else None)
    raw_fields = None
    if pid:
        resp = backend_get(f"/codebeamer/tracker-fields/{tracker.id}", params={"project_id": pid})
        if resp.get("ok"):
            raw_fields = resp.get("fields", [])

    if raw_fields is not None:
        fields = [
            {
                "id":            f.get("id"),
                "name":          f.get("name"),
                "type":          f.get("type"),
                "multiple":      f.get("multipleValues") or False,
                "options":       [{"id": o.get("id"), "name": o.get("name")} for o in (f.get("options") or [])],
                "tracker_field": f.get("trackerItemField"),
            }
            for f in raw_fields if not f.get("hidden")
        ]
        return fields

    # Fallback: live CB API via FieldDefinition (slow — N requests, no corpus available)
    fields = []
    for f in tracker.get_fields():
        if f.hidden:
            continue
        fields.append({
            "id":            f.id,
            "name":          f.name,
            "type":          f._type,
            "multiple":      f.multiple_values or False,
            "options":       [{"id": o.id, "name": o.name} for o in f.options] if f.options else [],
            "tracker_field": f.tracker_item_field,
        })
    return fields


def _serialize_tree(nodes: list, tracker_meta: dict = None) -> list:
    """Convert raw CB tree nodes (from tree.json) to template-ready dicts."""
    result = []
    for node in nodes:
        if node.get("isFolder"):
            result.append({
                "kind":     "folder",
                "name":     node.get("text", ""),
                "children": _serialize_tree(node.get("children") or [], tracker_meta),
            })
        elif node.get("trackerId"):
            tid = node["trackerId"]
            meta = (tracker_meta or {}).get(str(tid), {})
            type_id = meta.get("type_id", -1)
            result.append({
                "kind":       "tracker",
                "id":         tid,
                "name":       meta.get("name") or str(tid),
                "type_id":    type_id,
                "type_name":  meta.get("type_name"),
                "color":      meta.get("color", "#6c757d"),
                "icon":       TRACKER_ICONS.get(type_id, "label"),
                "item_count": len(node.get("items") or []),
            })
    return result


def codebeamer_create_item(request, tracker_id):
    from django.shortcuts import redirect as _redirect

    credentials = _cb_load_credentials()
    if not credentials:
        return _redirect("app_main:codebeamer")

    if request.method == "POST":
        try:
            cb = Codebeamer(credentials["cb_url"], credentials["username"], credentials["password"], api_root="api/v3")
            tracker = cb.get_tracker(tracker_id)
            t_type = tracker.type_name.lower()

            print(f"[create_item] tracker_id={tracker_id} type={t_type}")
            for key, val in request.POST.items():
                if key != "csrfmiddlewaretoken":
                    print(f"  POST[{key}] = {val!r}")

            kwargs = {}

            if t_type == "requirement":
                name        = request.POST.get("name", "").strip()
                description = request.POST.get("description", "").strip()
                type_name   = request.POST.get("type_name", "").strip()
                if request.POST.get("story_points"):
                    kwargs["story_points"] = int(request.POST["story_points"])
                if request.POST.get("business_value"):
                    priority = request.POST["business_value"]
                    id = 0
                    if priority == "Must have":
                        id = 1
                    elif priority == "Should have":
                        id = 3
                    elif priority == "Nice to have":
                        id = 5
                    kwargs["priority"] = {"type": "ChoiceOptionReference", "id": id, "name": priority}
                if request.POST.get("complexity"):
                    complexity = request.POST["complexity"]
                    id = 0
                    if complexity == "Complex":
                        id = 3
                    elif complexity == "Standard":
                        id = 2
                    elif complexity == "Simple":
                        id = 1
                    kwargs["severities"] = [{"type": "ChoiceOptionReference", "id": id, "name": complexity}]

                item = tracker.create_tracker_item(
                    name=name,
                    description=description,
                    type_name=type_name,
                    **kwargs,
                )
            elif t_type == "release":
                name        = request.POST.get("name", "").strip()
                description = request.POST.get("description", "").strip()

                if request.POST.get("start_date"):
                    kwargs["start_date"] = _to_cb_date(request.POST.get("start_date", "").strip())

                if request.POST.get("planned_release_date"):
                    kwargs["end_date"] = _to_cb_date(request.POST.get("planned_release_date", "").strip())

                if request.POST.get("actual_release_date"):
                    kwargs["actual_release_date"] = _to_cb_date(request.POST.get("actual_release_date", "").strip())

                if request.POST.get("release_method", "").strip():
                    release_method_val = request.POST.get("release_method", "").strip()
                    id = 0
                    if release_method_val == "Scrum":
                        id = 1
                    elif release_method_val == "KanBan":
                        id = 2
                    elif release_method_val == "Waterfall":
                        id = 3
                    kwargs["release_method"] = {
                        "type": "ChoiceOptionReference",
                        "id":   id,
                        "name": release_method_val if release_method_val else "Unset",
                    }

                #kwargs["status"] = {"type": "ChoiceOptionReference", "id": 1, "name": "Unset"}

                item = tracker.create_tracker_item(
                    name=name,
                    description=description,
                    type_name="Release",
                    **kwargs,
                )
            elif t_type == "issue":
                name        = request.POST.get("name", "").strip()
                description = request.POST.get("description", "").strip()
                type_name   = request.POST.get("type_name", "").strip()
                severity    = request.POST.get("severity", "").strip()
                priority    = request.POST.get("priority", "").strip()
                resolution   = request.POST.get("resolution", "").strip()
                planned_effort_raw = request.POST.get("planned_effort", "").strip()
                planned_effort = int(planned_effort_raw) if planned_effort_raw.isdigit() else 0

                if planned_effort > 0:
                    kwargs["estimated_millis"] = planned_effort * 3600000  # convert hours to milliseconds

                if request.POST.get("start_date"):
                    kwargs["start_date"] = _to_cb_date(request.POST.get("start_date", "").strip())
                    print("Start date:", kwargs["start_date"])

                if request.POST.get("end_date"):
                    kwargs["end_date"] = _to_cb_date(request.POST.get("end_date", "").strip())
                    print("End date:", kwargs["end_date"])

                release_refs = []
                for ref in request.POST.getlist("release_ref"):
                    parts = ref.split("|", 1)
                    if len(parts) == 2:
                        release_refs.append({
                            "id":   int(parts[0]),
                            "name": parts[1],
                            "type": "TrackerItemReference",
                        })
                if release_refs:
                    kwargs["versions"] = release_refs

                if severity:
                    severity_id = 0
                    if severity == "Blocker":
                        severity_id = 1
                    elif severity == "Critical":
                        severity_id = 2
                    elif severity == "Major":
                        severity_id = 3
                    elif severity == "Minor":
                        severity_id = 4
                    elif severity == "Trivial":
                        severity_id = 5
                    kwargs["severities"] = [{"type": "ChoiceOptionReference", "id": severity_id, "name": severity}]

                if priority:
                    priority_id = 0
                    if priority == "Highest":
                        priority_id = 1
                    elif priority == "High":
                        priority_id = 2
                    elif priority == "Medium":
                        priority_id = 3
                    elif priority == "Low":
                        priority_id = 4
                    elif priority == "Lowest":
                        priority_id = 5
                    kwargs["priority"] = {"type": "ChoiceOptionReference", "id": priority_id, "name": priority}

                if resolution:
                    resolution_id = 0
                    if resolution == "Fixed":
                        resolution_id = 1
                    elif resolution == "Invalid":
                        resolution_id = 2
                    elif resolution == "Won't Fix":
                        resolution_id = 3
                    elif resolution == "Later":
                        resolution_id = 4
                    elif resolution == "Remind":
                        resolution_id = 5
                    elif resolution == "Duplicate":
                        resolution_id = 6
                    elif resolution == "Works for Me":
                        resolution_id = 7
                    elif resolution == "In Development":
                        resolution_id = 8
                    elif resolution == "Implemented":
                        resolution_id = 9
                    elif resolution == "Party Implemented":
                        resolution_id = 10
                    kwargs["resolutions"] = [{"type": "ChoiceOptionReference", "id": resolution_id, "name": resolution}]

                print("::", kwargs)
                item = tracker.create_tracker_item(
                    name=name,
                    description=description,
                    type_name=type_name,
                    **kwargs,
                )
            else:
                name        = request.POST.get("name", "").strip()
                description = request.POST.get("description", "").strip()
                type_name   = request.POST.get("type_name", "").strip()
                item = tracker.create_tracker_item(
                    name=name,
                    description=description,
                    type_name=type_name,
                )

            return _redirect("app_main:codebeamer_tracker", tracker_id=tracker_id)
        except Exception as e:
            pass  # fall through to render error

    return _redirect("app_main:codebeamer_tracker", tracker_id=tracker_id)



def codebeamer_save(request):
    credentials = _cb_load_credentials()
    if not credentials or not credentials.get("project_id"):
        from django.shortcuts import redirect as _redirect
        return _redirect("app_main:codebeamer")
    try:
        backend_post("/codebeamer/save", {"project_id": credentials["project_id"]})
    except Exception:
        pass
    from django.shortcuts import redirect as _redirect
    return _redirect("app_main:codebeamer")


def codebeamer_item(request, item_id):
    credentials = _cb_load_credentials()
    if not credentials:
        from django.shortcuts import redirect as _redirect
        return _redirect("app_main:codebeamer")

    context = {
        "title": f"AI-delix - Item {item_id}",
        "item_id": item_id,
        "cb_url": credentials.get("cb_url", ""),
    }
    try:
        cb = Codebeamer(credentials["cb_url"], credentials["username"], credentials["password"], api_root="api/v3")
        item = cb.get_tracker_item(item_id)

        def _safe(fn):
            try: return fn()
            except: return None

        status_name   = _safe(lambda: item.status.value.name if item.status and item.status.value else None)
        priority_name = _safe(lambda: item.priority.name if item.priority else None)

        tracker = item.tracker
        type_slug = (tracker.type_name or "").lower().replace(" ", "_").replace("/", "_") if tracker else ""

        context["item"] = {
            "id":           item.id,
            "name":         item.name,
            "description":        item.description,
            "description_format": _safe(lambda: item.description_format),
            "type_name":          item.type_name,
            "status":       status_name,
            "status_color": STATUS_COLORS.get(status_name, "#6c757d"),
            "priority":     priority_name,
            "priority_color": PRIORITY_COLORS.get(priority_name, "#adb5bd"),
            "severity":     _safe(lambda: item.severities[0].get("name") if item.severities else None),
            "resolution":   _safe(lambda: item.resolutions[0].get("name") if item.resolutions else None),
            "start_date":   _safe(lambda: str(item.start_date.date()) if item.start_date else None),
            "end_date":     _safe(lambda: str(item.end_date.date()) if item.end_date else None),
            "created_by":   _safe(lambda: item.created_by.name if item.created_by else None),
            "created_at":   _safe(lambda: item.created_at),
            "modified_by":  _safe(lambda: item.modified_by.name if item.modified_by else None),
            "modified_at":  _safe(lambda: item.modified_at),
            "assigned_to":   _safe(lambda: ", ".join(u.name for u in item.assigned_to) if item.assigned_to else None),
            "owners":        _safe(lambda: ", ".join(u.name for u in item.owners) if item.owners else None),
            "parent_id":     _safe(lambda: item.parent.id if item.parent else None),
            "parent_name":   _safe(lambda: item.parent.name if item.parent else None),
            "category":      _safe(lambda: item.categories[0].get("name") if item.categories else None),
            "release":       _safe(lambda: ", ".join(v.get("name","") for v in item.versions) if item.versions else None),
            "versions":      _safe(lambda: [{"id": v.get("id"), "name": v.get("name","")} for v in item.versions] if item.versions else []),
            "subjects":      _safe(lambda: [{"id": s.get("id") if isinstance(s, dict) else getattr(s, "id", None), "name": s.get("name") if isinstance(s, dict) else getattr(s, "name", None)} for s in item.subjects] if item.subjects else []),
            "story_points":  _safe(lambda: item.story_points),
            "planned_millis":_safe(lambda: item.estimated_millis),
            "spent_millis":  _safe(lambda: item.spent_millis),
            "tracker_id":    tracker.id if tracker else None,
            "tracker_name":  tracker.name if tracker else None,
            "tracker_icon":  TRACKER_ICONS.get(tracker.type_id, "label") if tracker else "label",
            "tracker_color": tracker.color or "#6c757d" if tracker else "#6c757d",
        }

        from django.template.loader import get_template
        from django.template import TemplateDoesNotExist
        specific = f"app_main/cb_item_views/{type_slug}.html"
        try:
            get_template(specific)
            context["item_view_template"] = specific
        except TemplateDoesNotExist:
            context["item_view_template"] = None

        # Build traceability trees from saved JSON
        project_id = credentials.get("project_id")
        max_levels = int(request.GET.get("levels", 2))
        context["max_levels"] = max_levels
        context["levels_range"] = range(1, 11)

        # Collect IDs needing enrichment (subjects + versions of this item)
        cur_item = context.get("item") or {}
        enrich_ids = [str(s.get("id")) for s in cur_item.get("subjects", []) if s.get("id")]
        enrich_ids += [str(v.get("id")) for v in cur_item.get("versions", []) if v.get("id")]

        tree_resp = backend_get(f"/codebeamer/tree/{item_id}", params={
            "project_id": project_id,
            "levels": max_levels,
            "enrich_ids": ",".join(enrich_ids),
        })
        if not tree_resp.get("ok"):
            context["no_project_json"] = True
            context["upstream_rows"] = []
            context["downstream_rows"] = []
            context["upstream_cols"] = max_levels
            context["downstream_cols"] = max_levels
        else:
            context["upstream_rows"]   = tree_resp["upstream_rows"]
            context["downstream_rows"] = tree_resp["downstream_rows"]
            context["upstream_cols"]   = tree_resp["cols"]
            context["downstream_cols"] = tree_resp["cols"]

            # Enrich subjects/versions using only the needed entries
            item_tracker_map = {int(k): v for k, v in tree_resp.get("enriched", {}).items()}
            def _enrich(items):
                for i in items:
                    info = item_tracker_map.get(i.get("id"), {"icon": "article", "color": "#6c757d"})
                    i["icon"] = info["icon"]; i["color"] = info["color"]
                return items
            if context.get("item"):
                context["item"]["subjects"] = _enrich(context["item"].get("subjects") or [])
                context["item"]["versions"] = _enrich(context["item"].get("versions") or [])

    except Exception as e:
        context["fetch_error"] = str(e)

    return render(request, "app_main/codebeamer_item.html", context)


def codebeamer_tree(request):
    credentials = _cb_load_credentials()
    if not credentials or not credentials.get("project_id"):
        from django.shortcuts import redirect as _redirect
        return _redirect("app_main:codebeamer")

    project_id = credentials["project_id"]
    context = {
        "title": "AI-delix - Codebeamer Tree",
        "cb_url": credentials.get("cb_url", ""),
    }
    resp = backend_get("/codebeamer/tree-view", params={"project_id": project_id})
    if resp.get("ok"):
        context["project_name"] = resp.get("project_name", "")
        context["tree"] = _serialize_tree(resp.get("tree", []), resp.get("tracker_meta", {}))
    else:
        context["fetch_error"] = resp.get("error", "Tree not available")

    return render(request, "app_main/codebeamer_tree.html", context)


def codebeamer_tracker(request, tracker_id):
    credentials = _cb_load_credentials()
    if not credentials:
        from django.shortcuts import redirect as _redirect
        return _redirect("app_main:codebeamer")

    context = {
        "title": f"AI-delix - Tracker {tracker_id}",
        "tracker_id": tracker_id,
        "cb_url": credentials.get("cb_url", ""),
    }
    try:
        cb = Codebeamer(credentials["cb_url"], credentials["username"], credentials["password"], api_root="api/v3")
        tracker = cb.get_tracker(tracker_id)
        context["tracker"] = {
            "id":          tracker.id,
            "name":        tracker.name,
            "type_id":     tracker.type_id,
            "type_name":   tracker.type_name,
            "description": tracker.description,
            "key_name":    tracker.key_name,
            "version":     tracker.version,
            "created_at":  tracker.created_at,
            "created_by":  tracker.created_by.name if tracker.created_by else None,
            "modified_at": tracker.modified_at,
            "modified_by": tracker.modified_by.name if tracker.modified_by else None,
            "project_id":  tracker.project.id if tracker.project else None,
            "project_name":tracker.project.name if tracker.project else None,
            "icon":        TRACKER_ICONS.get(tracker.type_id, "label"),
        }
        from pybeamer.tracker_item import TrackerItem as _TI
        _proj_id = tracker.project.id if tracker.project else credentials.get("project_id")
        all_fields = _serialize_fields(tracker, project_id=_proj_id)
        # Category field drives the type_name — extract it separately
        category_field = next(
            (f for f in all_fields if f["tracker_field"] in ("categories", "category") or f["name"].lower() in ("category", "categories")),
            None
        )
        if category_field and category_field["options"]:
            # Only keep options that exist in TYPE_MAP
            valid = {v: k for k, v in _TI.TYPE_MAP.items()}
            context["item_type_options"] = [
                {"id": o["id"], "name": o["name"]}
                for o in category_field["options"]
                if o["id"] in valid or o["name"] in _TI.TYPE_MAP
            ] or category_field["options"]
        else:
            context["item_type_options"] = [{"id": None, "name": k} for k in _TI.TYPE_MAP.keys()]
        # Exclude category field from regular form fields
        context["tracker_fields"] = [f for f in all_fields if f != category_field]

        # Load Release items from corpus JSON via FastAPI (no live CB calls)
        project_id = tracker.project.id if tracker.project else credentials.get("project_id")
        release_items = []
        if project_id:
            resp = backend_get("/codebeamer/items-by-type",
                               params={"project_id": project_id, "type_name": "release"})
            release_items = resp.get("items", [])
        context["release_items"] = release_items

        # Pick form template by tracker type_name, fallback to default
        type_slug = (tracker.type_name or "").lower().replace(" ", "_").replace("/", "_")
        from django.template.loader import get_template
        from django.template import TemplateDoesNotExist
        specific = f"app_main/cb_tracker_forms/{type_slug}.html"
        try:
            get_template(specific)
            context["tracker_form_template"] = specific
        except TemplateDoesNotExist:
            context["tracker_form_template"] = "app_main/cb_tracker_forms/default.html"
        items = tracker.get_items()
        context["items_total"] = len(items)
        is_issue = (tracker.type_name or "").lower() == "issue"
        context["is_issue_tracker"] = is_issue

        # item_map for icon + status-color lookup (built during pull)
        _proj_id = tracker.project.id if tracker.project else credentials.get("project_id")
        _item_map_resp = backend_get("/codebeamer/item-map", params={"project_id": _proj_id}) if _proj_id else {}
        _item_map = _item_map_resp.get("item_map", {}) if _item_map_resp.get("ok") else {}
        tracker_icon  = TRACKER_ICONS.get(tracker.type_id, "label")
        tracker_color = tracker.color or "#6c757d"

        def _safe(fn):
            try: return fn()
            except: return None

        def _serialize_item(i):
            entry = _item_map.get(str(i.id)) or _item_map.get(i.id) or {}
            status_name = _safe(lambda: i.status.value.name if i.status and i.status.value else None)
            base = {
                "id":          i.id,
                "name":        i.name,
                "type_name":   i.type_name,
                "icon":        entry.get("icon", tracker_icon),
                "color":       entry.get("color") or STATUS_COLORS.get(status_name, tracker_color),
                "status":      status_name,
                "status_color": STATUS_COLORS.get(status_name, tracker_color),
            }
            if is_issue:
                priority_name = _safe(lambda: i.priority.name if i.priority else None)
                base["priority"]       = priority_name
                base["priority_color"] = PRIORITY_COLORS.get(priority_name, "#adb5bd")
                base["severity"]   = _safe(lambda: i.severities[0].get("name") if i.severities else None)
                base["resolution"] = _safe(lambda: i.resolutions[0].get("name") if i.resolutions else None)
                base["start_date"] = _safe(lambda: str(i.start_date.date()) if i.start_date else None)
                base["end_date"]   = _safe(lambda: str(i.end_date.date()) if i.end_date else None)
            return base

        context["items"] = [_serialize_item(i) for i in items]
    except Exception as e:
        context["fetch_error"] = str(e)

    return render(request, "app_main/codebeamer_tracker.html", context)


def aispice(request):
    with open(_FEATURES_04_DATASET, encoding="utf-8") as f:
        dataset = json.load(f)

    entity_counts = _collect_aspice_counts(dataset)

    # --- POST actions ---
    action = ""
    gap_results = []
    gap_total_errors = 0
    gap_total_warnings = 0
    validation_issues = []
    validation_summary = {}

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action == "detect_gaps":
            for case in dataset.get("inspection_cases", []):
                findings = inspect_case(case)
                errors = [f for f in findings if f["severity"] == "error"]
                warnings = [f for f in findings if f["severity"] == "warning"]
                gap_total_errors += len(errors)
                gap_total_warnings += len(warnings)
                gap_results.append({
                    "case_id": case.get("inspection_case_id"),
                    "feature_name": case.get("feature", {}).get("name"),
                    "findings": findings,
                    "errors": errors,
                    "warnings": warnings,
                })
        elif action == "validate":
            validation_issues = validate_dataset(dataset)
            validation_summary = summarize_issues(validation_issues)

    # --- transform for JSON treeview ---
    cases = {}
    for case in dataset.get("inspection_cases", []):
        cid = case.get("inspection_case_id", case.get("target_feature_id", "unknown"))
        c = dict(case)
        c["input_evidence"] = {e["id"]: e for e in c.get("input_evidence", []) if "id" in e}
        c["requirements"] = {r["id"]: r for r in c.get("requirements", []) if "id" in r}
        c["trace_links"] = {t["id"]: t for t in c.get("trace_links", []) if "id" in t}
        c["test_cases"] = {t["id"]: t for t in c.get("test_cases", []) if "id" in t}
        c["expected_findings"] = {f["id"]: f for f in c.get("expected_findings", []) if "id" in f}
        cases[cid] = c

    rules = {r["rule_id"]: r for r in dataset.get("rule_catalog", []) if "rule_id" in r}

    dataset_out = {k: v for k, v in dataset.items() if k not in ("inspection_cases", "rule_catalog")}
    dataset_out["rule_catalog"] = rules
    dataset_out["inspection_cases"] = cases

    feature_catalog = {}
    for case in dataset.get("inspection_cases", []):
        feat = case.get("feature", {})
        fid = feat.get("id")
        if fid and fid not in feature_catalog:
            feature_catalog[fid] = feat

    return render(request, "app_main/aspice.html", context={
        "title": "AI-delix - ASPICE Compliance Checker",
        "description": "ASPICE compliance checker",
        "dataset_json": json.dumps(dataset_out, ensure_ascii=False),
        "entity_counts": entity_counts,
        "feature_catalog": feature_catalog.values(),
        "action": action,
        "gap_results": gap_results,
        "gap_total_errors": gap_total_errors,
        "gap_total_warnings": gap_total_warnings,
        "validation_issues": validation_issues,
        "validation_summary": validation_summary,
    })


def aspice_process_grid(request):
    """Render ProcessGrid view using local corpus + Django-only processing."""

    def corpus_json_path(project_id: int) -> Path:
        return _CORPUS_ROOT / str(project_id) / "codebeamer.json"

    def scenarios_output_path(project_id: int) -> Path:
        return _CORPUS_ROOT / str(project_id) / ".sync" / "aspice_scenarios.json"

    def load_corpus(project_id: int) -> dict[str, Any] | None:
        path = corpus_json_path(project_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def normalize_tracker_role(tracker_name: str, tracker_key: str) -> str:
        tn = tracker_name.lower()
        tk = tracker_key.lower()
        if "feature" in tn or tk == "feat":
            return "feature"
        if "customer requirement" in tn or "stakeholder requirement" in tn or tk in {"crs", "stk"}:
            return "stakeholder_requirement"
        if "system requirement" in tn or tk == "srs":
            return "system_requirement"
        if "software requirement" in tn:
            return "software_requirement"
        if "test case" in tn or "qualification test" in tn or tk in {"tc", "test", "testcase"}:
            return "test_case"
        return "other"

    def aspice_process_for_role(role: str) -> str:
        return {
            "stakeholder_requirement": "SYS.1",
            "system_requirement": "SYS.2",
            "software_requirement": "SWE.1",
            "test_case": "SYS.5",
        }.get(role, "UNKNOWN")

    def build_records_from_corpus(corpus: dict[str, Any]) -> list[dict[str, Any]]:
        trackers = corpus.get("trackers") or []
        items = corpus.get("items") or []
        if not isinstance(trackers, list) or not isinstance(items, list):
            return []

        trackers_by_id: dict[int, dict[str, Any]] = {}
        for tracker in trackers:
            if isinstance(tracker, dict) and isinstance(tracker.get("id"), int):
                trackers_by_id[tracker["id"]] = tracker

        items_by_id: dict[int, dict[str, Any]] = {}
        upstream_by_id: dict[int, list[int]] = {}
        downstream_by_id: dict[int, list[int]] = {}
        roles_by_id: dict[int, str] = {}

        for item in items:
            if not isinstance(item, dict):
                continue
            iid = item.get("id")
            if not isinstance(iid, int):
                continue
            items_by_id[iid] = item

            upstream: list[int] = []
            for subj in item.get("subjects") or []:
                if not isinstance(subj, dict):
                    continue
                sid = subj.get("id")
                if isinstance(sid, int):
                    upstream.append(sid)
                    downstream_by_id.setdefault(sid, []).append(iid)
            upstream_by_id[iid] = upstream

            tracker_ref = item.get("tracker") or {}
            tracker = trackers_by_id.get(tracker_ref.get("id") if isinstance(tracker_ref, dict) else None, {})
            roles_by_id[iid] = normalize_tracker_role(str(tracker.get("name") or ""), str(tracker.get("keyName") or ""))

        feature_ids = [iid for iid, role in roles_by_id.items() if role == "feature"]
        if not feature_ids:
            feature_ids = [
                iid
                for iid, role in roles_by_id.items()
                if role == "stakeholder_requirement" and not upstream_by_id.get(iid)
            ]

        def collect_descendants(root_id: int) -> set[int]:
            visited: set[int] = set()
            queue: list[int] = [root_id]
            while queue:
                cur = queue.pop(0)
                if cur in visited:
                    continue
                visited.add(cur)
                for nxt in downstream_by_id.get(cur, []):
                    if nxt not in visited:
                        queue.append(nxt)
            return visited

        records: list[dict[str, Any]] = []
        for feature_id in feature_ids:
            feature_item = items_by_id.get(feature_id)
            if not feature_item:
                continue

            cluster = collect_descendants(feature_id)
            cluster.add(feature_id)

            requirements: list[dict[str, Any]] = []
            test_cases: list[dict[str, Any]] = []
            trace_links: list[dict[str, Any]] = []

            for iid in sorted(cluster):
                item = items_by_id.get(iid)
                if not item:
                    continue
                role = roles_by_id.get(iid, "other")
                upstream = [uid for uid in upstream_by_id.get(iid, []) if uid in cluster]

                for uid in upstream:
                    trace_links.append({
                        "id": f"trace-{uid}-{iid}",
                        "from": str(uid),
                        "to": str(iid),
                        "link_type": "derived_by",
                    })

                if role in {"stakeholder_requirement", "system_requirement", "software_requirement"}:
                    requirements.append({
                        "id": str(iid),
                        "feature_id": str(feature_id),
                        "type": role,
                        "aspice_process": aspice_process_for_role(role),
                        "statement": str(item.get("name") or ""),
                        "derived_from": [str(uid) for uid in upstream],
                        "source_evidence": [],
                        "verification_criteria": "",
                        "safety_relevance": "",
                        "review_state": "",
                        "status": str(((item.get("status") or {}).get("name")) if isinstance(item.get("status"), dict) else ""),
                    })

                if role == "test_case":
                    verifies = [
                        str(uid)
                        for uid in upstream
                        if roles_by_id.get(uid) in {"stakeholder_requirement", "system_requirement", "software_requirement"}
                    ]
                    test_cases.append({
                        "id": str(iid),
                        "feature_id": str(feature_id),
                        "type": "test_case",
                        "aspice_process": "SYS.5",
                        "title": str(item.get("name") or ""),
                        "verifies": verifies,
                        "test_steps": [],
                        "expected_result": "",
                    })

            feature_status = ""
            if isinstance(feature_item.get("status"), dict):
                feature_status = str(feature_item.get("status", {}).get("name") or "")

            records.append({
                "sample_id": f"SCN-FEAT-{feature_id}",
                "scenario_type": "feature_lifecycle",
                "feature": {
                    "id": str(feature_id),
                    "name": str(feature_item.get("name") or f"Feature {feature_id}"),
                    "description": str(feature_item.get("description") or ""),
                    "status": feature_status,
                },
                "input_evidence": [],
                "input_requirements": requirements,
                "input_trace_links": trace_links,
                "input_test_cases": test_cases,
                "expected_inspection_findings": [],
            })
        return records

    def analyze_tests_from_corpus(corpus: dict[str, Any]) -> dict[str, Any]:
        trackers = corpus.get("trackers") or []
        items = corpus.get("items") or []
        if not isinstance(trackers, list) or not isinstance(items, list):
            return {
                "totals": {
                    "tests": 0,
                    "requirements": 0,
                    "tests_with_links": 0,
                    "tests_without_links": 0,
                    "requirements_with_tests": 0,
                    "requirements_without_tests": 0,
                },
                "test_trackers": [],
                "orphan_tests": [],
                "uncovered_requirements": [],
            }

        trackers_by_id: dict[int, dict[str, Any]] = {
            t["id"]: t for t in trackers if isinstance(t, dict) and isinstance(t.get("id"), int)
        }
        item_by_id: dict[int, dict[str, Any]] = {}
        role_by_item: dict[int, str] = {}
        test_tracker_counts: dict[str, int] = {}

        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), int):
                continue
            iid = item["id"]
            item_by_id[iid] = item
            tracker_ref = item.get("tracker") or {}
            tracker_id = tracker_ref.get("id") if isinstance(tracker_ref, dict) else None
            tracker = trackers_by_id.get(tracker_id, {})
            role = normalize_tracker_role(str(tracker.get("name") or ""), str(tracker.get("keyName") or ""))
            role_by_item[iid] = role
            if role == "test_case":
                key = str(tracker.get("name") or f"tracker-{tracker_id}")
                test_tracker_counts[key] = test_tracker_counts.get(key, 0) + 1

        req_roles = {"stakeholder_requirement", "system_requirement", "software_requirement"}
        requirement_ids = {iid for iid, role in role_by_item.items() if role in req_roles}
        test_ids = [iid for iid, role in role_by_item.items() if role == "test_case"]

        req_to_tests: dict[int, list[int]] = {}
        orphan_tests: list[dict[str, Any]] = []
        tests_with_links = 0

        for tid in test_ids:
            test_item = item_by_id.get(tid, {})
            upstream_ids = [
                subj.get("id")
                for subj in (test_item.get("subjects") or [])
                if isinstance(subj, dict) and isinstance(subj.get("id"), int)
            ]
            verified_reqs = [rid for rid in upstream_ids if rid in requirement_ids]
            if verified_reqs:
                tests_with_links += 1
                for rid in verified_reqs:
                    req_to_tests.setdefault(rid, []).append(tid)
            else:
                orphan_tests.append({"id": str(tid), "name": str(test_item.get("name") or "")})

        uncovered_requirements = []
        for rid in sorted(requirement_ids):
            if req_to_tests.get(rid):
                continue
            req_item = item_by_id.get(rid, {})
            uncovered_requirements.append({
                "id": str(rid),
                "name": str(req_item.get("name") or ""),
                "type": role_by_item.get(rid, ""),
            })

        return {
            "totals": {
                "tests": len(test_ids),
                "requirements": len(requirement_ids),
                "tests_with_links": tests_with_links,
                "tests_without_links": len(test_ids) - tests_with_links,
                "requirements_with_tests": len(requirement_ids) - len(uncovered_requirements),
                "requirements_without_tests": len(uncovered_requirements),
            },
            "test_trackers": [
                {"name": name, "count": count}
                for name, count in sorted(test_tracker_counts.items(), key=lambda x: (-x[1], x[0]))
            ],
            "orphan_tests": orphan_tests[:30],
            "uncovered_requirements": uncovered_requirements[:30],
        }

    action = ""
    build_result = None
    fetch_error = ""
    test_analysis = None
    test_analysis_error = ""

    selected_project_id = request.GET.get("project_id", "").strip()
    selected_project_name = ""
    project_id_int = int(selected_project_id) if selected_project_id.isdigit() else None

    corpus = load_corpus(project_id_int) if project_id_int is not None else None
    if corpus and not selected_project_name:
        selected_project_name = str(corpus.get("project_name") or "")

    if request.method == "POST":
        action = request.POST.get("action", "").strip()
        selected_project_id = request.POST.get("project_id", "").strip()
        project_id_int = int(selected_project_id) if selected_project_id.isdigit() else None
        corpus = load_corpus(project_id_int) if project_id_int is not None else None
        if corpus:
            selected_project_name = str(corpus.get("project_name") or "")

        if action == "build":
            if project_id_int is None:
                fetch_error = "Project ID is required"
            elif corpus is None:
                fetch_error = f"Corpus not found: {corpus_json_path(project_id_int)}"
            else:
                try:
                    from .aspice_scenario_builder import build_scenario_records

                    records = build_records_from_corpus(corpus)
                    scenarios_built = build_scenario_records(records)
                    out = scenarios_output_path(project_id_int)
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps(scenarios_built, ensure_ascii=False, indent=2), encoding="utf-8")
                    build_result = {
                        "ok": True,
                        "project_id": project_id_int,
                        "corpus_path": str(corpus_json_path(project_id_int)),
                        "output_path": str(out),
                        "records_count": len(records),
                        "count": len(scenarios_built),
                    }
                except Exception as exc:
                    fetch_error = f"Build failed: {exc}"

    scenarios = []
    if project_id_int is not None:
        out = scenarios_output_path(project_id_int)
        if out.exists():
            try:
                loaded = json.loads(out.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    scenarios = loaded
                else:
                    fetch_error = fetch_error or "Scenario file has invalid structure"
            except Exception as exc:
                fetch_error = fetch_error or f"Failed to read scenarios: {exc}"
        elif not fetch_error:
            fetch_error = "Scenario records are not available yet. Run build first."

    if project_id_int is not None and corpus is not None:
        try:
            test_analysis = analyze_tests_from_corpus(corpus)
        except Exception as exc:
            test_analysis_error = str(exc)

    scenario_type_filter = request.GET.get("scenario_type", "").strip()
    feature_id_filter = request.GET.get("feature_id", "").strip()
    severity_filter = request.GET.get("severity", "").strip().lower()
    gap_rule_filter = request.GET.get("gap_rule", "").strip()
    aspice_process_filter = request.GET.get("aspice_process", "").strip().upper()

    available_scenario_types = sorted({str(s.get("scenario_type") or "") for s in scenarios if s.get("scenario_type")})

    filtered_scenarios = scenarios
    if scenario_type_filter:
        filtered_scenarios = [
            s for s in filtered_scenarios
            if str(s.get("scenario_type") or "") == scenario_type_filter
        ]

    scenarios_after_type_filter = list(filtered_scenarios)

    feature_summary_map: dict[str, dict[str, Any]] = {}
    for scenario in filtered_scenarios:
        feature_id = str(scenario.get("feature_id") or "").strip()
        if not feature_id:
            continue
        feature_name = str(scenario.get("feature_name") or feature_id)
        row = feature_summary_map.setdefault(feature_id, {
            "feature_id": feature_id,
            "feature_name": feature_name,
            "scenario_count": 0,
            "error_count": 0,
            "warning_count": 0,
        })
        row["scenario_count"] += 1
        for gap in scenario.get("gaps", []) or []:
            sev = str(gap.get("severity") or "").lower()
            if sev == "error":
                row["error_count"] += 1
            elif sev == "warning":
                row["warning_count"] += 1

    available_features = sorted(
        feature_summary_map.values(),
        key=lambda x: (str(x.get("feature_name") or "").lower(), str(x.get("feature_id") or "")),
    )

    if feature_id_filter:
        filtered_scenarios = [
            s for s in filtered_scenarios
            if str(s.get("feature_id") or "") == feature_id_filter
        ]

    selected_scenario_id = request.GET.get("scenario_id", "").strip()
    selected = None

    if selected_scenario_id:
        selected = next((s for s in filtered_scenarios if str(s.get("scenario_id")) == selected_scenario_id), None)
    if selected is None and filtered_scenarios:
        selected = filtered_scenarios[0]

    lifecycle_pipeline = []
    if selected:
        reqs_by_type: dict[str, list[dict[str, Any]]] = {}
        for req in selected.get("requirements", []) or []:
            rtype = str(req.get("type") or "unknown")
            reqs_by_type.setdefault(rtype, []).append(req)

        lifecycle_pipeline = [
            {
                "stage_id": "feature",
                "label": "Feature",
                "process": "",
                "filter_process": "",
                "color": "purple",
                "item_type": "feature",
                "items": [{
                    "id": selected.get("feature_id"),
                    "name": selected.get("feature_name"),
                    "description": "",
                }],
            },
            {
                "stage_id": "sys1",
                "label": "SYS.1",
                "process": "Stakeholder Requirements",
                "filter_process": "SYS.1",
                "color": "success",
                "item_type": "requirement",
                "items": reqs_by_type.get("stakeholder_requirement", []),
            },
            {
                "stage_id": "sys2",
                "label": "SYS.2",
                "process": "System Requirements Analysis",
                "filter_process": "SYS.2",
                "color": "primary",
                "item_type": "requirement",
                "items": reqs_by_type.get("system_requirement", []),
            },
            {
                "stage_id": "swe1",
                "label": "SWE.1",
                "process": "Software Requirements",
                "filter_process": "SWE.1",
                "color": "primary",
                "item_type": "requirement",
                "items": reqs_by_type.get("software_requirement", []),
            },
            {
                "stage_id": "test",
                "label": "SYS.5",
                "process": "Test Cases",
                "filter_process": "SYS.5",
                "color": "warning",
                "item_type": "test_case",
                "items": selected.get("test_cases", []) or [],
            },
            {
                "stage_id": "findings",
                "label": "Findings",
                "process": "Inspection Findings",
                "filter_process": "",
                "color": "danger",
                "item_type": "finding",
                "items": selected.get("gaps", []) or [],
            },
        ]

    selected_feature_id = feature_id_filter or (str(selected.get("feature_id") or "") if selected else "")
    selected_feature_summary = None
    if selected_feature_id:
        feature_scenarios = [
            s for s in scenarios_after_type_filter
            if str(s.get("feature_id") or "") == selected_feature_id
        ]
        if feature_scenarios:
            req_ids: set[str] = set()
            test_ids: set[str] = set()
            trace_ids: set[str] = set()
            errors = 0
            warnings = 0
            infos = 0
            recommendations_set: set[str] = set()
            gap_rule_counts: dict[str, int] = {}
            feature_name = str(feature_scenarios[0].get("feature_name") or selected_feature_id)

            for fs in feature_scenarios:
                for req in fs.get("requirements", []) or []:
                    rid = str(req.get("id") or "").strip()
                    if rid:
                        req_ids.add(rid)

                for tc in fs.get("test_cases", []) or []:
                    tid = str(tc.get("id") or "").strip()
                    if tid:
                        test_ids.add(tid)

                for tr in fs.get("trace_links", []) or []:
                    tid = str(tr.get("id") or "").strip()
                    if tid:
                        trace_ids.add(tid)

                for gap in fs.get("gaps", []) or []:
                    sev = str(gap.get("severity") or "").lower()
                    if sev == "error":
                        errors += 1
                    elif sev == "warning":
                        warnings += 1
                    elif sev == "info":
                        infos += 1

                    rule_id = str(gap.get("rule_id") or "").strip()
                    if rule_id:
                        gap_rule_counts[rule_id] = gap_rule_counts.get(rule_id, 0) + 1

                    rec = str(gap.get("recommendation") or "").strip()
                    if rec:
                        recommendations_set.add(rec)

            selected_feature_summary = {
                "feature_id": selected_feature_id,
                "feature_name": feature_name,
                "scenario_count": len(feature_scenarios),
                "requirements_count": len(req_ids),
                "tests_count": len(test_ids),
                "trace_links_count": len(trace_ids),
                "errors": errors,
                "warnings": warnings,
                "infos": infos,
                "top_gap_rules": [
                    {"rule_id": rid, "count": cnt}
                    for rid, cnt in sorted(gap_rule_counts.items(), key=lambda x: (-x[1], x[0]))[:5]
                ],
                "recommendations": sorted(recommendations_set),
            }

    process_steps = selected.get("process_steps", []) if selected else []
    if aspice_process_filter:
        process_steps = [
            step for step in process_steps
            if str(step.get("aspice_process") or "").upper() == aspice_process_filter
        ]

    available_aspice_processes = sorted({
        str(step.get("aspice_process") or "").upper()
        for s in filtered_scenarios
        for step in s.get("process_steps", [])
        if step.get("aspice_process")
    })

    gaps = selected.get("gaps", []) if selected else []
    if severity_filter in {"error", "warning", "info"}:
        gaps = [g for g in gaps if str(g.get("severity") or "").lower() == severity_filter]
    if gap_rule_filter:
        gaps = [g for g in gaps if str(g.get("rule_id") or "") == gap_rule_filter]
    if aspice_process_filter:
        active_rules = {
            str(rule)
            for step in process_steps
            for rule in (step.get("validation_rule") or [])
            if str(rule).strip()
        }
        if active_rules:
            gaps = [g for g in gaps if str(g.get("rule_id") or "") in active_rules]

    recommendations = []
    seen_recommendations = set()
    for g in gaps:
        rec = str(g.get("recommendation") or "").strip()
        if not rec or rec in seen_recommendations:
            continue
        seen_recommendations.add(rec)
        recommendations.append(rec)

    if not recommendations and selected:
        recommendations = selected.get("recommendations", []) or []

    export_format = request.GET.get("export", "").strip().lower()
    if export_format == "json" and selected:
        from django.http import JsonResponse
        return JsonResponse(selected, json_dumps_params={"ensure_ascii": False, "indent": 2})

    return render(request, "app_main/aspice_process_grid.html", context={
        "title": "AI-delix - ASPICE ProcessGrid",
        "description": "Scenario-based ASPICE lifecycle process grid",
        "action": action,
        "build_result": build_result,
        "fetch_error": fetch_error,
        "selected_project_id": selected_project_id,
        "selected_project_name": selected_project_name,
        "test_analysis": test_analysis,
        "test_analysis_error": test_analysis_error,
        "scenario_count": len(filtered_scenarios),
        "scenarios": filtered_scenarios,
        "raw_scenario_count": len(scenarios),
        "selected": selected,
        "lifecycle_pipeline": lifecycle_pipeline,
        "selected_feature_summary": selected_feature_summary,
        "process_steps": process_steps,
        "gaps": gaps,
        "recommendations": recommendations,
        "scenario_type_filter": scenario_type_filter,
        "feature_id_filter": feature_id_filter,
        "severity_filter": severity_filter,
        "gap_rule_filter": gap_rule_filter,
        "aspice_process_filter": aspice_process_filter,
        "available_scenario_types": available_scenario_types,
        "available_features": available_features,
        "available_aspice_processes": available_aspice_processes,
    })


def lifecycle_scenario(request, feature_id):
    with open(_FEATURES_04_DATASET, encoding="utf-8") as f:
        dataset = json.load(f)

    case = next(
        (c for c in dataset.get("inspection_cases", [])
         if c.get("feature", {}).get("id") == feature_id),
        None,
    )

    pipeline = []
    if case:
        reqs_by_type = {}
        for req in case.get("requirements", []):
            rtype = req.get("type", "unknown")
            reqs_by_type.setdefault(rtype, []).append(req)

        pipeline = [
            {"stage_id": "feature",   "label": "Feature",  "process": "",                              "color": "purple",    "item_type": "feature",      "items": [case.get("feature", {})]},
            {"stage_id": "evidence",  "label": "Evidence", "process": "Customer Needs",                "color": "secondary", "item_type": "evidence",     "items": case.get("input_evidence", [])},
            {"stage_id": "sys1",      "label": "SYS.1",    "process": "Stakeholder Requirements",      "color": "success",   "item_type": "requirement",  "items": reqs_by_type.get("stakeholder_requirement", [])},
            {"stage_id": "sys2",      "label": "SYS.2",    "process": "System Requirements Analysis",  "color": "primary",   "item_type": "requirement",  "items": reqs_by_type.get("system_requirement", [])},
            {"stage_id": "sys3",      "label": "SYS.3",    "process": "System Architectural Design",   "color": "info",      "item_type": "requirement",  "items": []},
            {"stage_id": "swe1",      "label": "SWE.1",    "process": "Software Requirements",         "color": "primary",   "item_type": "requirement",  "items": reqs_by_type.get("software_requirement", [])},
            {"stage_id": "swe2",      "label": "SWE.2",    "process": "SW Architectural Design",       "color": "info",      "item_type": "requirement",  "items": []},
            {"stage_id": "swe3",      "label": "SWE.3",    "process": "SW Detailed Design",            "color": "info",      "item_type": "requirement",  "items": []},
            {"stage_id": "test",      "label": "SYS.5",    "process": "Test Cases",                    "color": "warning",   "item_type": "test_case",    "items": case.get("test_cases", [])},
            {"stage_id": "findings",  "label": "Findings", "process": "Inspection Findings",           "color": "danger",    "item_type": "finding",      "items": case.get("expected_findings", [])},
        ]

    return render(request, "app_main/lifecycle_scenario.html", context={
        "title": f"AI-delix — Lifecycle: {feature_id}",
        "description": "Feature lifecycle scenario",
        "feature_id": feature_id,
        "case": case,
        "pipeline": pipeline,
    })


def feature_timeline(request, feature_id):
    with open(_FEATURES_04_DATASET, encoding="utf-8") as f:
        dataset = json.load(f)

    case = next(
        (c for c in dataset.get("inspection_cases", [])
         if c.get("feature", {}).get("id") == feature_id),
        None,
    )

    customer_seeds = []
    view_layers = []
    code_evidence = []
    doc_evidence = []

    if case:
        reqs_by_type = {}
        for req in case.get("requirements", []):
            reqs_by_type.setdefault(req.get("type", "unknown"), []).append(req)

        customer_seeds = reqs_by_type.get("stakeholder_requirement", [])

        sys_reqs = reqs_by_type.get("system_requirement", [])
        swe_reqs = reqs_by_type.get("software_requirement", [])
        view_layers = [
            {"label": "SYS.2 — System Requirements Analysis",  "short": "sys2", "count": len(sys_reqs), "requirements": sys_reqs},
            {"label": "SYS.3 — System Architectural Design",   "short": "sys3", "count": 0,             "requirements": []},
            {"label": "SWE.1 — Software Requirements Analysis","short": "swe1", "count": len(swe_reqs), "requirements": swe_reqs},
            {"label": "SWE.2 — Software Architectural Design", "short": "swe2", "count": 0,             "requirements": []},
            {"label": "SWE.3 — Software Detailed Design",      "short": "swe3", "count": 0,             "requirements": []},
        ]

        for ev in case.get("input_evidence", []):
            if ev.get("type") == "code":
                code_evidence.append(ev)
            else:
                doc_evidence.append(ev)

    return render(request, "app_main/feature_timeline.html", context={
        "title": f"AI-delix — Timeline: {feature_id}",
        "description": "Feature timeline",
        "feature_id": feature_id,
        "case": case,
        "feature": case.get("feature", {}) if case else {},
        "customer_seeds": customer_seeds,
        "behaviors": [],
        "owner_elements": [],
        "total_reqs": len(case.get("requirements", [])) if case else 0,
        "evidence_count": len(case.get("input_evidence", [])) if case else 0,
        "code_evidence": code_evidence,
        "doc_evidence": doc_evidence,
        "view_layers": view_layers,
        "inspection_findings": case.get("expected_findings", []) if case else [],
    })

