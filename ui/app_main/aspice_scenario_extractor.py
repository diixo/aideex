from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_INPUT_PATH = Path("data/aspice/aspice_compliance_inspection_dataset_with_features.json")
DEFAULT_OUTPUT_PATH = Path("data/aspice/aspice_scenario_records_v1.json")


RULE_RECOMMENDATIONS = {
    "RULE-SYS2-HAS-UPSTREAM-STK": "Link the SYS.2 requirement to a stakeholder requirement or approved source artifact.",
    "RULE-SYS2-HAS-SOURCE-EVIDENCE": "Attach at least one evidence source (Confluence, spec, code reference, review note).",
    "RULE-SYS2-HAS-VERIFICATION": "Add explicit verification criteria or link a SYS.5 qualification test case.",
    "RULE-SYS2-IS-SPECIFIC": "Replace vague wording with measurable terms, thresholds, and conditions.",
    "RULE-SWE1-HAS-UPSTREAM-SYS2": "Link the SWE.1 requirement to an upstream SYS.2 requirement.",
    "RULE-SAFETY-RELEVANCE-CLASSIFIED": "Classify safety relevance before approval.",
    "RULE-SAFETY-REQUIRES-REVIEW": "Run formal review for safety-relevant requirements before approval.",
    "RULE-SAFETY-HAS-FAULT-REACTION": "Specify required fault reaction, degraded mode, warning behavior, and acceptance criteria.",
    "RULE-REQ-HAS-SOURCE-EVIDENCE": "Add source evidence to the requirement record.",
    "RULE-REQ-HAS-VERIFICATION-CRITERIA": "Provide testable verification criteria.",
    "RULE-SYS2-HAS-LINKED-TEST-CASE": "Link a relevant SYS.5 test case to this requirement.",
}


