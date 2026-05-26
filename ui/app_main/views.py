import json
import os

from django.conf import settings
from django.shortcuts import render, redirect

from .aspice_gap_detector import detect_gaps
from .check_aspice_bp_dataset import validate_record


def main(request):
    #return redirect(to="app_main:confluence")
    return render(request, "app_main/index.html", context={
        "title": "AIdelix",
        "description": "AIdelix description"})


def dashboard(request):
    return render(request, "app_main/dashboard.html", context={
        "title": "AIdelix - Dashboard",
        "description": "AIdelix dashboard description"})


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


def requirements(request):
    records = _load_jsonl(_BP_DATASET)

    action = ""
    analysis_ctx = {}
    if request.method == "POST":
        action = request.POST.get("action", "")
        if action in ("detect_gaps", "validate"):
            analysis_ctx = _run_analysis(action)

    gaps_by_sample = {}
    if action == "detect_gaps" and "analysis_results" in analysis_ctx:
        for ar in analysis_ctx["analysis_results"]:
            gaps_by_sample[ar["sample_id"]] = ar["gaps"]

    scenarios = []
    for record in records:
        sample_gaps = gaps_by_sample.get(record.get("sample_id"), [])
        record["tree"] = _build_tree(record, gaps=sample_gaps)
        scenarios.append(record)

    context = {
        "title": "AIdelix - Requirements",
        "description": "AIdelix requirements description",
        "scenarios": scenarios,
    }
    context.update(analysis_ctx)

    return render(request, "app_main/requirements.html", context)


def gap_analysis(request):
    context = {
        "title": "AIdelix - Gap Analysis",
        "description": "ASPICE gap analysis results",
    }

    if request.method == "POST":
        action = request.POST.get("action", "")
        if action in ("detect_gaps", "validate"):
            context.update(_run_analysis(action))

    return render(request, "app_main/gap_analysis.html", context)

