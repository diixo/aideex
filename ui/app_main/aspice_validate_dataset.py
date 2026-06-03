import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


DEFAULT_JSON_PATH = Path("aspice_compliance_checker_test_dataset_v04.json")
DEFAULT_JSONL_PATH = Path("aspice_compliance_checker_test_cases_v04.jsonl")


VALID_REQUIREMENT_TYPES = {
    "stakeholder_requirement",
    "system_requirement",
    "software_requirement",
}

VALID_ASPICE_BY_REQ_TYPE = {
    "stakeholder_requirement": {"SYS.1"},
    "system_requirement": {"SYS.2"},
    "software_requirement": {"SWE.1"},
}

VALID_LINK_TYPES = {
    "derived_by",
    "allocated_to_software",
    "verified_by",
    "allocated_to_architecture",
    "impacts",
}

VALID_SAFETY_RELEVANCE = {"Yes", "No", "TBD"}
VALID_REVIEW_STATES = {"Not reviewed", "Reviewed", "In review", "Rejected", "TBD"}
VALID_STATUSES = {"Draft", "Approved", "Rejected", "Deprecated", "Implemented", "Verified", "TBD"}

VALID_SEVERITIES = {"error", "warning", "info"}
VALID_DIMENSIONS = {
    "source_evidence",
    "upstream_traceability",
    "downstream_traceability",
    "verification_criteria",
    "linked_test_case",
    "review_status",
    "safety_classification",
    "cross_level_consistency",
}


ValidationIssue = dict[str, Any]
InspectionCase = dict[str, Any]


def by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items if "id" in item}


def issue(
    *,
    severity: str,
    code: str,
    message: str,
    path: str = "",
    case_id: str | None = None,
) -> ValidationIssue:
    payload: ValidationIssue = {
        "severity": severity,
        "code": code,
        "message": message,
    }

    if path:
        payload["path"] = path

    if case_id:
        payload["inspection_case_id"] = case_id

    return payload


def load_json_dataset(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise ValueError("Top-level JSON must be an object.")

    return payload


def load_jsonl_cases(path: Path) -> list[InspectionCase]:
    cases: list[InspectionCase] = []

    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                case = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no}: {exc}") from exc

            if not isinstance(case, dict):
                raise ValueError(f"Line {line_no}: each JSONL line must be an object.")

            cases.append(case)

    return cases