def load_dataset(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if not isinstance(payload, dict):
        raise ValueError("Dataset top-level payload must be an object.")

    return payload


def by_id(items: list[dict[str, Any]], key: str = "id") -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        identifier = item.get(key)
        if isinstance(identifier, str) and identifier:
            result[identifier] = item
    return result


def incoming_links(links: list[dict[str, Any]], target_id: str) -> list[dict[str, Any]]:
    return [link for link in links if link.get("to") == target_id]


def outgoing_links(links: list[dict[str, Any]], source_id: str) -> list[dict[str, Any]]:
    return [link for link in links if link.get("from") == source_id]


def infer_process_for_requirement(req_type: str) -> str:
    if req_type == "stakeholder_requirement":
        return "SYS.1"
    if req_type == "system_requirement":
        return "SYS.2"
    if req_type == "software_requirement":
        return "SWE.1"
    return "TBD"


def to_recommendation(rule_id: str, message: str) -> str:
    return RULE_RECOMMENDATIONS.get(rule_id, f"Investigate and resolve: {message}")


def evidence_labels(evidence_ids: list[str], evidence_by_id: dict[str, dict[str, Any]]) -> list[str]:
    labels: list[str] = []

    for source_id in evidence_ids:
        source = evidence_by_id.get(source_id, {})
        source_type = str(source.get("source_type") or "unknown")
        title = str(source.get("title") or source_id)
        labels.append(f"{source_type}:{title}")

    return labels


def normalize_expected_findings(
    findings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    gaps: list[dict[str, Any]] = []
    checks: list[dict[str, Any]] = []

    for finding in findings:
        rule_id = str(finding.get("rule_id") or "RULE-UNKNOWN")
        message = str(finding.get("finding") or "")
        severity = str(finding.get("severity") or "warning")
        requirement_id = str(finding.get("requirement_id") or "")

        checks.append({
            "rule": rule_id,
            "status": "failed" if severity == "error" else "warning",
            "target": requirement_id,
            "message": message,
        })

        gaps.append({
            "rule": rule_id,
            "severity": severity,
            "target_artifact": requirement_id,
            "gap": message,
            "recommendation": to_recommendation(rule_id, message),
        })

    return gaps, checks


def extract_requirement_steps(
    requirements: list[dict[str, Any]],
    links: list[dict[str, Any]],
    test_cases: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    req_steps: list[dict[str, Any]] = []
    tests_by_req: dict[str, list[str]] = {}

    for test_case in test_cases:
        test_case_id = str(test_case.get("id") or "")
        for req_id in test_case.get("verifies", []):
            if isinstance(req_id, str) and req_id:
                tests_by_req.setdefault(req_id, []).append(test_case_id)

    for req in requirements:
        req_id = str(req.get("id") or "")
        req_type = str(req.get("type") or "unknown")
        req_process = str(req.get("aspice_process") or infer_process_for_requirement(req_type))
        req_derived_from = [x for x in req.get("derived_from", []) if isinstance(x, str) and x]
        req_evidence_ids = [x for x in req.get("source_evidence", []) if isinstance(x, str) and x]

        incoming = incoming_links(links, req_id)
        outgoing = outgoing_links(links, req_id)
        touching_links = [str(link.get("id")) for link in (incoming + outgoing) if isinstance(link.get("id"), str)]

        validation_rules: list[str] = []
        if req_type == "system_requirement":
            validation_rules = [
                "RULE-SYS2-HAS-UPSTREAM-STK",
                "RULE-SYS2-HAS-SOURCE-EVIDENCE",
                "RULE-SYS2-HAS-VERIFICATION",
            ]
        elif req_type == "software_requirement":
            validation_rules = [
                "RULE-SWE1-HAS-UPSTREAM-SYS2",
                "RULE-REQ-HAS-SOURCE-EVIDENCE",
                "RULE-REQ-HAS-VERIFICATION-CRITERIA",
            ]
        elif req_type == "stakeholder_requirement":
            validation_rules = ["RULE-REQ-HAS-SOURCE-EVIDENCE"]

        req_steps.append({
            "Process Step": f"process_{req_type}",
            "Artifact Type": req_type,
            "ASPICE Process": req_process,
            "Input Artifact": req_derived_from + [str(link.get("from")) for link in incoming if link.get("from")],
            "Output Artifact": [req_id],
            "Trace Link": touching_links,
            "Evidence Source": evidence_labels(req_evidence_ids, evidence_by_id),
            "Validation Rule": validation_rules,
            "linked_test_cases": tests_by_req.get(req_id, []),
            "status": req.get("status"),
            "review_state": req.get("review_state"),
            "safety_relevance": req.get("safety_relevance"),
        })

    return req_steps


def extract_test_steps(
    test_cases: list[dict[str, Any]],
    evidence_by_id: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []

    for test_case in test_cases:
        test_case_id = str(test_case.get("id") or "")
        verifies = [x for x in test_case.get("verifies", []) if isinstance(x, str) and x]

        validation_rules = ["RULE-SYS2-HAS-LINKED-TEST-CASE"] if verifies else []

        steps.append({
            "Process Step": "define_test_case",
            "Artifact Type": str(test_case.get("type") or "test_case"),
            "ASPICE Process": str(test_case.get("aspice_process") or "SYS.5"),
            "Input Artifact": verifies,
            "Output Artifact": [test_case_id],
            "Trace Link": [],
            "Evidence Source": evidence_labels([str(x) for x in test_case.get("source_evidence", []) if isinstance(x, str)], evidence_by_id),
            "Validation Rule": validation_rules,
            "title": test_case.get("title"),
            "expected_result": test_case.get("expected_result"),
        })

    return steps


def extract_record(record: dict[str, Any]) -> dict[str, Any]:
    feature = record.get("feature", {})
    feature_id = str(feature.get("id") or "FEAT-UNKNOWN")
    sample_id = str(record.get("sample_id") or "SCN-UNKNOWN")
    scenario_title = str(feature.get("name") or sample_id)

    evidence = record.get("input_evidence", [])
    evidence_by_id = by_id(evidence, key="source_id")

    requirements = record.get("input_requirements", [])
    trace_links = record.get("input_trace_links", [])
    test_cases = record.get("input_test_cases", [])

    requirement_steps = extract_requirement_steps(
        requirements=requirements,
        links=trace_links,
        test_cases=test_cases,
        evidence_by_id=evidence_by_id,
    )
    test_steps = extract_test_steps(
        test_cases=test_cases,
        evidence_by_id=evidence_by_id,
    )

    expected_findings = record.get("expected_inspection_findings", [])
    gaps, checks = normalize_expected_findings(expected_findings)

    return {
        "Scenario": {
            "scenario_id": sample_id,
            "feature_id": feature_id,
            "scenario_type": record.get("scenario_type"),
            "title": scenario_title,
            "description": feature.get("description"),
            "feature_owner": feature.get("feature_owner"),
        },
        "Process Step": requirement_steps + test_steps,
        "Validation Rule": checks,
        "Gap": gaps,
        "Recommendation": [item["recommendation"] for item in gaps],
        "source_summary": {
            "requirements": len(requirements),
            "trace_links": len(trace_links),
            "test_cases": len(test_cases),
            "evidence": len(evidence),
        },
    }


def extract_scenarios(payload: dict[str, Any]) -> dict[str, Any]:
    records = payload.get("records", [])
    if not isinstance(records, list):
        raise ValueError("Dataset must contain list field 'records'.")

    scenario_records = [extract_record(record) for record in records if isinstance(record, dict)]

    return {
        "schema_version": "1.0",
        "dataset_type": "aspice_scenario_records",
        "source_dataset_type": payload.get("dataset_type"),
        "scenario_records": scenario_records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract normalized ASPICE scenario records from feature dataset.")
    parser.add_argument("input", nargs="?", default=str(DEFAULT_INPUT_PATH), help="Path to source dataset JSON")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Path to output normalized scenario JSON")
    args = parser.parse_args()

    payload = load_dataset(Path(args.input))
    result = extract_scenarios(payload)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Extracted {len(result['scenario_records'])} scenario records -> {output_path}")


if __name__ == "__main__":
    main()
