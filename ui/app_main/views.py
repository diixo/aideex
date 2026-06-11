import json
import os
import re

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


def codebeamer_behavior_scenario_proxy(request):
    from django.http import JsonResponse
    project_id = request.GET.get("project_id")
    item_id = request.GET.get("item_id")
    try:
        data = backend_get(
            "/codebeamer/assessment/behavior-scenario",
            params={"project_id": project_id, "item_id": item_id},
        )
    except Exception as e:
        data = {"ok": False, "scenario": {}, "error": str(e)}
    return JsonResponse(data)


def codebeamer_corpus_load_status_proxy(request):
    from django.http import JsonResponse
    credentials = _cb_load_credentials()
    project_id = (credentials or {}).get("project_id")
    if not project_id:
        return JsonResponse({"stage": "missing", "file_size_mb": 0, "items": 0})
    try:
        data = backend_get("/codebeamer/corpus/load-status", params={"project_id": project_id})
    except Exception as e:
        data = {"stage": "error", "error": str(e)}
    return JsonResponse(data)


def codebeamer_classifier_status_proxy(request):
    from django.http import JsonResponse
    project_id = request.GET.get("project_id")
    try:
        data = backend_get("/codebeamer/assessment/classifier-status", params={"project_id": project_id})
    except Exception as e:
        data = {"status": "error", "error": str(e)}
    return JsonResponse(data)


def codebeamer_assessment_ai_analysis_proxy(request):
    from django.http import JsonResponse
    project_id = request.GET.get("project_id")
    include_security = request.GET.get("include_security", "1")
    try:
        data = backend_get(
            "/codebeamer/assessment/ai-analysis",
            params={
                "project_id": project_id,
                "include_security": "true" if include_security == "1" else "false",
            },
        )
    except Exception as e:
        data = {"ok": False, "analysis": "", "error": str(e)}
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
        project_id = credentials.get("project_id")

        if project_id:
            # Always fire corpus preload immediately (non-blocking)
            import threading as _threading
            _threading.Thread(
                target=backend_get,
                args=("/codebeamer/corpus/preload",),
                kwargs={"params": {"project_id": project_id}},
                daemon=True,
            ).start()

            if request.GET.get("_loaded") == "1":
                # Corpus is in memory — load full tracker context (fast)
                try:
                    context["projects"] = _cb_load_projects()
                    tracker_ctx = _cb_load_tracker_context(project_id, context["projects"])
                    if tracker_ctx is None:
                        from django.shortcuts import redirect as _redirect
                        return _redirect("app_main:codebeamer_select_project")
                    context.update(tracker_ctx)
                except Exception as e:
                    context["fetch_error"] = str(e)
            else:
                # Render skeleton immediately — corpus loads in background
                context["selected_project_id"] = project_id
                context["selected_project_name"] = str(project_id)
                context["corpus_loading"] = True
                context["projects"] = []  # suppress Step 1/2 forms
        else:
            try:
                context["projects"] = _cb_load_projects()
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


def _serialize_fields(tracker: Tracker = None, project_id: int = None, tracker_id: int = None):
    tid = tracker_id or (tracker.id if tracker else None)
    pid = project_id or (tracker.project.id if tracker and tracker.project else None)
    raw_fields = None
    if tid and pid:
        resp = backend_get(f"/codebeamer/tracker-fields/{tid}", params={"project_id": pid})
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
        "gap_code": request.GET.get("gap", ""),
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