def load_payload(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".jsonl":
        return {
            "schema_version": "0.4",
            "dataset_type": "aspice_compliance_checker_test_dataset_jsonl",
            "inspection_cases": load_jsonl_cases(path),
        }

    return load_json_dataset(path)


def require_field(
    obj: dict[str, Any],
    field: str,
    issues: list[ValidationIssue],
    *,
    path: str,
    case_id: str | None = None,
    expected_type: type | tuple[type, ...] | None = None,
) -> bool:
    if field not in obj:
        issues.append(issue(
            severity="error",
            code="MISSING_FIELD",
            message=f"Missing required field '{field}'.",
            path=f"{path}.{field}" if path else field,
            case_id=case_id,
        ))
        return False

    if expected_type is not None and not isinstance(obj[field], expected_type):
        issues.append(issue(
            severity="error",
            code="INVALID_FIELD_TYPE",
            message=f"Field '{field}' has invalid type. Expected {expected_type}, got {type(obj[field]).__name__}.",
            path=f"{path}.{field}" if path else field,
            case_id=case_id,
        ))
        return False

    return True


def validate_top_level(payload: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    require_field(payload, "schema_version", issues, path="", expected_type=str)
    require_field(payload, "inspection_cases", issues, path="", expected_type=list)

    if payload.get("schema_version") != "0.4":
        issues.append(issue(
            severity="warning",
            code="UNEXPECTED_SCHEMA_VERSION",
            message=f"Expected schema_version '0.4', got {payload.get('schema_version')!r}.",
            path="schema_version",
        ))

    if "rule_catalog" in payload and not isinstance(payload["rule_catalog"], list):
        issues.append(issue(
            severity="error",
            code="INVALID_RULE_CATALOG_TYPE",
            message="rule_catalog must be a list when present.",
            path="rule_catalog",
        ))

    if "inspection_dimensions" in payload and not isinstance(payload["inspection_dimensions"], list):
        issues.append(issue(
            severity="error",
            code="INVALID_INSPECTION_DIMENSIONS_TYPE",
            message="inspection_dimensions must be a list when present.",
            path="inspection_dimensions",
        ))

    return issues


def validate_rule_catalog(payload: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    rule_catalog = payload.get("rule_catalog", [])

    if not isinstance(rule_catalog, list):
        return issues

    seen_rule_ids: set[str] = set()

    for idx, rule in enumerate(rule_catalog):
        path = f"rule_catalog[{idx}]"

        if not isinstance(rule, dict):
            issues.append(issue(
                severity="error",
                code="INVALID_RULE_TYPE",
                message="Rule must be an object.",
                path=path,
            ))
            continue

        require_field(rule, "rule_id", issues, path=path, expected_type=str)
        require_field(rule, "dimension", issues, path=path, expected_type=str)
        require_field(rule, "severity_if_failed", issues, path=path, expected_type=str)

        rule_id = rule.get("rule_id")
        if rule_id:
            if rule_id in seen_rule_ids:
                issues.append(issue(
                    severity="error",
                    code="DUPLICATE_RULE_ID",
                    message=f"Duplicate rule_id '{rule_id}'.",
                    path=f"{path}.rule_id",
                ))
            seen_rule_ids.add(rule_id)

        dimension = rule.get("dimension")
        if dimension and dimension not in VALID_DIMENSIONS:
            issues.append(issue(
                severity="warning",
                code="UNKNOWN_RULE_DIMENSION",
                message=f"Unknown rule dimension '{dimension}'.",
                path=f"{path}.dimension",
            ))

        severity = rule.get("severity_if_failed")
        if severity and severity not in VALID_SEVERITIES:
            issues.append(issue(
                severity="warning",
                code="UNKNOWN_RULE_SEVERITY",
                message=f"Unknown severity_if_failed '{severity}'.",
                path=f"{path}.severity_if_failed",
            ))

    return issues


def validate_feature(case: InspectionCase, issues: list[ValidationIssue], case_id: str) -> None:
    path = "feature"

    if not require_field(case, "feature", issues, path="", case_id=case_id, expected_type=dict):
        return

    feature = case["feature"]

    require_field(feature, "id", issues, path=path, case_id=case_id, expected_type=str)
    require_field(feature, "type", issues, path=path, case_id=case_id, expected_type=str)
    require_field(feature, "name", issues, path=path, case_id=case_id, expected_type=str)
    require_field(feature, "description", issues, path=path, case_id=case_id, expected_type=str)
    require_field(feature, "safety_relevance", issues, path=path, case_id=case_id, expected_type=str)
    require_field(feature, "status", issues, path=path, case_id=case_id, expected_type=str)

    if feature.get("type") != "feature":
        issues.append(issue(
            severity="error",
            code="INVALID_FEATURE_TYPE",
            message="feature.type must be 'feature'.",
            path="feature.type",
            case_id=case_id,
        ))

    if case.get("target_feature_id") and feature.get("id") != case.get("target_feature_id"):
        issues.append(issue(
            severity="error",
            code="TARGET_FEATURE_MISMATCH",
            message="target_feature_id must match feature.id.",
            path="target_feature_id",
            case_id=case_id,
        ))

    if feature.get("safety_relevance") not in VALID_SAFETY_RELEVANCE:
        issues.append(issue(
            severity="warning",
            code="INVALID_FEATURE_SAFETY_RELEVANCE",
            message=f"Unexpected feature.safety_relevance {feature.get('safety_relevance')!r}.",
            path="feature.safety_relevance",
            case_id=case_id,
        ))

    if feature.get("status") not in VALID_STATUSES:
        issues.append(issue(
            severity="warning",
            code="INVALID_FEATURE_STATUS",
            message=f"Unexpected feature.status {feature.get('status')!r}.",
            path="feature.status",
            case_id=case_id,
        ))

    aspice_scope = feature.get("aspice_scope")
    if aspice_scope is not None:
        if not isinstance(aspice_scope, dict):
            issues.append(issue(
                severity="error",
                code="INVALID_ASPICE_SCOPE_TYPE",
                message="feature.aspice_scope must be an object.",
                path="feature.aspice_scope",
                case_id=case_id,
            ))
        elif "inspection_focus" in aspice_scope and not isinstance(aspice_scope["inspection_focus"], list):
            issues.append(issue(
                severity="error",
                code="INVALID_INSPECTION_FOCUS_TYPE",
                message="feature.aspice_scope.inspection_focus must be a list.",
                path="feature.aspice_scope.inspection_focus",
                case_id=case_id,
            ))


def collect_ids(case: InspectionCase) -> dict[str, set[str]]:
    ids: dict[str, set[str]] = {
        "evidence": set(),
        "requirements": set(),
        "trace_links": set(),
        "test_cases": set(),
        "findings": set(),
    }

    ids["evidence"] = {x["id"] for x in case.get("input_evidence", []) if isinstance(x, dict) and "id" in x}
    ids["requirements"] = {x["id"] for x in case.get("requirements", []) if isinstance(x, dict) and "id" in x}
    ids["trace_links"] = {x["id"] for x in case.get("trace_links", []) if isinstance(x, dict) and "id" in x}
    ids["test_cases"] = {x["id"] for x in case.get("test_cases", []) if isinstance(x, dict) and "id" in x}
    ids["findings"] = {x["id"] for x in case.get("expected_findings", []) if isinstance(x, dict) and "id" in x}

    return ids


def validate_duplicate_ids(case: InspectionCase, issues: list[ValidationIssue], case_id: str) -> None:
    collections = {
        "input_evidence": case.get("input_evidence", []),
        "requirements": case.get("requirements", []),
        "trace_links": case.get("trace_links", []),
        "test_cases": case.get("test_cases", []),
        "expected_findings": case.get("expected_findings", []),
    }

    all_ids: list[tuple[str, str]] = []

    for collection_name, items in collections.items():
        if not isinstance(items, list):
            continue

        counter = Counter()
        for item in items:
            if isinstance(item, dict) and "id" in item:
                counter[item["id"]] += 1
                all_ids.append((item["id"], collection_name))

        for item_id, count in counter.items():
            if count > 1:
                issues.append(issue(
                    severity="error",
                    code="DUPLICATE_ID_IN_COLLECTION",
                    message=f"ID '{item_id}' appears {count} times in {collection_name}.",
                    path=collection_name,
                    case_id=case_id,
                ))

    global_counter = Counter(item_id for item_id, _ in all_ids)
    for item_id, count in global_counter.items():
        if count > 1:
            locations = [loc for found_id, loc in all_ids if found_id == item_id]
            issues.append(issue(
                severity="warning",
                code="DUPLICATE_ID_ACROSS_COLLECTIONS",
                message=f"ID '{item_id}' appears across collections: {locations}.",
                case_id=case_id,
            ))


def validate_evidence(case: InspectionCase, issues: list[ValidationIssue], case_id: str) -> None:
    if not require_field(case, "input_evidence", issues, path="", case_id=case_id, expected_type=list):
        return

    for idx, ev in enumerate(case.get("input_evidence", [])):
        path = f"input_evidence[{idx}]"

        if not isinstance(ev, dict):
            issues.append(issue(
                severity="error",
                code="INVALID_EVIDENCE_TYPE",
                message="Evidence item must be an object.",
                path=path,
                case_id=case_id,
            ))
            continue

        require_field(ev, "id", issues, path=path, case_id=case_id, expected_type=str)
        require_field(ev, "type", issues, path=path, case_id=case_id, expected_type=str)
        require_field(ev, "text", issues, path=path, case_id=case_id, expected_type=str)


def validate_requirements(case: InspectionCase, issues: list[ValidationIssue], case_id: str) -> None:
    if not require_field(case, "requirements", issues, path="", case_id=case_id, expected_type=list):
        return

    ids = collect_ids(case)
    feature_id = case.get("feature", {}).get("id")

    for idx, req in enumerate(case.get("requirements", [])):
        path = f"requirements[{idx}]"

        if not isinstance(req, dict):
            issues.append(issue(
                severity="error",
                code="INVALID_REQUIREMENT_TYPE",
                message="Requirement must be an object.",
                path=path,
                case_id=case_id,
            ))
            continue

        require_field(req, "id", issues, path=path, case_id=case_id, expected_type=str)
        require_field(req, "feature_id", issues, path=path, case_id=case_id, expected_type=str)
        require_field(req, "type", issues, path=path, case_id=case_id, expected_type=str)
        require_field(req, "aspice_process", issues, path=path, case_id=case_id, expected_type=str)
        require_field(req, "statement", issues, path=path, case_id=case_id, expected_type=str)
        require_field(req, "source_evidence", issues, path=path, case_id=case_id, expected_type=list)
        require_field(req, "derived_from", issues, path=path, case_id=case_id, expected_type=list)
        require_field(req, "verification_criteria", issues, path=path, case_id=case_id, expected_type=str)
        require_field(req, "safety_relevance", issues, path=path, case_id=case_id, expected_type=str)
        require_field(req, "review_state", issues, path=path, case_id=case_id, expected_type=str)
        require_field(req, "status", issues, path=path, case_id=case_id, expected_type=str)

        req_id = req.get("id")
        req_type = req.get("type")
        aspice_process = req.get("aspice_process")

        if feature_id and req.get("feature_id") != feature_id:
            issues.append(issue(
                severity="error",
                code="REQUIREMENT_FEATURE_MISMATCH",
                message=f"Requirement {req_id} feature_id must match feature.id.",
                path=f"{path}.feature_id",
                case_id=case_id,
            ))

        if req_type not in VALID_REQUIREMENT_TYPES:
            issues.append(issue(
                severity="error",
                code="UNKNOWN_REQUIREMENT_TYPE",
                message=f"Unknown requirement type {req_type!r}.",
                path=f"{path}.type",
                case_id=case_id,
            ))
        else:
            valid_processes = VALID_ASPICE_BY_REQ_TYPE.get(req_type, set())
            if aspice_process not in valid_processes:
                issues.append(issue(
                    severity="warning",
                    code="REQUIREMENT_ASPICE_PROCESS_MISMATCH",
                    message=f"{req_id}: {req_type} usually maps to {sorted(valid_processes)}, got {aspice_process!r}.",
                    path=f"{path}.aspice_process",
                    case_id=case_id,
                ))

        for ev_id in req.get("source_evidence", []):
            if ev_id not in ids["evidence"]:
                issues.append(issue(
                    severity="error",
                    code="UNKNOWN_SOURCE_EVIDENCE_REF",
                    message=f"{req_id} references unknown source evidence '{ev_id}'.",
                    path=f"{path}.source_evidence",
                    case_id=case_id,
                ))

        for upstream_id in req.get("derived_from", []):
            if upstream_id not in ids["requirements"]:
                issues.append(issue(
                    severity="error",
                    code="UNKNOWN_DERIVED_FROM_REF",
                    message=f"{req_id} references unknown upstream requirement '{upstream_id}'.",
                    path=f"{path}.derived_from",
                    case_id=case_id,
                ))

        safety = req.get("safety_relevance")
        if safety not in VALID_SAFETY_RELEVANCE:
            issues.append(issue(
                severity="warning",
                code="INVALID_REQUIREMENT_SAFETY_RELEVANCE",
                message=f"{req_id}: unexpected safety_relevance {safety!r}.",
                path=f"{path}.safety_relevance",
                case_id=case_id,
            ))

        review = req.get("review_state")
        if review not in VALID_REVIEW_STATES:
            issues.append(issue(
                severity="warning",
                code="INVALID_REVIEW_STATE",
                message=f"{req_id}: unexpected review_state {review!r}.",
                path=f"{path}.review_state",
                case_id=case_id,
            ))

        status = req.get("status")
        if status not in VALID_STATUSES:
            issues.append(issue(
                severity="warning",
                code="INVALID_REQUIREMENT_STATUS",
                message=f"{req_id}: unexpected status {status!r}.",
                path=f"{path}.status",
                case_id=case_id,
            ))


def validate_trace_links(case: InspectionCase, issues: list[ValidationIssue], case_id: str) -> None:
    if not require_field(case, "trace_links", issues, path="", case_id=case_id, expected_type=list):
        return

    ids = collect_ids(case)
    valid_targets = ids["requirements"] | ids["test_cases"]

    for idx, link in enumerate(case.get("trace_links", [])):
        path = f"trace_links[{idx}]"

        if not isinstance(link, dict):
            issues.append(issue(
                severity="error",
                code="INVALID_TRACE_LINK_TYPE",
                message="Trace link must be an object.",
                path=path,
                case_id=case_id,
            ))
            continue

        require_field(link, "id", issues, path=path, case_id=case_id, expected_type=str)
        require_field(link, "from", issues, path=path, case_id=case_id, expected_type=str)
        require_field(link, "to", issues, path=path, case_id=case_id, expected_type=str)
        require_field(link, "link_type", issues, path=path, case_id=case_id, expected_type=str)

        source_id = link.get("from")
        target_id = link.get("to")
        link_type = link.get("link_type")

        if source_id not in ids["requirements"] and source_id not in ids["test_cases"]:
            issues.append(issue(
                severity="error",
                code="UNKNOWN_TRACE_SOURCE",
                message=f"Trace link source '{source_id}' does not exist.",
                path=f"{path}.from",
                case_id=case_id,
            ))

        if target_id not in valid_targets:
            issues.append(issue(
                severity="error",
                code="UNKNOWN_TRACE_TARGET",
                message=f"Trace link target '{target_id}' does not exist.",
                path=f"{path}.to",
                case_id=case_id,
            ))

        if link_type not in VALID_LINK_TYPES:
            issues.append(issue(
                severity="warning",
                code="UNKNOWN_LINK_TYPE",
                message=f"Unknown link_type {link_type!r}.",
                path=f"{path}.link_type",
                case_id=case_id,
            ))

        # Stronger semantic checks for common v04 link types.
        reqs = by_id(case.get("requirements", []))
        tests = by_id(case.get("test_cases", []))

        if link_type == "derived_by":
            if source_id not in reqs or target_id not in reqs:
                issues.append(issue(
                    severity="warning",
                    code="DERIVED_BY_NON_REQUIREMENT",
                    message="derived_by should connect requirements.",
                    path=path,
                    case_id=case_id,
                ))

        if link_type == "allocated_to_software":
            source = reqs.get(source_id, {})
            target = reqs.get(target_id, {})
            if source.get("type") != "system_requirement" or target.get("type") != "software_requirement":
                issues.append(issue(
                    severity="warning",
                    code="ALLOCATED_TO_SOFTWARE_TYPE_MISMATCH",
                    message="allocated_to_software should connect system_requirement -> software_requirement.",
                    path=path,
                    case_id=case_id,
                ))

        if link_type == "verified_by":
            source = reqs.get(source_id, {})
            target = tests.get(target_id, {})
            if source.get("type") != "system_requirement" or not target:
                issues.append(issue(
                    severity="warning",
                    code="VERIFIED_BY_TYPE_MISMATCH",
                    message="verified_by should connect system_requirement -> test_case.",
                    path=path,
                    case_id=case_id,
                ))


def validate_test_cases(case: InspectionCase, issues: list[ValidationIssue], case_id: str) -> None:
    if not require_field(case, "test_cases", issues, path="", case_id=case_id, expected_type=list):
        return

    ids = collect_ids(case)
    feature_id = case.get("feature", {}).get("id")

    for idx, tc in enumerate(case.get("test_cases", [])):
        path = f"test_cases[{idx}]"

        if not isinstance(tc, dict):
            issues.append(issue(
                severity="error",
                code="INVALID_TEST_CASE_TYPE",
                message="Test case must be an object.",
                path=path,
                case_id=case_id,
            ))
            continue

        require_field(tc, "id", issues, path=path, case_id=case_id, expected_type=str)
        require_field(tc, "feature_id", issues, path=path, case_id=case_id, expected_type=str)
        require_field(tc, "type", issues, path=path, case_id=case_id, expected_type=str)
        require_field(tc, "aspice_process", issues, path=path, case_id=case_id, expected_type=str)
        require_field(tc, "title", issues, path=path, case_id=case_id, expected_type=str)
        require_field(tc, "verifies", issues, path=path, case_id=case_id, expected_type=list)
        require_field(tc, "review_state", issues, path=path, case_id=case_id, expected_type=str)
        require_field(tc, "status", issues, path=path, case_id=case_id, expected_type=str)
        require_field(tc, "test_steps", issues, path=path, case_id=case_id, expected_type=list)
        require_field(tc, "expected_result", issues, path=path, case_id=case_id, expected_type=str)

        tc_id = tc.get("id")

        if tc.get("type") != "test_case":
            issues.append(issue(
                severity="error",
                code="INVALID_TEST_CASE_KIND",
                message=f"{tc_id}: test case type must be 'test_case'.",
                path=f"{path}.type",
                case_id=case_id,
            ))

        if tc.get("aspice_process") != "SYS.5":
            issues.append(issue(
                severity="warning",
                code="TEST_CASE_ASPICE_PROCESS_MISMATCH",
                message=f"{tc_id}: expected aspice_process SYS.5 for system qualification test case.",
                path=f"{path}.aspice_process",
                case_id=case_id,
            ))

        if feature_id and tc.get("feature_id") != feature_id:
            issues.append(issue(
                severity="error",
                code="TEST_CASE_FEATURE_MISMATCH",
                message=f"{tc_id}: feature_id must match feature.id.",
                path=f"{path}.feature_id",
                case_id=case_id,
            ))

        for req_id in tc.get("verifies", []):
            if req_id not in ids["requirements"]:
                issues.append(issue(
                    severity="error",
                    code="UNKNOWN_TEST_VERIFIES_REF",
                    message=f"{tc_id} verifies unknown requirement '{req_id}'.",
                    path=f"{path}.verifies",
                    case_id=case_id,
                ))

        if tc.get("review_state") not in VALID_REVIEW_STATES:
            issues.append(issue(
                severity="warning",
                code="INVALID_TEST_REVIEW_STATE",
                message=f"{tc_id}: unexpected review_state {tc.get('review_state')!r}.",
                path=f"{path}.review_state",
                case_id=case_id,
            ))

        if tc.get("status") not in VALID_STATUSES:
            issues.append(issue(
                severity="warning",
                code="INVALID_TEST_STATUS",
                message=f"{tc_id}: unexpected status {tc.get('status')!r}.",
                path=f"{path}.status",
                case_id=case_id,
            ))


def validate_expected_findings(case: InspectionCase, issues: list[ValidationIssue], case_id: str) -> None:
    if not require_field(case, "expected_findings", issues, path="", case_id=case_id, expected_type=list):
        return

    ids = collect_ids(case)
    valid_targets = ids["requirements"] | ids["test_cases"] | {case.get("feature", {}).get("id")}

    for idx, finding in enumerate(case.get("expected_findings", [])):
        path = f"expected_findings[{idx}]"

        if not isinstance(finding, dict):
            issues.append(issue(
                severity="error",
                code="INVALID_FINDING_TYPE",
                message="Expected finding must be an object.",
                path=path,
                case_id=case_id,
            ))
            continue

        require_field(finding, "id", issues, path=path, case_id=case_id, expected_type=str)
        require_field(finding, "rule_id", issues, path=path, case_id=case_id, expected_type=str)
        require_field(finding, "dimension", issues, path=path, case_id=case_id, expected_type=str)
        require_field(finding, "target_id", issues, path=path, case_id=case_id, expected_type=str)
        require_field(finding, "severity", issues, path=path, case_id=case_id, expected_type=str)
        require_field(finding, "message", issues, path=path, case_id=case_id, expected_type=str)

        if finding.get("target_id") not in valid_targets:
            issues.append(issue(
                severity="error",
                code="UNKNOWN_FINDING_TARGET",
                message=f"Finding target_id '{finding.get('target_id')}' does not exist.",
                path=f"{path}.target_id",
                case_id=case_id,
            ))

        if finding.get("dimension") not in VALID_DIMENSIONS:
            issues.append(issue(
                severity="warning",
                code="UNKNOWN_FINDING_DIMENSION",
                message=f"Unknown finding dimension {finding.get('dimension')!r}.",
                path=f"{path}.dimension",
                case_id=case_id,
            ))

        if finding.get("severity") not in VALID_SEVERITIES:
            issues.append(issue(
                severity="warning",
                code="UNKNOWN_FINDING_SEVERITY",
                message=f"Unknown finding severity {finding.get('severity')!r}.",
                path=f"{path}.severity",
                case_id=case_id,
            ))


def validate_case(case: InspectionCase, index: int) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    case_id = case.get("inspection_case_id", f"<case-{index}>")

    require_field(case, "inspection_case_id", issues, path="", case_id=case_id, expected_type=str)
    require_field(case, "inspection_type", issues, path="", case_id=case_id, expected_type=str)
    require_field(case, "case_title", issues, path="", case_id=case_id, expected_type=str)
    require_field(case, "target_feature_id", issues, path="", case_id=case_id, expected_type=str)

    if case.get("inspection_type") != "aspice_compliance_inspection":
        issues.append(issue(
            severity="warning",
            code="UNKNOWN_INSPECTION_TYPE",
            message=f"Unexpected inspection_type {case.get('inspection_type')!r}.",
            path="inspection_type",
            case_id=case_id,
        ))

    validate_feature(case, issues, case_id)
    validate_evidence(case, issues, case_id)
    validate_requirements(case, issues, case_id)
    validate_trace_links(case, issues, case_id)
    validate_test_cases(case, issues, case_id)
    validate_expected_findings(case, issues, case_id)
    validate_duplicate_ids(case, issues, case_id)

    return issues


def validate_dataset(payload: dict[str, Any]) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    issues.extend(validate_top_level(payload))
    issues.extend(validate_rule_catalog(payload))

    cases = payload.get("inspection_cases", [])
    if not isinstance(cases, list):
        return issues

    case_ids = []
    feature_ids_by_case = defaultdict(list)

    for idx, case in enumerate(cases):
        if not isinstance(case, dict):
            issues.append(issue(
                severity="error",
                code="INVALID_CASE_TYPE",
                message="Each inspection case must be an object.",
                path=f"inspection_cases[{idx}]",
            ))
            continue

        case_id = case.get("inspection_case_id")
        if case_id:
            case_ids.append(case_id)

        feature_id = case.get("feature", {}).get("id")
        if feature_id:
            feature_ids_by_case[feature_id].append(case_id or f"<case-{idx}>")

        issues.extend(validate_case(case, idx))

    duplicate_case_ids = [item_id for item_id, count in Counter(case_ids).items() if count > 1]
    for case_id in duplicate_case_ids:
        issues.append(issue(
            severity="error",
            code="DUPLICATE_INSPECTION_CASE_ID",
            message=f"Duplicate inspection_case_id '{case_id}'.",
        ))

    # Reusing a feature across several inspection cases can be valid,
    # so this is only informational.
    for feature_id, related_cases in feature_ids_by_case.items():
        if len(related_cases) > 1:
            issues.append(issue(
                severity="info",
                code="FEATURE_USED_IN_MULTIPLE_CASES",
                message=f"Feature '{feature_id}' appears in multiple inspection cases: {related_cases}.",
            ))

    return issues


def summarize_issues(issues: list[ValidationIssue]) -> dict[str, int]:
    counts = Counter(item["severity"] for item in issues)
    return {
        "errors": counts.get("error", 0),
        "warnings": counts.get("warning", 0),
        "info": counts.get("info", 0),
        "total": len(issues),
    }


def print_report(issues: list[ValidationIssue]) -> None:
    summary = summarize_issues(issues)

    print("=" * 120)
    print(
        f"VALIDATION SUMMARY: "
        f"errors={summary['errors']}, "
        f"warnings={summary['warnings']}, "
        f"info={summary['info']}, "
        f"total={summary['total']}"
    )
    print("=" * 120)

    if not issues:
        print("Dataset is structurally valid.")
        return

    severity_order = {"error": 0, "warning": 1, "info": 2}

    for item in sorted(
        issues,
        key=lambda x: (
            severity_order.get(x["severity"], 99),
            x.get("inspection_case_id", ""),
            x.get("code", ""),
            x.get("path", ""),
        )
    ):
        label = item["severity"].upper()
        case = item.get("inspection_case_id", "-")
        path = item.get("path", "-")
        print(f"[{label}] {item['code']} | case={case} | path={path}")
        print(f"  {item['message']}")


def write_report_json(issues: list[ValidationIssue], output_path: Path) -> None:
    payload = {
        "schema_version": "0.4",
        "result_type": "aspice_v04_dataset_validation_report",
        "summary": summarize_issues(issues),
        "issues": issues,
    }

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate ASPICE compliance checker dataset v04.")
    parser.add_argument(
        "path",
        nargs="?",
        default=str(DEFAULT_JSON_PATH),
        help="Path to v04 full JSON dataset or v04 JSONL inspection cases file."
    )
    parser.add_argument(
        "--write-json",
        default="",
        help="Optional path for validation report JSON."
    )
    parser.add_argument(
        "--fail-on-warning",
        action="store_true",
        help="Exit with code 1 if warnings are present."
    )

    args = parser.parse_args()

    payload = load_payload(Path(args.path))
    issues = validate_dataset(payload)

    print_report(issues)

    if args.write_json:
        output_path = Path(args.write_json)
        write_report_json(issues, output_path)
        print(f"Validation report written to: {output_path}")

    summary = summarize_issues(issues)

    if summary["errors"] > 0:
        raise SystemExit(1)

    if args.fail_on_warning and summary["warnings"] > 0:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
