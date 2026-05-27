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


def _build_feature_tree(record):
    reqs_by_id = {}
    test_cases = []

    for r in record.get("input_requirements", []):
        reqs_by_id[r["id"]] = {**r, "children": [], "test_cases": [], "findings": []}

    for tc in record.get("input_test_cases", []):
        test_cases.append(tc)
        for rid in tc.get("verifies", []):
            if rid in reqs_by_id:
                reqs_by_id[rid]["test_cases"].append(tc)

    for f in record.get("expected_inspection_findings", []):
        rid = f.get("requirement_id")
        if rid and rid in reqs_by_id:
            reqs_by_id[rid]["findings"].append(f)

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


def _detect_feature_gaps(record):
    gaps = []
    reqs = {r["id"]: r for r in record.get("input_requirements", []) if "id" in r}
    trace_links = record.get("input_trace_links", [])

    def has_outgoing(artifact_id, link_type=None):
        return any(
            lnk.get("from") == artifact_id and (link_type is None or lnk.get("link_type") == link_type)
            for lnk in trace_links
        )

    def has_incoming(artifact_id, link_type=None):
        return any(
            lnk.get("to") == artifact_id and (link_type is None or lnk.get("link_type") == link_type)
            for lnk in trace_links
        )

    for rid, req in reqs.items():
        rtype = req.get("type")
        if rtype not in {"system_requirement", "software_requirement"}:
            continue

        if not req.get("source_evidence"):
            gaps.append({"severity": "error", "category": "evidence", "artifact_id": rid,
                         "message": f"{rid} has no source evidence."})

        if not req.get("derived_from") and not has_incoming(rid):
            gaps.append({"severity": "error", "category": "traceability", "artifact_id": rid,
                         "message": f"{rid} has no upstream traceability."})

        if rtype == "system_requirement":
            if not req.get("verification_criteria") and not has_outgoing(rid, "verified_by"):
                gaps.append({"severity": "error", "category": "verification", "artifact_id": rid,
                             "message": f"{rid} has no verification criteria and no linked test case."})

            safety = req.get("safety_relevance")
            if safety == "TBD":
                gaps.append({"severity": "warning", "category": "safety", "artifact_id": rid,
                             "message": f"{rid} safety relevance is TBD."})
            if safety == "Yes" and req.get("review_state") != "Reviewed":
                gaps.append({"severity": "warning", "category": "review", "artifact_id": rid,
                             "message": f"{rid} is safety-relevant but review_state is '{req.get('review_state')}'."})

    return gaps


def _collect_feature_counts(dataset):
    records = dataset.get("records", [])
    counts = {
        "features": len(records),
        "inspection_rules": len(dataset.get("inspection_rules", [])),
        "input_evidence": 0,
        "input_requirements": 0,
        "input_trace_links": 0,
        "input_test_cases": 0,
        "expected_findings": 0,
    }
    for rec in records:
        counts["input_evidence"] += len(rec.get("input_evidence", []))
        counts["input_requirements"] += len(rec.get("input_requirements", []))
        counts["input_trace_links"] += len(rec.get("input_trace_links", []))
        counts["input_test_cases"] += len(rec.get("input_test_cases", []))
        counts["expected_findings"] += len(rec.get("expected_inspection_findings", []))
    return counts


def featured(request):
    dataset_path = os.path.join(
        settings.BASE_DIR, "..", "data", "aspice",
        "aspice_compliance_inspection_dataset_with_features.json",
    )
    with open(dataset_path, encoding="utf-8") as f:
        dataset = json.load(f)

    feature_counts = _collect_feature_counts(dataset)

    gap_results = []
    gap_total_errors = 0
    gap_total_warnings = 0
    if request.method == "POST" and request.POST.get("action") == "detect_gaps":
        for record in dataset.get("records", []):
            gaps = _detect_feature_gaps(record)
            errors = [g for g in gaps if g["severity"] == "error"]
            warnings = [g for g in gaps if g["severity"] == "warning"]
            gap_total_errors += len(errors)
            gap_total_warnings += len(warnings)
            gap_results.append({
                "name": record.get("feature", {}).get("name", record.get("sample_id", "Unknown")),
                "sample_id": record.get("sample_id"),
                "gaps": gaps,
                "errors": errors,
                "warnings": warnings,
            })

    features = {}
    for record in dataset.get("records", []):
        name = record.get("feature", {}).get("name", record.get("sample_id", "Unknown"))
        rec = dict(record)
        rec["input_evidence"] = {
            e["source_id"]: e for e in rec.get("input_evidence", []) if "source_id" in e
        }
        rec["input_requirements"] = {
            r["id"]: r for r in rec.get("input_requirements", []) if "id" in r
        }
        rec["input_trace_links"] = {
            t["id"]: t for t in rec.get("input_trace_links", []) if "id" in t
        }
        rec["input_test_cases"] = {
            t["id"]: t for t in rec.get("input_test_cases", []) if "id" in t
        }
        rec["expected_inspection_findings"] = {
            f["id"]: f for f in rec.get("expected_inspection_findings", []) if "id" in f
        }
        features[name] = rec

    rules = {}
    for rule in dataset.get("inspection_rules", []):
        rules[rule.get("rule_id", "unknown")] = rule

    dataset_out = {k: v for k, v in dataset.items() if k not in ("records", "inspection_rules")}
    dataset_out["inspection_rules"] = rules
    dataset_out["features"] = features

    return render(request, "app_main/featured.html", context={
        "title": "AIdelix - Features",
        "description": "Feature-level ASPICE compliance inspection",
        "dataset_json": json.dumps(dataset_out, ensure_ascii=False),
        "feature_counts": feature_counts,
        "gap_results": gap_results,
        "gap_total_errors": gap_total_errors,
        "gap_total_warnings": gap_total_warnings,
    })


_FEATURES_04_DATASET = os.path.join(
    settings.BASE_DIR, "..", "data", "aspice-new",
    "aspice_compliance_checker_test_dataset_v04.json",
)


def _collect_features_04_counts(dataset):
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


def features_04(request):
    with open(_FEATURES_04_DATASET, encoding="utf-8") as f:
        dataset = json.load(f)

    entity_counts = _collect_features_04_counts(dataset)

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

    return render(request, "app_main/features_04.html", context={
        "title": "AIdelix - Features v04",
        "description": "ASPICE compliance checker — dataset v04",
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

    return render(request, "app_main/lifecycle_scenario.html", context={
        "title": f"AIdelix - Lifecycle: {feature_id}",
        "description": "Feature lifecycle scenario",
        "feature_id": feature_id,
        "case": case,
    })


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

