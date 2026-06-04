import json
import os

from django.conf import settings
from django.shortcuts import render, redirect

from .aspice_gap_detector import detect_gaps
from .aspice_check_bp_dataset import validate_record
from .aspice_compliance_checker import inspect_case
from .aspice_validate_dataset import validate_dataset, summarize_issues

from pybeamer import Codebeamer


# Material Icons name per tracker type_id. Types that don't have an
# explicit assignment fall back to "label" via the .get() in the view.
_WORK_ITEM_TYPES = {"requirement", "bug", "epic", "issue", "user story", "task", "document", "testrun", "risk", "change request", "work log"}

_CODEBEAMER_TRACKER_ICONS: dict[int, str] = {
    2:   "bug_report",              # Bugs
    3:   "published_with_changes",  # Change Requests — "changes" arrow cycle
    5:   "lightbulb",               # Requirement Specifications (CRS / SRS)
    6:   "check_circle",            # Tasks
    8:   "schedule",                # Timekeeping — clock face
    9:   "play_arrow",              # Test Runs — triangle play button
    10:  "cloud",                   # User Stories
    11:  "warning",                 # Risks — flame/explosion (no `bomb` in classic Material Icons)
    102: "outlined_flag",           # Test Cases — test tube
    103: "directions_subway",       # Releases — launch icon
    107: "phone",                   # Contacts — handset
    108: "playlist_play",           # Test Sets — collection of tests
    109: "tune",                    # Test Configurations — sliders
    150: "groups",                  # Teams — multiple people
    12:  "e_mobiledata",            # Epic
    13:  "description",             # Document
    1:   "fireplace",               # Issue
    105: "extension",               # Components — puzzle piece
}

_STATUS_COLORS = {
    "New":      "#dc3545",
    "Verified": "#fd7e14",
    "Reopened": "#fd7e14",
    "Resolved": "#198754",
    "Closed":   "#0f5132",
    "Rejected": "#6c757d",
    "Draft":    "#ffab46",
}

PRIORITY_COLORS = {
    "Highest": "#dc3545",
    "High": "#fd7e14",
    "Normal": "#198754",
    "Low": "#0d6efd",
    "Lowest": "#6c757d",
}


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


def _load_jsonl(path):
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


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


_CB_SECRETS_FILE = os.path.join(settings.BASE_DIR, "..", "secret", "codebeamer.json")