def codebeamer_assessment(request):
    credentials = _cb_load_credentials()
    if not credentials or not credentials.get("project_id"):
        from django.shortcuts import redirect as _redirect
        return _redirect("app_main:codebeamer_select_project")

    project_id = int(request.GET.get("project_id") or credentials.get("project_id"))
    if request.GET:
        include_security = "include_security" in request.GET
    else:
        include_security = True

    context = {
        "title": "AI-delix - Codebeamer ASPICE Assessment",
        "description": "Codebeamer ASPICE test assessment and traceability",
        "project_id": project_id,
        "include_security": include_security,
        "summary": {},
        "process_breakdown": [],
        "gaps": [],
        "gaps_total": 0,
        "test_trackers": [],
        "test_items_total": 0,
        "mermaid_graph": "",
        "behavior_mermaid_graph": "",
        "full_chain_mermaid_graph": "",
        "behavior_summary": {},
        "behavior_total": 0,
        "behavior_feature_summary": [],
        "sample_req_items": [],
        "cb_host": "",
    }

    try:
        resp = backend_get(
            "/codebeamer/assessment/test-coverage",
            params={
                "project_id": project_id,
                "include_security": "true" if include_security else "false",
            },
        )
        if not resp.get("ok"):
            context["fetch_error"] = resp.get("error", "Assessment data is unavailable")
            return render(request, "app_main/codebeamer_assessment.html", context)

        context["project_name"] = resp.get("project_name", str(project_id))
        context["summary"] = resp.get("summary") or {}
        context["process_breakdown"] = resp.get("process_breakdown") or []
        all_gaps = resp.get("gaps") or []
        context["gaps"] = all_gaps[:100]
        context["gaps_total"] = len(all_gaps)
        context["test_trackers"] = resp.get("test_trackers") or []
        context["test_items_total"] = sum(t.get("item_refs", 0) for t in context["test_trackers"])
        context["behavior_summary"] = resp.get("behavior_summary") or {}
        context["behavior_total"] = sum((resp.get("behavior_summary") or {}).values())
        feature_summary = resp.get("behavior_feature_summary") or {}
        context["behavior_feature_summary"] = [
            {"feature": k, "counts": v}
            for k, v in list(feature_summary.items())[:80]
        ]
        # Expose first 10 uncovered requirements for the behavior-scenario widget
        _entity_names = {
            e["entity_id"]: e.get("name", "") for e in (resp.get("entities") or [])
        }
        context["sample_req_items"] = [
            {"id": int(g["target_id"]), "name": _entity_names.get(g["target_id"], g["target_id"])}
            for g in (resp.get("gaps") or [])
            if g.get("gap_code") == "REQ_NO_TESTCASE"
        ][:10]

        def _mid(s):
            """Sanitize a string to a valid Mermaid node ID (alphanumeric + underscore only)."""
            return re.sub(r'[^A-Za-z0-9_]', '_', s)

        # Build compact Mermaid graph for requirement -> testcase -> testrun links.
        cb_host = (resp.get("cb_host") or "").rstrip("/")
        context["cb_host"] = cb_host
        graph = resp.get("graph") or {}
        nodes = {n.get("id"): n for n in (graph.get("nodes") or []) if n.get("id")}
        edges = graph.get("edges") or []
        edge_lines = []
        click_lines = []
        seen_node_ids = set()
        for e in edges[:200]:
            src = e.get("from")
            dst = e.get("to")
            if not src or not dst or src not in nodes or dst not in nodes:
                continue
            src_label = nodes[src].get("label", src).replace('"', "'")
            dst_label = nodes[dst].get("label", dst).replace('"', "'")
            edge_lines.append(f'{_mid(src)}["{src_label}"] --> {_mid(dst)}["{dst_label}"]')
            for nid in (src, dst):
                if nid not in seen_node_ids:
                    seen_node_ids.add(nid)
                    item_id = nodes[nid].get("item_id")
                    if cb_host and item_id:
                        url = f"{cb_host}/item/{item_id}"
                        click_lines.append(f'click {_mid(nid)} "{url}" "_blank"')

        if edge_lines:
            context["mermaid_graph"] = "graph LR\n" + "\n".join(edge_lines) + (
                "\n" + "\n".join(click_lines) if click_lines else ""
            )

        # Build behavior graph for Feature Behavior view.
        b_graph = resp.get("behavior_graph") or {}
        b_nodes = {n.get("id"): n for n in (b_graph.get("nodes") or []) if n.get("id")}
        b_edges = b_graph.get("edges") or []
        b_edge_lines = []
        for e in b_edges[:260]:
            src = e.get("from")
            dst = e.get("to")
            if not src or not dst or src not in b_nodes or dst not in b_nodes:
                continue
            src_label = str(b_nodes[src].get("label", src)).replace('"', "'")
            dst_label = str(b_nodes[dst].get("label", dst)).replace('"', "'")
            b_edge_lines.append(f'{_mid(src)}["{src_label}"] --> {_mid(dst)}["{dst_label}"]')

        if b_edge_lines:
            context["behavior_mermaid_graph"] = "graph LR\n" + "\n".join(b_edge_lines)

        # Build full upstream chain graph: TestRun → TestCase → Req → … → Stakeholder (TD)
        _TYPE_STYLE = {
            "testrun":      ("TR", "run"),
            "testcase":     ("TC", "tc"),
            "requirement":  ("R",  "req"),
            "system_req":   ("SR", "sreq"),
            "software_req": ("SW", "swreq"),
            "stakeholder":  ("SH", "stakeholder"),
        }
        c_graph = resp.get("full_chain_graph") or {}
        c_nodes = {n["id"]: n for n in (c_graph.get("nodes") or []) if n.get("id")}
        c_edges = c_graph.get("edges") or []
        c_lines = []
        c_class_assignments: dict[str, list[str]] = {}
        for nid, n in c_nodes.items():
            ntype = n.get("type", "unknown")
            prefix, cls = _TYPE_STYLE.get(ntype, ("N", "req"))
            safe_id = _mid(nid)
            label = str(n.get("label", nid)).replace('"', "'")
            c_lines.append(f'  {safe_id}["{prefix}: {label}"]')
            c_class_assignments.setdefault(cls, []).append(safe_id)
        for e in c_edges:
            s, d = e.get("from"), e.get("to")
            if s and d and s in c_nodes and d in c_nodes:
                c_lines.append(f"  {_mid(s)} --> {_mid(d)}")
        if c_lines:
            header = "graph TD\n"
            class_defs = (
                "  classDef stakeholder fill:#fff3cd,stroke:#856404,color:#000\n"
                "  classDef sreq fill:#cfe2ff,stroke:#084298,color:#000\n"
                "  classDef swreq fill:#d1ecf1,stroke:#0c5460,color:#000\n"
                "  classDef req fill:#e2e3e5,stroke:#383d41,color:#000\n"
                "  classDef tc fill:#d1e7dd,stroke:#0f5132,color:#000\n"
                "  classDef run fill:#f8d7da,stroke:#842029,color:#000\n"
            )
            class_assigns = "\n".join(
                f"  class {','.join(ids)} {cls}"
                for cls, ids in c_class_assignments.items()
            )
            context["full_chain_mermaid_graph"] = (
                header + class_defs + "\n".join(c_lines) + "\n" + class_assigns
            )

    except Exception as e:
        context["fetch_error"] = str(e)

    return render(request, "app_main/codebeamer_assessment.html", context)


