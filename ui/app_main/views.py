import json
import os

from django.conf import settings
from django.shortcuts import render, redirect

from .aspice_gap_detector import detect_gaps
from .check_aspice_bp_dataset import validate_record
from .aspice_v04_compliance_checker import inspect_case
from .validate_aspice_v04_dataset import validate_dataset, summarize_issues


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


def codebeamer(request):
    return render(request, "app_main/codebeamer.html", context={
        "title": "AI-delix - Codebeamer",
        "description": "Codebeamer integration",
    })


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