def _cb_save_credentials(cb_url, username, password, project_id=None):
    os.makedirs(os.path.dirname(_CB_SECRETS_FILE), exist_ok=True)
    data = {"cb_url": cb_url, "username": username, "password": password}
    if project_id is not None:
        data["project_id"] = project_id
    with open(_CB_SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _cb_load_credentials():
    try:
        with open(_CB_SECRETS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _cb_load_tracker_context(cb, project_id, projects):
    project = cb.get_project(project_id)
    trackers = project.get_trackers()
    project_name = next((p["name"] for p in projects if p["id"] == project_id), str(project_id))
    all_trackers = [
        {
            "id":        t.id,
            "name":      t.name,
            "type_id":   t.type_id,
            "type_name": t.type_name,
            "color":    t.color or "#6c757d",
            "icon":     _CODEBEAMER_TRACKER_ICONS.get(t.type_id, "label"),
            "key_name": t.key_name,
        }
        for t in trackers
    ]
    return {
        "selected_project_id":   project_id,
        "selected_project_name": project_name,
        "work_items":   [t for t in all_trackers if (t["type_name"] or "").lower() in _WORK_ITEM_TYPES],
        "config_items": [t for t in all_trackers if (t["type_name"] or "").lower() not in _WORK_ITEM_TYPES],
        "trackers":     all_trackers,
        "has_project_json": os.path.exists(_cb_project_json_path(project_id)),
    }


def _cb_load_projects(cb):
    raw = cb._client.get('projects')
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get('projects', raw.get('items', []))
    else:
        raise ValueError(f"Unexpected /projects response: {type(raw).__name__}: {repr(raw)[:300]}")
    return [{"id": p["id"], "name": p["name"]} for p in items if isinstance(p, dict)]


def codebeamer(request):
    context = {
        "title": "AI-delix - Codebeamer",
        "description": "Codebeamer integration",
    }

    credentials = _cb_load_credentials()

    if request.method == "GET" and credentials:
        try:
            cb = Codebeamer(credentials["cb_url"], credentials["username"], credentials["password"], api_root="api/v3")
            context["projects"] = _cb_load_projects(cb)
            if credentials.get("project_id"):
                context.update(_cb_load_tracker_context(cb, credentials["project_id"], context["projects"]))
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
            credentials = _cb_load_credentials()
            credentials.pop("project_id", None)
            os.makedirs(os.path.dirname(_CB_SECRETS_FILE), exist_ok=True)
            with open(_CB_SECRETS_FILE, "w", encoding="utf-8") as f:
                json.dump(credentials, f)
            from django.shortcuts import redirect as _redirect
            return _redirect("app_main:codebeamer")

        if action in ("load_projects", "load_trackers"):
            try:
                cb = Codebeamer(cb_url, username, password, api_root='api/v3')

                if action == "load_projects":
                    context["projects"] = _cb_load_projects(cb)
                    _cb_save_credentials(cb_url, username, password)

                elif action == "load_trackers":
                    project_id = int(request.POST.get("project_id", 0))
                    context["projects"] = _cb_load_projects(cb)
                    _cb_save_credentials(cb_url, username, password, project_id=project_id)
                    context.update(_cb_load_tracker_context(cb, project_id, context["projects"]))

            except Exception as e:
                context["fetch_error"] = str(e)

        context.update({"cb_url": cb_url, "username": username, "password": password})

    return render(request, "app_main/codebeamer.html", context)


def _serialize_fields(tracker):
    fields = []
    for f in tracker.get_fields():
        if f.hidden:
            continue
        fields.append({
            "id":             f.id,
            "name":           f.name,
            "type":           f._type,
            "multiple":       f.multiple_values or False,
            "options":        [{"id": o.id, "name": o.name} for o in f.options] if f.options else [],
            "tracker_field":  f.tracker_item_field,
        })
    return fields


def _serialize_tree(nodes):
    from pybeamer.folder import Folder
    from pybeamer.tracker import Tracker
    result = []
    for node in nodes:
        if isinstance(node, Folder):
            result.append({
                "kind": "folder",
                "name": node.name,
                "children": _serialize_tree(node.children),
            })
        elif isinstance(node, Tracker):
            result.append({
                "kind": "tracker",
                "id":        node.id,
                "name":      node.name,
                "type_id":   node.type_id,
                "type_name": node.type_name,
                "color":     node.color or "#6c757d",
                "icon":      _CODEBEAMER_TRACKER_ICONS.get(node.type_id, "label"),
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


def _cb_project_json_path(project_id):
    return os.path.join(settings.BASE_DIR, "..", "data", f"codebeamer-prj-{project_id}.json")


def _cb_load_project_data(project_id):
    path = _cb_project_json_path(project_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _cb_build_item_tracker_map(project_id):
    """Returns dict: item_id -> {icon, color} based on its tracker type."""
    data = _cb_load_project_data(project_id)
    if data is None:
        return {}
    result = {}
    for tracker in data.get("trackers", []):
        t_type_id = tracker.get("type_id")
        icon  = _CODEBEAMER_TRACKER_ICONS.get(t_type_id, "label")
        color = tracker.get("color") or "#6c757d"
        for item in tracker.get("items", []):
            iid = item.get("id")
            if iid:
                result[iid] = {"icon": icon, "color": color}
    return result


def _cb_build_tree(item_ids, level, max_level, item_map, direction="upstream", visited=None):
    if visited is None:
        visited = set()
    result = []
    for iid in item_ids:
        iid = int(iid) if not isinstance(iid, int) else iid
        if iid in visited:
            continue
        new_visited = visited | {iid}
        item = item_map.get(iid, {
            "id": iid,
            "name": str(iid),
            "icon": "label",
            "color": "#6c757d",
            "upstream": [],
            "downstream": []
            })
        child_ids = item.get(direction, []) if level < max_level else []
        children = _cb_build_tree(child_ids, level + 1, max_level, item_map, direction, new_visited)
        leaf_count = sum(c["leaf_count"] for c in children) if children else 1
        result.append({"item": item, "level": level, "children": children, "leaf_count": leaf_count})
    return result


def _cb_tree_to_rows(nodes, max_level):
    """Returns list of rows. Each row = list of max_level cells.
    Cell = {item, rowspan} or None (covered by rowspan above)."""
    grid = []

    def recurse(nodes, current_row):
        for node in nodes:
            col = node["level"] - 1
            while len(grid) <= current_row + node["leaf_count"] - 1:
                grid.append([None] * max_level)
            grid[current_row][col] = {"item": node["item"], "rowspan": node["leaf_count"]}
            if node["children"]:
                recurse(node["children"], current_row)
            current_row += node["leaf_count"]

    recurse(nodes, 0)
    return grid


def _cb_build_full_item_map(project_id, downstream_map):
    """Build {item_id: {id, name, icon, color, upstream, downstream}} from saved JSON."""
    data = _cb_load_project_data(project_id)
    if data is None:
        return {}
    item_map = {}
    for tracker in data.get("trackers", []):
        t_type_id = tracker.get("type_id")
        icon  = _CODEBEAMER_TRACKER_ICONS.get(t_type_id, "label")
        color = tracker.get("color") or "#6c757d"
        for item in tracker.get("items", []):
            iid = item.get("id")
            if not iid:
                continue
            upstream = [s.get("id") for s in item.get("subjects", []) if s.get("id")]
            status = item.get("status")
            item_map[iid] = {
                "id":           iid,
                "name":         item.get("name", ""),
                "icon":         icon,
                "color":        _STATUS_COLORS.get(status, color) if status else color,
                "upstream":     upstream,
                "downstream":   [d["id"] for d in downstream_map.get(iid, [])],
            }
    return item_map


def _cb_build_downstream_map(project_id):
    """Returns dict: item_id -> list of {id, name, icon, color} items that reference it upstream."""
    data = _cb_load_project_data(project_id)
    if data is None:
        return {}
    downstream = {}
    for tracker in data.get("trackers", []):
        t_type_id = tracker.get("type_id")
        icon  = _CODEBEAMER_TRACKER_ICONS.get(t_type_id, "label")
        color = tracker.get("color") or "#6c757d"
        for item in tracker.get("items", []):
            item_id = item.get("id")
            item_name = item.get("name", "")
            for s in item.get("subjects", []):
                sid = s.get("id")
                if sid:
                    status = item.get("status")
                    downstream.setdefault(sid, []).append({
                        "id":    item_id,
                        "name":  item_name,
                        "icon":  icon,
                        "color": _STATUS_COLORS.get(status, color) if status else color,
                    })
    return downstream


def codebeamer_save(request):
    import json as _json
    from datetime import datetime
    from django.http import HttpResponse

    credentials = _cb_load_credentials()
    if not credentials or not credentials.get("project_id"):
        from django.shortcuts import redirect as _redirect
        return _redirect("app_main:codebeamer")

    cb = Codebeamer(credentials["cb_url"], credentials["username"], credentials["password"], api_root="api/v3")
    project = cb.get_project(credentials["project_id"])

    def _safe(fn):
        try: return fn()
        except: return None

    trackers_data = []
    for t in project.get_trackers():
        items_data = []
        for item in t.get_items():
            subjects = _safe(lambda: [
                {
                    "id":        s.get("id") if isinstance(s, dict) else getattr(s, "id", None),
                    "name":      s.get("name") if isinstance(s, dict) else getattr(s, "name", None),
                    "type_name": s.get("typeName", s.get("type", {}).get("name") if isinstance(s.get("type"), dict) else s.get("type")) if isinstance(s, dict) else getattr(s, "type_name", None),
                }
                for s in item.subjects
            ] if item.subjects else [])
            items_data.append({
                "id":                 item.id,
                "name":               item.name,
                "description":        item.description,
                "description_format": item.description_format,
                "type_name":          item.type_name,
                "status":             _safe(lambda: item.status.value.name if item.status and item.status.value else None),
                "subjects":           subjects or [],
            })
        trackers_data.append({
            "id":        t.id,
            "name":      t.name,
            "type_id":   t.type_id,
            "type_name": t.type_name,
            "key_name":  t.key_name,
            "color":     t.color or "#6c757d",
            "items":     items_data,
        })

    payload = {
        "project_id":   credentials["project_id"],
        "project_name": project.name,
        "exported_at":  datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S"),
        "trackers":     trackers_data,
    }
    content = _json.dumps(payload, ensure_ascii=False, indent=2)

    save_path = os.path.join(settings.BASE_DIR, "..", "data", f"codebeamer-prj-{credentials['project_id']}.json")
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    with open(save_path, "w", encoding="utf-8") as f:
        f.write(content)

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
            "status_color": _STATUS_COLORS.get(status_name, "#6c757d"),
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
            "tracker_icon":  _CODEBEAMER_TRACKER_ICONS.get(tracker.type_id, "label") if tracker else "label",
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

        downstream_map = _cb_build_downstream_map(project_id)
        if downstream_map is None:
            context["no_project_json"] = True
            context["upstream_rows"] = []
            context["downstream_rows"] = []
            context["upstream_cols"] = max_levels
            context["downstream_cols"] = max_levels
        else:
            item_map = _cb_build_full_item_map(project_id, downstream_map)

            # Upstream: subjects + versions as level 1
            up_ids = [s.get("id") for s in (context.get("item") or {}).get("subjects", []) if s.get("id")]
            up_ids += [v.get("id") for v in (context.get("item") or {}).get("versions", []) if v.get("id")]
            up_tree = _cb_build_tree(up_ids, 1, max_levels, item_map)
            context["upstream_rows"] = _cb_tree_to_rows(up_tree, max_levels)
            context["upstream_cols"] = max_levels

            # Downstream: items referencing current
            dn_ids = [d["id"] for d in downstream_map.get(item_id, [])]
            dn_tree = _cb_build_tree(dn_ids, 1, max_levels, item_map, direction="downstream", visited={item_id})
            context["downstream_rows"] = _cb_tree_to_rows(dn_tree, max_levels)
            context["downstream_cols"] = max_levels

            # Enrich subjects/versions with icon/color
            item_tracker_map = _cb_build_item_tracker_map(project_id)
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

    context = {
        "title": "AI-delix - Codebeamer Tree",
        "cb_url": credentials.get("cb_url", ""),
    }
    try:
        cb = Codebeamer(credentials["cb_url"], credentials["username"], credentials["password"], api_root="api/v3")
        project = cb.get_project(credentials["project_id"])
        context["project_name"] = project.name
        context["tree"] = _serialize_tree(project.get_tree())
    except Exception as e:
        context["fetch_error"] = str(e)

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
            "icon":        _CODEBEAMER_TRACKER_ICONS.get(tracker.type_id, "label"),
        }
        from pybeamer.tracker_item import TrackerItem as _TI
        all_fields = _serialize_fields(tracker)
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

        # Load Release items from Release-type trackers in the same project
        project_id = tracker.project.id if tracker.project else credentials.get("project_id")
        release_items = []
        if project_id:
            project = cb.get_project(project_id)
            for t in project.get_trackers():
                if (t.type_name or "").lower() in ("release", "releases") or t.type_id == 103:
                    for item in t.get_items():
                        release_items.append({"id": item.id, "name": item.name})
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
        # get_items() returns TrackerItem refs — accessing .type_name triggers
        # @loadable which calls _load() → GET /items/{id} → typeName populated
        tracker_obj = cb.get_tracker(tracker_id)
        items = tracker_obj.get_items()
        context["items_total"] = len(items)
        is_issue = (tracker.type_name or "").lower() == "issue"
        context["is_issue_tracker"] = is_issue

        def _safe(fn):
            try: return fn()
            except: return None

        def _serialize_item(i):
            base = {"id": i.id, "name": i.name, "type_name": i.type_name}
            if is_issue:
                priority_name = _safe(lambda: i.priority.name if i.priority else None)
                base["priority"] = priority_name
                base["priority_color"] = PRIORITY_COLORS.get(priority_name, "#adb5bd")
                status_name = _safe(lambda: i.status.value.name if i.status and i.status.value else None)
                base["status"] = status_name
                base["status_color"] = _STATUS_COLORS.get(status_name, "#6c757d")
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