def codebeamer_assessment_gaps_download(request):
    from django.http import HttpResponse
    credentials = _cb_load_credentials()
    if not credentials or not credentials.get("project_id"):
        return HttpResponse("No project selected", status=400)
    project_id = int(credentials["project_id"])
    include_security = request.GET.get("include_security", "1") == "1"
    try:
        resp = backend_get(
            "/codebeamer/assessment/test-coverage",
            params={
                "project_id": project_id,
                "include_security": "true" if include_security else "false",
            },
        )
    except Exception as e:
        return HttpResponse(f"Error: {e}", status=500)

    gaps = resp.get("gaps") or []
    project_name = resp.get("project_name", str(project_id))
    cb_host = (resp.get("cb_host") or "").rstrip("/")

    rows = ""
    for g in gaps:
        target_id = g.get("target_id", "")
        target_name = g.get("target_name", target_id)
        link = f'{cb_host}/item/{target_id}' if cb_host and target_id else "#"
        severity = g.get("severity", "")
        gap_code = g.get("gap_code", "")
        process = g.get("process_area", "")
        message = g.get("message", "")
        badge_color = "danger" if severity == "error" else "warning"
        rows += (
            f'<tr>'
            f'<td><a href="{link}" target="_blank"><b>{target_name}</b></a></td>'
            f'<td><span class="badge bg-{badge_color}">{severity}</span></td>'
            f'<td><code>{gap_code}</code></td>'
            f'<td>{process}</td>'
            f'<td>{message}</td>'
            f'</tr>\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Detected Gaps — {project_name}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
</head>
<body class="p-4">
<h4>Detected Gaps — {project_name}</h4>
<p class="text-muted">Total: {len(gaps)}</p>
<table class="table table-sm table-bordered table-striped">
<thead class="table-light">
<tr><th>Target</th><th>Severity</th><th>Gap Code</th><th>Process</th><th>Message</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>"""

    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="gaps_{project_id}.html"'
    return response


def codebeamer_assessment_coverage_download(request):
    from django.http import HttpResponse
    credentials = _cb_load_credentials()
    if not credentials or not credentials.get("project_id"):
        return HttpResponse("No project selected", status=400)
    project_id = int(credentials["project_id"])
    include_security = request.GET.get("include_security", "1") == "1"
    try:
        resp = backend_get(
            "/codebeamer/assessment/test-coverage",
            params={
                "project_id": project_id,
                "include_security": "true" if include_security else "false",
            },
        )
    except Exception as e:
        return HttpResponse(f"Error: {e}", status=500)

    breakdown = resp.get("process_breakdown") or []
    project_name = resp.get("project_name", str(project_id))

    rows = ""
    for p in breakdown:
        area = p.get("process_area", "")
        reqs_total = p.get("requirements_total", 0)
        reqs_with_tests = p.get("requirements_with_tests", 0)
        tests_total = p.get("tests_total", 0)
        runs_total = p.get("runs_total", 0)
        runs_passed = p.get("runs_passed", 0)
        pass_rate = p.get("pass_rate", 0)
        coverage_pct = round(reqs_with_tests / reqs_total * 100, 1) if reqs_total else 0
        rows += (
            f'<tr>'
            f'<td><b>{area}</b></td>'
            f'<td>{reqs_with_tests}/{reqs_total}</td>'
            f'<td>{coverage_pct}%</td>'
            f'<td>{tests_total}</td>'
            f'<td>{runs_total}</td>'
            f'<td>{runs_passed}</td>'
            f'<td>{pass_rate:.4f}</td>'
            f'</tr>\n'
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Process Coverage — {project_name}</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
</head>
<body class="p-4">
<h4>Process Coverage — {project_name}</h4>
<p class="text-muted">Total areas: {len(breakdown)}</p>
<table class="table table-sm table-bordered table-striped">
<thead class="table-light">
<tr><th>Process Area</th><th>Reqs (with tests / total)</th><th>Coverage</th><th>Tests</th><th>Runs</th><th>Passed</th><th>Pass Rate</th></tr>
</thead>
<tbody>
{rows}
</tbody>
</table>
</body>
</html>"""

    response = HttpResponse(html, content_type="text/html; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="coverage_{project_id}.html"'
    return response


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
        _proj_id = credentials.get("project_id")
        corpus_resp = backend_get(f"/codebeamer/tracker/{tracker_id}/corpus",
                                  params={"project_id": _proj_id}) if _proj_id else {}

        if corpus_resp.get("ok"):
            raw_t = corpus_resp["tracker"]
            t_type = raw_t.get("type") or {}
            type_id = t_type.get("id", -1)
            proj = raw_t.get("project") or {}
            context["tracker"] = {
                "id":          raw_t.get("id"),
                "name":        raw_t.get("name", ""),
                "type_id":     type_id,
                "type_name":   t_type.get("name"),
                "description": raw_t.get("description"),
                "key_name":    raw_t.get("keyName"),
                "version":     raw_t.get("version"),
                "created_at":  raw_t.get("createdAt"),
                "created_by":  (raw_t.get("createdBy") or {}).get("name"),
                "modified_at": raw_t.get("modifiedAt"),
                "modified_by": (raw_t.get("modifiedBy") or {}).get("name"),
                "project_id":  proj.get("id") or _proj_id,
                "project_name":proj.get("name"),
                "icon":        TRACKER_ICONS.get(type_id, "label"),
                "color":       raw_t.get("color") or "#6c757d",
            }
            raw_items = corpus_resp.get("items", [])
            _use_corpus_items = True
        else:
            cb = Codebeamer(credentials["cb_url"], credentials["username"], credentials["password"], api_root="api/v3")
            tracker = cb.get_tracker(tracker_id)
            type_id = tracker.type_id
            context["tracker"] = {
                "id":          tracker.id,
                "name":        tracker.name,
                "type_id":     type_id,
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
                "icon":        TRACKER_ICONS.get(type_id, "label"),
                "color":       tracker.color or "#6c757d",
            }
            _proj_id = tracker.project.id if tracker.project else _proj_id
            raw_items = None
            _use_corpus_items = False

        from pybeamer.tracker_item import TrackerItem as _TI
        all_fields = _serialize_fields(
            None if corpus_resp.get("ok") else tracker,  # type: ignore
            project_id=_proj_id,
            tracker_id=tracker_id,
        )
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
        project_id = context["tracker"].get("project_id") or credentials.get("project_id")
        release_items = []
        if project_id:
            resp = backend_get("/codebeamer/items-by-type",
                               params={"project_id": project_id, "type_name": "release"})
            release_items = resp.get("items", [])
        context["release_items"] = release_items

        # Pick form template by tracker type_name, fallback to default
        type_name = context["tracker"].get("type_name") or ""
        type_slug = type_name.lower().replace(" ", "_").replace("/", "_")
        from django.template.loader import get_template
        from django.template import TemplateDoesNotExist
        specific = f"app_main/cb_tracker_forms/{type_slug}.html"
        try:
            get_template(specific)
            context["tracker_form_template"] = specific
        except TemplateDoesNotExist:
            context["tracker_form_template"] = "app_main/cb_tracker_forms/default.html"

        is_issue = type_name.lower() == "issue"
        context["is_issue_tracker"] = is_issue

        # item_map for icon + status-color lookup
        _item_map_resp = backend_get("/codebeamer/item-map", params={"project_id": _proj_id}) if _proj_id else {}
        _item_map = _item_map_resp.get("item_map", {}) if _item_map_resp.get("ok") else {}
        tracker_icon  = context["tracker"].get("icon", "label")
        tracker_color = context["tracker"].get("color", "#6c757d")

        def _serialize_item_raw(it: dict) -> dict:
            iid = it.get("id")
            entry = _item_map.get(str(iid)) or _item_map.get(iid) or {}
            status = (it.get("status") or {}).get("name")
            base = {
                "id":          iid,
                "name":        it.get("name", ""),
                "type_name":   it.get("typeName"),
                "icon":        entry.get("icon", tracker_icon),
                "color":       entry.get("color") or STATUS_COLORS.get(status, tracker_color),
                "status":      status,
                "status_color": STATUS_COLORS.get(status, tracker_color),
            }
            if is_issue:
                priority = ((it.get("priority") or {}).get("name"))
                base["priority"]       = priority
                base["priority_color"] = PRIORITY_COLORS.get(priority, "#adb5bd")
                severities = it.get("severities") or []
                base["severity"]   = severities[0].get("name") if severities else None
                resolutions = it.get("resolutions") or []
                base["resolution"] = resolutions[0].get("name") if resolutions else None
                base["start_date"] = it.get("startDate", "")[:10] if it.get("startDate") else None
                base["end_date"]   = it.get("endDate", "")[:10] if it.get("endDate") else None
            return base

        if _use_corpus_items:
            context["items"] = [_serialize_item_raw(it) for it in raw_items]
        else:
            def _safe(fn):
                try: return fn()
                except: return None
            items = tracker.get_items()
            context["items"] = [_serialize_item_raw({
                "id": i.id, "name": i.name, "typeName": _safe(lambda: i.type_name),
                "status": {"name": _safe(lambda: i.status.value.name if i.status and i.status.value else None)},
                "priority": {"name": _safe(lambda: i.priority.name if i.priority else None)},
                "severities": _safe(lambda: i.severities) or [],
                "resolutions": _safe(lambda: i.resolutions) or [],
                "startDate": str(_safe(lambda: i.start_date)) if _safe(lambda: i.start_date) else None,
                "endDate": str(_safe(lambda: i.end_date)) if _safe(lambda: i.end_date) else None,
            }) for i in tracker.get_items()]
        context["items_total"] = len(context["items"])
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

