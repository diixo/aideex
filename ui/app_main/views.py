import json
import os

from django.conf import settings
from django.shortcuts import render, redirect

from .aspice_gap_detector import detect_gaps
from .check_aspice_bp_dataset import validate_record
from .aspice_v04_compliance_checker import inspect_case
from .validate_aspice_v04_dataset import validate_dataset, summarize_issues



# Material Icons name per tracker type_id. Types that don't have an
# explicit assignment fall back to "label" via the .get() in the view.
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
    12: "explicit",                 # Epic
    13: "description",              # Document
    1: "fireplace",                 # Issue
}


def main(request):
    #return redirect(to="app_main:confluence")
    return render(request, "app_main/index.html", context={
        "title": "AI-delix",
        "description": "AI-delix description"})


def dashboard(request):
    return render(request, "app_main/dashboard.html", context={
        "title": "AI-delix - Dashboard",
        "description": "AI-delix dashboard description"})


_REQ_TYPES = {"stakeholder_requirement", "system_requirement", "software_requirement"}


def _build_tree(record, gaps=None):
    reqs_by_id = {}
    test_cases = []

    for a in record.get("expected_artifacts", []):
        aid = a.get("id")
        if not aid:
            continue
        atype = a.get("type")
        if atype in _REQ_TYPES:
            reqs_by_id[aid] = {**a, "children": [], "test_cases": [], "gaps": []}
        elif atype == "test_case":
            test_cases.append(a)

    for tc in test_cases:
        for rid in tc.get("verifies", []):
            if rid in reqs_by_id:
                reqs_by_id[rid]["test_cases"].append(tc)

    if gaps:
        for g in gaps:
            rid = g.get("artifact_id")
            if rid and rid in reqs_by_id:
                reqs_by_id[rid]["gaps"].append(g)

    roots = []
    for rid, node in reqs_by_id.items():
        parent_found = False
        for pid in node.get("derived_from", []):
            if pid in reqs_by_id:
                reqs_by_id[pid]["children"].append(node)
                parent_found = True
        if not parent_found:
            roots.append(node)

    return roots


def _load_jsonl(path):
    records = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


_BP_DATASET = os.path.join(
    settings.BASE_DIR, "..", "data", "aspice",
    "aspice_pipeline_dataset_with_base_practices.jsonl",
)


def _run_analysis(action):
    records = _load_jsonl(_BP_DATASET)
    total_errors = 0
    total_warnings = 0
    total_validation_errors = 0
    results = []

    for record in records:
        gaps = []
        validation_errors = []

        if action in ("detect_gaps", "both"):
            gaps = detect_gaps(record)
        if action in ("validate", "both"):
            validation_errors = validate_record(record)

        errors = [g for g in gaps if g["severity"] == "error"]
        warnings = [g for g in gaps if g["severity"] == "warning"]
        total_errors += len(errors)
        total_warnings += len(warnings)
        total_validation_errors += len(validation_errors)

        results.append({
            "sample_id": record.get("sample_id"),
            "title": record.get("title"),
            "scenario_type": record.get("scenario_type"),
            "process_scope": record.get("process_scope", []),
            "gaps": gaps,
            "errors": errors,
            "warnings": warnings,
            "validation_errors": validation_errors,
            "bp_coverage": record.get("base_practice_coverage", []),
        })

    return {
        "analysis_results": results,
        "analysis_action": action,
        "total_errors": total_errors,
        "total_warnings": total_warnings,
        "total_validation_errors": total_validation_errors,
        "total_scenarios": len(records),
    }



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
    return {
        "selected_project_id":   project_id,
        "selected_project_name": project_name,
        "trackers": [
            {
                "id":        t.id,
                "name":      t.name,
                "type_id":   t.type_id,
                "type_name": t.type_name,
                "color":     t.color or "#6c757d",
                "icon":      _CODEBEAMER_TRACKER_ICONS.get(t.type_id, "label"),
            }
            for t in trackers
        ],
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

    saved = _cb_load_credentials()

    if request.method == "GET" and saved:
        try:
            from pybeamer import Codebeamer
            cb = Codebeamer(saved["cb_url"], saved["username"], saved["password"], api_root="api/v3")
            context["projects"] = _cb_load_projects(cb)
            if saved.get("project_id"):
                context.update(_cb_load_tracker_context(cb, saved["project_id"], context["projects"]))
        except Exception as e:
            context["fetch_error"] = str(e)
        context.update({"cb_url": saved.get("cb_url", ""), "username": saved.get("username", ""), "password": saved.get("password", "")})
        return render(request, "app_main/codebeamer.html", context)

    if request.method == "POST":
        action   = request.POST.get("action", "")
        cb_url   = request.POST.get("cb_url", "").strip() or saved.get("cb_url", "")
        username = request.POST.get("username", "").strip() or saved.get("username", "")
        password = request.POST.get("password", "").strip() or saved.get("password", "")

        if action == "change_project":
            saved = _cb_load_credentials()
            saved.pop("project_id", None)
            os.makedirs(os.path.dirname(_CB_SECRETS_FILE), exist_ok=True)
            with open(_CB_SECRETS_FILE, "w", encoding="utf-8") as f:
                json.dump(saved, f)
            from django.shortcuts import redirect as _redirect
            return _redirect("app_main:codebeamer")

        if action in ("load_projects", "load_trackers"):
            try:
                from pybeamer import Codebeamer
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
    from pybeamer import Codebeamer
    from django.shortcuts import redirect as _redirect

    saved = _cb_load_credentials()
    if not saved:
        return _redirect("app_main:codebeamer")

    if request.method == "POST":
        try:
            cb = Codebeamer(saved["cb_url"], saved["username"], saved["password"], api_root="api/v3")
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
                print("Creating Release with data:", request.POST)
                name        = request.POST.get("name", "").strip()
                description = request.POST.get("description", "").strip()

                def _to_cb_date(d):
                    return f"{d}T00:00:00.000Z" if d else None

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
                print("::", kwargs)
                #kwargs["status"] = {"type": "ChoiceOptionReference", "id": 1, "name": "Unset"}

                item = tracker.create_tracker_item(
                    name=name,
                    description=description,
                    type_name="Release",
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


def codebeamer_tree(request):
    saved = _cb_load_credentials()
    if not saved or not saved.get("project_id"):
        from django.shortcuts import redirect as _redirect
        return _redirect("app_main:codebeamer")

    context = {
        "title": "AI-delix - Codebeamer Tree",
        "cb_url": saved.get("cb_url", ""),
    }
    try:
        from pybeamer import Codebeamer
        cb = Codebeamer(saved["cb_url"], saved["username"], saved["password"], api_root="api/v3")
        project = cb.get_project(saved["project_id"])
        context["project_name"] = project.name
        context["tree"] = _serialize_tree(project.get_tree())
    except Exception as e:
        context["fetch_error"] = str(e)

    return render(request, "app_main/codebeamer_tree.html", context)


def codebeamer_tracker(request, tracker_id):
    saved = _cb_load_credentials()
    if not saved:
        from django.shortcuts import redirect as _redirect
        return _redirect("app_main:codebeamer")

    context = {
        "title": f"AI-delix - Tracker {tracker_id}",
        "tracker_id": tracker_id,
        "cb_url": saved.get("cb_url", ""),
    }
    try:
        from pybeamer import Codebeamer
        cb = Codebeamer(saved["cb_url"], saved["username"], saved["password"], api_root="api/v3")
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
        project_id = tracker.project.id if tracker.project else saved.get("project_id")
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
        context["items"] = [
            {"id": i.id, "name": i.name, "type_name": i.type_name}
            for i in items
        ]
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

