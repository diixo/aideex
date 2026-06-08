"""Build normalized ASPICE scenario records from feature inspection payloads.

The builder is intentionally lightweight: it extracts a feature-level scenario
record from Codebeamer-derived inspection data and preserves the fields needed
for traceability, evidence, gap analysis, and reporting.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_JSON_PATH = Path("data/aspice/aspice_compliance_inspection_dataset_with_features.json")


GapRecord = dict[str, Any]
ScenarioRecord = dict[str, Any]


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _ref(identifier: Any, name: Any = None, *, kind: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": str(identifier)}
    if name is not None:
        payload["name"] = str(name)
    if kind is not None:
        payload["type"] = kind
    return payload


def _feature_context_step(record: dict[str, Any]) -> dict[str, Any]:
    feature = record.get("feature") or {}
    evidence = [
        _ref(item.get("source_id"), item.get("title") or item.get("source_type"), kind=item.get("source_type"))
        for item in record.get("input_evidence", [])
        if isinstance(item, dict) and item.get("source_id")
    ]

    return {
        "process_step": "capture_feature_context",
        "artifact_type": "feature",
        "aspice_process": "SYS.1",
        "input_artifact": evidence,
        "output_artifact": [_ref(feature.get("id") or record.get("sample_id"), feature.get("name") or "feature", kind="feature")],
        "trace_link": [],
        "evidence_source": evidence,
        "validation_rule": ["feature context is grounded in source evidence"],
    }


def _requirement_process(req_type: str | None) -> str:
    return {
        "stakeholder_requirement": "SYS.1",
        "system_requirement": "SYS.2",
        "software_requirement": "SWE.1",
    }.get(req_type or "", "UNKNOWN")


def _step_name_for_requirement(req_type: str | None) -> str:
    return {
        "stakeholder_requirement": "capture_stakeholder_requirement",
        "system_requirement": "derive_system_requirement",
        "software_requirement": "derive_software_requirement",
    }.get(req_type or "", "derive_requirement")


def _trace_links_for_requirement(
    requirement: dict[str, Any],
    trace_links: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    req_id = requirement.get("id")
    derived_from = [str(item) for item in _as_list(requirement.get("derived_from")) if item]
    explicit = [
        link
        for link in trace_links
        if isinstance(link, dict)
        and (link.get("to") == req_id or link.get("from") in derived_from)
    ]

    if explicit:
        return explicit

    synthetic: list[dict[str, Any]] = []
    link_type = {
        "stakeholder_requirement": "derived_by",
        "system_requirement": "derived_by",
        "software_requirement": "allocated_to_software",
    }.get(requirement.get("type"), "related_to")

    for upstream_id in derived_from:
        synthetic.append({
            "id": f"synthetic-{upstream_id}-{req_id}",
            "from": upstream_id,
            "to": req_id,
            "link_type": link_type,
        })

    return synthetic


def _input_artifacts_for_requirement(
    requirement: dict[str, Any],
    record: dict[str, Any],
) -> list[dict[str, Any]]:
    req_type = requirement.get("type")
    if req_type == "stakeholder_requirement":
        return [
            _ref(item.get("source_id"), item.get("title") or item.get("source_type"), kind=item.get("source_type"))
            for item in record.get("input_evidence", [])
            if isinstance(item, dict) and item.get("source_id")
        ]

    if req_type in {"system_requirement", "software_requirement"}:
        return [
            _ref(upstream_id, upstream_id, kind="requirement")
            for upstream_id in _as_list(requirement.get("derived_from"))
            if upstream_id
        ]

    return []


def _requirement_validation_rules(requirement: dict[str, Any]) -> list[str]:
    req_type = requirement.get("type")
    if req_type == "stakeholder_requirement":
        return ["source evidence exists"]
    if req_type == "system_requirement":
        return [
            "upstream stakeholder traceability exists",
            "source evidence exists",
            "verification criteria exists",
        ]
    if req_type == "software_requirement":
        return [
            "upstream SYS.2 traceability exists",
            "source evidence exists",
        ]
    return ["artifact is traceable"]


def _test_case_step(test_case: dict[str, Any]) -> dict[str, Any]:
    verified = [str(item) for item in _as_list(test_case.get("verifies")) if item]
    return {
        "process_step": "define_test_case",
        "artifact_type": "test_case",
        "aspice_process": str(test_case.get("aspice_process") or "SYS.5"),
        "input_artifact": [_ref(req_id, req_id, kind="requirement") for req_id in verified],
        "output_artifact": [_ref(test_case.get("id"), test_case.get("title") or test_case.get("id"), kind="test_case")],
        "trace_link": [
            {
                "id": f"synthetic-{req_id}-{test_case.get('id')}",
                "from": req_id,
                "to": str(test_case.get("id")),
                "link_type": "verified_by",
            }
            for req_id in verified
        ],
        "evidence_source": [
            {
                "id": str(test_case.get("id")),
                "name": str(test_case.get("title") or test_case.get("id")),
                "type": "test_case",
            }
        ],
        "validation_rule": ["test case verifies at least one requirement"],
    }


def _behavior_scenarios(record: dict[str, Any]) -> list[dict[str, Any]]:
    feature = record.get("feature") or {}
    behaviors = feature.get("behaviors") or []
    result: list[dict[str, Any]] = []

    for idx, behavior in enumerate(behaviors, start=1):
        if not isinstance(behavior, dict):
            continue

        result.append({
            "behavior_id": str(behavior.get("behavior_id") or f"BEH-{idx:03d}"),
            "name": str(behavior.get("name") or behavior.get("title") or f"Behavior {idx}"),
            "trigger": behavior.get("trigger"),
            "preconditions": _as_list(behavior.get("preconditions")),
            "main_flow": _as_list(behavior.get("main_flow")),
            "alternative_flows": _as_list(behavior.get("alternative_flows")),
            "postconditions": _as_list(behavior.get("postconditions")),
            "verification_idea": behavior.get("verification_idea") or behavior.get("verification"),
            "source": "feature.behaviors",
        })

    return result


def _gap_records(record: dict[str, Any]) -> list[GapRecord]:
    gaps: list[GapRecord] = []

    for finding in record.get("expected_inspection_findings", []):
        if not isinstance(finding, dict):
            continue
        gaps.append({
            "rule_id": finding.get("rule_id"),
            "severity": finding.get("severity"),
            "target_id": finding.get("requirement_id") or finding.get("feature_id"),
            "gap": finding.get("finding"),
            "recommendation": _recommendation_for_rule(str(finding.get("rule_id") or "")),
            "source": "expected_inspection_findings",
        })

    seen: set[tuple[str, str]] = set()
    requirements = record.get("input_requirements", [])
    trace_links = [link for link in record.get("input_trace_links", []) if isinstance(link, dict)]
    test_cases = [tc for tc in record.get("input_test_cases", []) if isinstance(tc, dict)]

    for req in requirements:
        if not isinstance(req, dict):
            continue

        req_id = str(req.get("id") or "")
        req_type = req.get("type")
        upstream = [str(item) for item in _as_list(req.get("derived_from")) if item]
        has_source_evidence = bool(req.get("source_evidence"))

        if req_type == "stakeholder_requirement":
            if not has_source_evidence:
                _append_gap(seen, gaps, "RULE-REQ-HAS-SOURCE-EVIDENCE", "error", req_id, "Stakeholder requirement has no source evidence.", "Add a source evidence reference to the stakeholder requirement.")
            continue

        if req_type == "system_requirement":
            if not upstream and not any(link.get("to") == req_id for link in trace_links):
                _append_gap(seen, gaps, "RULE-SYS2-HAS-UPSTREAM-STK", "error", req_id, "System requirement has no upstream stakeholder traceability.", "Derive the SYS.2 requirement from a stakeholder requirement or approved source.")

            if not has_source_evidence:
                _append_gap(seen, gaps, "RULE-SYS2-HAS-SOURCE-EVIDENCE", "error", req_id, "System requirement has no source evidence.", "Link the requirement to Confluence, code, or another approved source.")

            verification_criteria = str(req.get("verification_criteria") or "").strip()
            has_test_case = any(req_id in _as_list(tc.get("verifies")) for tc in test_cases)
            if not verification_criteria and not has_test_case:
                _append_gap(seen, gaps, "RULE-SYS2-HAS-VERIFICATION", "error", req_id, "System requirement has no verification criteria and no linked SYS.5 qualification test.", "Add verification criteria or link a qualifying test case.")

            safety_relevance = str(req.get("safety_relevance") or "").strip()
            if safety_relevance == "TBD":
                _append_gap(seen, gaps, "RULE-SAFETY-RELEVANCE-CLASSIFIED", "warning", req_id, "Safety relevance is TBD and should be classified before approval.", "Classify safety relevance before moving the requirement to approval.")

            if safety_relevance == "Yes" and str(req.get("review_state") or "") != "Reviewed":
                _append_gap(seen, gaps, "RULE-SAFETY-REQUIRES-REVIEW", "warning", req_id, "Safety-relevant requirement is not reviewed.", "Review the safety-relevant requirement before approval.")

            statement = str(req.get("statement") or "").lower()
            vague_terms = ("low", "fast", "suitable", "appropriate", "as needed")
            if any(term in statement for term in vague_terms) and "threshold" not in statement:
                _append_gap(seen, gaps, "RULE-SYS2-IS-SPECIFIC", "warning", req_id, "System requirement contains vague wording and should be made specific.", "Define the threshold or measurable condition explicitly.")

        if req_type == "software_requirement":
            if not upstream and not any(link.get("to") == req_id for link in trace_links):
                _append_gap(seen, gaps, "RULE-SWE1-HAS-UPSTREAM-SYS2", "error", req_id, "Software requirement has no upstream SYS.2 traceability.", "Link the software requirement to its upstream SYS.2 requirement.")

    for tc in test_cases:
        tc_id = str(tc.get("id") or "")
        verified = [str(item) for item in _as_list(tc.get("verifies")) if item]
        if verified:
            continue
        _append_gap(seen, gaps, "RULE-TEST-HAS-VERIFIED-REQ", "warning", tc_id, "Test case does not verify any requirements.", "Link the test case to the requirement(s) it verifies.")

    feature = record.get("feature") or {}
    if not requirements:
        _append_gap(seen, gaps, "RULE-FEATURE-HAS-REQUIREMENTS", "error", str(feature.get("id") or record.get("sample_id") or ""), "Feature has no linked requirements.", "Create at least one stakeholder or system requirement for the feature.")

    return gaps


def _append_gap(
    seen: set[tuple[str, str]],
    gaps: list[GapRecord],
    rule_id: str,
    severity: str,
    target_id: str,
    gap: str,
    recommendation: str,
) -> None:
    key = (rule_id, target_id)
    if key in seen:
        return
    seen.add(key)
    gaps.append({
        "rule_id": rule_id,
        "severity": severity,
        "target_id": target_id,
        "gap": gap,
        "recommendation": recommendation,
        "source": "derived",
    })


def _recommendation_for_rule(rule_id: str) -> str:
    return {
        "RULE-FEATURE-HAS-REQUIREMENTS": "Create linked stakeholder and system requirements for the feature.",
        "RULE-SYS2-HAS-UPSTREAM-STK": "Derive the requirement from stakeholder input or approved source evidence.",
        "RULE-SYS2-HAS-SOURCE-EVIDENCE": "Attach Confluence, code, or another approved source reference.",
        "RULE-SYS2-HAS-VERIFICATION": "Add verification criteria or link a SYS.5 qualification test.",
        "RULE-SYS2-IS-SPECIFIC": "Replace vague wording with a measurable threshold or explicit condition.",
        "RULE-SWE1-HAS-UPSTREAM-SYS2": "Link the software requirement to the upstream SYS.2 requirement.",
        "RULE-SAFETY-RELEVANCE-CLASSIFIED": "Classify safety relevance before approval.",
        "RULE-SAFETY-HAS-FAULT-REACTION": "Define the required system reaction, degraded mode, warning, or safe behavior.",
        "RULE-SAFETY-REQUIRES-REVIEW": "Review the safety-relevant requirement before approval.",
        "RULE-REQ-HAS-SOURCE-EVIDENCE": "Add a source evidence reference.",
        "RULE-TEST-HAS-VERIFIED-REQ": "Link the test case to one or more verified requirements.",
    }.get(rule_id, "Review the traceability chain and add the missing evidence or link.")


def _process_steps(record: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = [_feature_context_step(record)]
    trace_links = [link for link in record.get("input_trace_links", []) if isinstance(link, dict)]

    for req in record.get("input_requirements", []):
        if not isinstance(req, dict):
            continue

        req_id = req.get("id")
        req_type = req.get("type")
        steps.append({
            "process_step": _step_name_for_requirement(req_type),
            "artifact_type": req_type,
            "aspice_process": str(req.get("aspice_process") or _requirement_process(req_type)),
            "input_artifact": _input_artifacts_for_requirement(req, record),
            "output_artifact": [_ref(req_id, req.get("statement") or req_id, kind=req_type)],
            "trace_link": _trace_links_for_requirement(req, trace_links),
            "evidence_source": [
                _ref(item.get("source_id"), item.get("title") or item.get("source_type"), kind=item.get("source_type"))
                for item in _as_list(req.get("source_evidence"))
                if isinstance(item, dict) and item.get("source_id")
            ] or _feature_context_step(record)["evidence_source"],
            "validation_rule": _requirement_validation_rules(req),
        })

    for tc in record.get("input_test_cases", []):
        if not isinstance(tc, dict):
            continue
        steps.append(_test_case_step(tc))

    return steps


def build_feature_scenario(record: dict[str, Any]) -> ScenarioRecord:
    feature = record.get("feature") or {}
    feature_id = str(feature.get("id") or record.get("sample_id") or "FEATURE-UNKNOWN")
    scenario_id = str(record.get("sample_id") or feature_id)

    scenario = {
        "schema_version": "0.1",
        "record_type": "aspice_scenario_record",
        "scenario_id": scenario_id,
        "scenario": str(feature.get("name") or scenario_id),
        "scenario_type": str(record.get("scenario_type") or "feature_lifecycle"),
        "feature_id": feature_id,
        "feature_name": str(feature.get("name") or ""),
        "feature_description": str(feature.get("description") or ""),
        "feature_owner": str(feature.get("feature_owner") or ""),
        "safety_relevance": str(feature.get("safety_relevance") or ""),
        "status": str(feature.get("status") or ""),
        "source_evidence": [
            _ref(item.get("source_id"), item.get("title") or item.get("source_type"), kind=item.get("source_type"))
            for item in record.get("input_evidence", [])
            if isinstance(item, dict) and item.get("source_id")
        ],
        "behavior_scenarios": _behavior_scenarios(record),
        "process_steps": _process_steps(record),
        "gaps": _gap_records(record),
    }

    scenario["recommendations"] = _unique_recommendations(scenario["gaps"])
    scenario["trace_links"] = _collect_trace_links(record)
    scenario["requirements"] = _normalize_requirements(record)
    scenario["test_cases"] = _normalize_test_cases(record)

    return scenario


def _collect_trace_links(record: dict[str, Any]) -> list[dict[str, Any]]:
    links = [link for link in record.get("input_trace_links", []) if isinstance(link, dict)]
    for tc in record.get("input_test_cases", []):
        if not isinstance(tc, dict):
            continue
        tc_id = tc.get("id")
        for req_id in _as_list(tc.get("verifies")):
            if not req_id:
                continue
            links.append({
                "id": f"synthetic-{req_id}-{tc_id}",
                "from": str(req_id),
                "to": str(tc_id),
                "link_type": "verified_by",
            })
    return links


def _normalize_requirements(record: dict[str, Any]) -> list[dict[str, Any]]:
    requirements: list[dict[str, Any]] = []
    for req in record.get("input_requirements", []):
        if not isinstance(req, dict):
            continue
        requirements.append({
            "id": str(req.get("id") or ""),
            "feature_id": str(req.get("feature_id") or ""),
            "type": str(req.get("type") or ""),
            "aspice_process": str(req.get("aspice_process") or _requirement_process(req.get("type"))),
            "statement": str(req.get("statement") or ""),
            "derived_from": [str(item) for item in _as_list(req.get("derived_from")) if item],
            "source_evidence": [str(item) for item in _as_list(req.get("source_evidence")) if item],
            "verification_criteria": str(req.get("verification_criteria") or ""),
            "safety_relevance": str(req.get("safety_relevance") or ""),
            "review_state": str(req.get("review_state") or ""),
            "status": str(req.get("status") or ""),
        })
    return requirements


def _normalize_test_cases(record: dict[str, Any]) -> list[dict[str, Any]]:
    tests: list[dict[str, Any]] = []
    for tc in record.get("input_test_cases", []):
        if not isinstance(tc, dict):
            continue
        tests.append({
            "id": str(tc.get("id") or ""),
            "feature_id": str(tc.get("feature_id") or ""),
            "type": str(tc.get("type") or "test_case"),
            "aspice_process": str(tc.get("aspice_process") or "SYS.5"),
            "title": str(tc.get("title") or ""),
            "verifies": [str(item) for item in _as_list(tc.get("verifies")) if item],
            "test_steps": [str(item) for item in _as_list(tc.get("test_steps")) if item],
            "expected_result": str(tc.get("expected_result") or ""),
        })
    return tests


def _unique_recommendations(gaps: list[GapRecord]) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()
    for gap in gaps:
        recommendation = str(gap.get("recommendation") or "").strip()
        if not recommendation or recommendation in seen:
            continue
        seen.add(recommendation)
        ordered.append(recommendation)
    return ordered


def load_records(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)

    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        return [record for record in payload["records"] if isinstance(record, dict)]

    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]

    raise ValueError("Unsupported dataset structure: expected a dict with records or a list of records.")


def build_scenario_records(records: list[dict[str, Any]]) -> list[ScenarioRecord]:
    return [build_feature_scenario(record) for record in records]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build normalized ASPICE scenario records from feature inspection data.")
    parser.add_argument("path", nargs="?", default=str(DEFAULT_JSON_PATH), help="Path to a feature inspection dataset JSON file.")
    parser.add_argument("--write-json", default="", help="Optional path for the generated scenario records JSON.")
    args = parser.parse_args()

    records = load_records(Path(args.path))
    scenarios = build_scenario_records(records)

    if args.write_json:
        output_path = Path(args.write_json)
        output_path.write_text(json.dumps(scenarios, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Scenario records written to: {output_path}")
        return

    print(json.dumps(scenarios, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()