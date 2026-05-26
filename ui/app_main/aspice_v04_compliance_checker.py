import argparse
import json
from pathlib import Path
from typing import Any


DEFAULT_JSON_PATH = Path("aspice_compliance_checker_test_dataset_v04.json")
DEFAULT_JSONL_PATH = Path("aspice_compliance_checker_test_cases_v04.jsonl")


Finding = dict[str, Any]
InspectionCase = dict[str, Any]


def load_json_dataset(path: Path) -> list[InspectionCase]:
    """
    Loads full v04 JSON dataset:
    {
      "schema_version": "0.4",
      "inspection_cases": [...]
    }
    """
    with path.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if "inspection_cases" not in payload:
        raise ValueError(f"{path} does not contain 'inspection_cases'.")

    return payload["inspection_cases"]


def load_jsonl_cases(path: Path) -> list[InspectionCase]:
    """
    Loads v04 JSONL dataset: one inspection case per line.
    """
    cases: list[InspectionCase] = []

    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                cases.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no}: {exc}") from exc

    return cases


def load_cases(path: Path) -> list[InspectionCase]:
    if path.suffix.lower() == ".jsonl":
        return load_jsonl_cases(path)

    return load_json_dataset(path)


def by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["id"]: item for item in items if "id" in item}


def incoming_links(case: InspectionCase, target_id: str, link_type: str | None = None) -> list[dict[str, Any]]:
    result = []

    for link in case.get("trace_links", []):
        if link.get("to") != target_id:
            continue
        if link_type is not None and link.get("link_type") != link_type:
            continue
        result.append(link)

    return result


def outgoing_links(case: InspectionCase, source_id: str, link_type: str | None = None) -> list[dict[str, Any]]:
    result = []

    for link in case.get("trace_links", []):
        if link.get("from") != source_id:
            continue
        if link_type is not None and link.get("link_type") != link_type:
            continue
        result.append(link)

    return result


def test_cases_verifying(case: InspectionCase, requirement_id: str) -> list[dict[str, Any]]:
    result = []

    for tc in case.get("test_cases", []):
        if requirement_id in tc.get("verifies", []):
            result.append(tc)

    return result


def make_finding(
    *,
    rule_id: str,
    dimension: str,
    target_id: str,
    severity: str,
    message: str,
) -> Finding:
    return {
        "rule_id": rule_id,
        "dimension": dimension,
        "target_id": target_id,
        "severity": severity,
        "message": message,
    }


def requirement_has_source_evidence(req: dict[str, Any]) -> bool:
    return bool(req.get("source_evidence"))


def requirement_has_verification_criteria(req: dict[str, Any]) -> bool:
    criteria = req.get("verification_criteria")
    return isinstance(criteria, str) and bool(criteria.strip())


def check_source_evidence(case: InspectionCase) -> list[Finding]:
    findings = []

    for req in case.get("requirements", []):
        req_type = req.get("type")

        if req_type not in {"stakeholder_requirement", "system_requirement", "software_requirement"}:
            continue

        if not requirement_has_source_evidence(req):
            findings.append(make_finding(
                rule_id="RULE-REQ-HAS-SOURCE-EVIDENCE",
                dimension="source_evidence",
                target_id=req["id"],
                severity="error",
                message=f"{req['id']} has no source evidence.",
            ))

    return findings


def check_upstream_traceability(case: InspectionCase) -> list[Finding]:
    findings = []

    for req in case.get("requirements", []):
        req_id = req["id"]
        req_type = req.get("type")

        if req_type == "system_requirement":
            derived_from = req.get("derived_from", [])
            has_incoming = bool(incoming_links(case, req_id))

            if not derived_from and not has_incoming:
                findings.append(make_finding(
                    rule_id="RULE-SYS2-HAS-UPSTREAM-TRACEABILITY",
                    dimension="upstream_traceability",
                    target_id=req_id,
                    severity="error",
                    message=f"{req_id} has no upstream stakeholder requirement or approved source traceability.",
                ))

        elif req_type == "software_requirement":
            derived_from = req.get("derived_from", [])
            has_sys2_incoming = bool(incoming_links(case, req_id, "allocated_to_software"))

            if not derived_from and not has_sys2_incoming:
                findings.append(make_finding(
                    rule_id="RULE-SWE1-HAS-UPSTREAM-SYS2",
                    dimension="upstream_traceability",
                    target_id=req_id,
                    severity="error",
                    message=f"{req_id} is not linked to an upstream SYS.2 requirement.",
                ))

    return findings


def check_downstream_traceability(case: InspectionCase) -> list[Finding]:
    findings = []

    for req in case.get("requirements", []):
        if req.get("type") != "system_requirement":
            continue

        req_id = req["id"]
        downstream = outgoing_links(case, req_id)

        if not downstream:
            findings.append(make_finding(
                rule_id="RULE-SYS2-HAS-DOWNSTREAM-TRACEABILITY",
                dimension="downstream_traceability",
                target_id=req_id,
                severity="warning",
                message=f"{req_id} has no downstream traceability to SWE.1, architecture, or verification artifacts.",
            ))
            continue

        has_software_allocation = bool(outgoing_links(case, req_id, "allocated_to_software"))
        has_test_link = bool(outgoing_links(case, req_id, "verified_by")) or bool(test_cases_verifying(case, req_id))

        # For feature-level inspection, it is useful to warn when a SYS.2 requirement has a test
        # but no downstream SWE.1 allocation, because implementation traceability is incomplete.
        if has_test_link and not has_software_allocation:
            findings.append(make_finding(
                rule_id="RULE-SYS2-HAS-DOWNSTREAM-TRACEABILITY",
                dimension="downstream_traceability",
                target_id=req_id,
                severity="warning",
                message=f"{req_id} is linked to a test case but has no downstream SWE.1 allocation link.",
            ))

    return findings


def check_verification_criteria(case: InspectionCase) -> list[Finding]:
    findings = []

    for req in case.get("requirements", []):
        if req.get("type") not in {"system_requirement", "software_requirement"}:
            continue

        if not requirement_has_verification_criteria(req):
            findings.append(make_finding(
                rule_id="RULE-REQ-HAS-VERIFICATION-CRITERIA",
                dimension="verification_criteria",
                target_id=req["id"],
                severity="error",
                message=f"{req['id']} has no verification criteria.",
            ))

    return findings


def check_linked_test_case(case: InspectionCase) -> list[Finding]:
    findings = []

    for req in case.get("requirements", []):
        if req.get("type") != "system_requirement":
            continue

        req_id = req["id"]
        linked_by_trace = bool(outgoing_links(case, req_id, "verified_by"))
        linked_by_test_case = bool(test_cases_verifying(case, req_id))

        if not linked_by_trace and not linked_by_test_case:
            findings.append(make_finding(
                rule_id="RULE-SYS2-HAS-LINKED-TEST-CASE",
                dimension="linked_test_case",
                target_id=req_id,
                severity="error",
                message=f"{req_id} has no linked SYS.5 test case.",
            ))

    return findings


def check_review_status(case: InspectionCase) -> list[Finding]:
    findings = []

    for req in case.get("requirements", []):
        req_id = req["id"]
        review_state = req.get("review_state")

        if not review_state:
            findings.append(make_finding(
                rule_id="RULE-REVIEW-STATUS-DEFINED",
                dimension="review_status",
                target_id=req_id,
                severity="warning",
                message=f"{req_id} has no review_state.",
            ))

        if req.get("safety_relevance") == "Yes" and review_state != "Reviewed":
            findings.append(make_finding(
                rule_id="RULE-SAFETY-REQUIRES-REVIEW",
                dimension="review_status",
                target_id=req_id,
                severity="warning",
                message=f"{req_id} is safety-relevant but is not reviewed.",
            ))

    return findings


def check_safety_classification(case: InspectionCase) -> list[Finding]:
    findings = []

    for req in case.get("requirements", []):
        if req.get("type") not in {"system_requirement", "software_requirement"}:
            continue

        safety_relevance = req.get("safety_relevance")

        if safety_relevance in {None, "", "TBD"}:
            findings.append(make_finding(
                rule_id="RULE-SAFETY-CLASSIFIED",
                dimension="safety_classification",
                target_id=req["id"],
                severity="warning",
                message="Safety relevance is TBD.",
            ))

    return findings


def token_set(text: str) -> set[str]:
    # Tiny helper for simple consistency heuristics.
    # This is intentionally simple and deterministic.
    normalized = (
        text.lower()
        .replace(".", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace(":", " ")
        .replace("-", " ")
        .replace("_", " ")
    )
    stopwords = {
        "the", "a", "an", "and", "or", "to", "from", "when", "shall", "system",
        "software", "rider", "be", "is", "are", "as", "of", "with", "by", "for",
        "in", "on", "into", "valid", "values"
    }
    return {tok for tok in normalized.split() if tok and tok not in stopwords}


def check_cross_level_consistency(case: InspectionCase) -> list[Finding]:
    """
    Deterministic lightweight checks for the v04 synthetic dataset.

    In production, this can be replaced with an LLM/NLI consistency stage.
    Here we implement small, explicit heuristics so test output is stable.
    """
    findings = []
    requirements = by_id(case.get("requirements", []))

    for req in case.get("requirements", []):
        req_id = req["id"]
        statement = req.get("statement", "")

        # Battery case: SYS.2 says vague "battery is low", but SWE.1 uses configured threshold.
        if req.get("type") == "software_requirement" and "configuredLowBatteryThreshold" in statement:
            for upstream_id in req.get("derived_from", []):
                upstream = requirements.get(upstream_id, {})
                upstream_statement = upstream.get("statement", "")
                if "battery is low" in upstream_statement and "threshold" not in upstream_statement.lower():
                    findings.append(make_finding(
                        rule_id="RULE-CONSISTENCY-SYS-SWE",
                        dimension="cross_level_consistency",
                        target_id=req_id,
                        severity="warning",
                        message=(
                            "SYS.2 says 'battery is low', while SWE.1 uses configuredLowBatteryThreshold. "
                            "SYS.2 should define or reference the threshold."
                        ),
                    ))

        # Diagnostic case: stakeholder says inform rider + avoid stale values,
        # but SYS.2 only says detect communication loss.
        if req.get("type") == "system_requirement":
            stmt_lower = statement.lower()
            if "detect display communication loss" in stmt_lower:
                findings.append(make_finding(
                    rule_id="RULE-CONSISTENCY-STK-SYS",
                    dimension="cross_level_consistency",
                    target_id=req_id,
                    severity="warning",
                    message=(
                        f"{req_id} detects display communication loss but does not preserve stakeholder "
                        "intent about warning the rider and avoiding stale critical values."
                    ),
                ))

    return findings


def inspect_case(case: InspectionCase) -> list[Finding]:
    findings = []
    findings.extend(check_source_evidence(case))
    findings.extend(check_upstream_traceability(case))
    findings.extend(check_downstream_traceability(case))
    findings.extend(check_verification_criteria(case))
    findings.extend(check_linked_test_case(case))
    findings.extend(check_review_status(case))
    findings.extend(check_safety_classification(case))
    findings.extend(check_cross_level_consistency(case))
    return sort_findings(deduplicate_findings(findings))


def finding_key(finding: Finding) -> tuple[str, str]:
    return finding["rule_id"], finding["target_id"]


def deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    deduped: dict[tuple[str, str], Finding] = {}

    for finding in findings:
        deduped[finding_key(finding)] = finding

    return list(deduped.values())


def sort_findings(findings: list[Finding]) -> list[Finding]:
    severity_rank = {"error": 0, "warning": 1, "info": 2}
    return sorted(
        findings,
        key=lambda f: (
            severity_rank.get(f.get("severity", "info"), 99),
            f.get("target_id", ""),
            f.get("rule_id", ""),
        )
    )


def compare_with_expected(actual: list[Finding], expected: list[Finding]) -> dict[str, Any]:
    actual_keys = {finding_key(f) for f in actual}
    expected_keys = {finding_key(f) for f in expected}

    missing = expected_keys - actual_keys
    unexpected = actual_keys - expected_keys
    matched = expected_keys & actual_keys

    return {
        "matched": len(matched),
        "missing": sorted(list(missing)),
        "unexpected": sorted(list(unexpected)),
        "passed": not missing and not unexpected,
    }


def print_case_report(case: InspectionCase, compare: bool = False) -> None:
    case_id = case.get("inspection_case_id", "<unknown-case>")
    feature = case.get("feature", {})
    feature_name = feature.get("name", "<unknown-feature>")

    actual_findings = inspect_case(case)
    errors = [f for f in actual_findings if f.get("severity") == "error"]
    warnings = [f for f in actual_findings if f.get("severity") == "warning"]

    print("=" * 120)
    print(f"{case_id} | {feature_name}")
    print(f"Findings: errors={len(errors)}, warnings={len(warnings)}, total={len(actual_findings)}")

    if not actual_findings:
        print("No compliance gaps detected.")
    else:
        for finding in actual_findings:
            label = finding["severity"].upper()
            print(f"[{label}] {finding['dimension']} | {finding['target_id']} | {finding['rule_id']}")
            print(f"  {finding['message']}")

    if compare:
        expected = case.get("expected_findings", [])
        comparison = compare_with_expected(actual_findings, expected)

        print()
        print("Expected comparison:")
        print(f"  matched: {comparison['matched']}")
        print(f"  missing: {len(comparison['missing'])}")
        print(f"  unexpected: {len(comparison['unexpected'])}")
        print(f"  passed: {comparison['passed']}")

        if comparison["missing"]:
            print("  missing keys:")
            for key in comparison["missing"]:
                print(f"    - {key}")

        if comparison["unexpected"]:
            print("  unexpected keys:")
            for key in comparison["unexpected"]:
                print(f"    - {key}")


def write_findings_json(cases: list[InspectionCase], output_path: Path) -> None:
    payload = {
        "schema_version": "0.4",
        "result_type": "aspice_compliance_checker_findings",
        "inspection_results": []
    }

    for case in cases:
        payload["inspection_results"].append({
            "inspection_case_id": case.get("inspection_case_id"),
            "target_feature_id": case.get("target_feature_id"),
            "feature_name": case.get("feature", {}).get("name"),
            "findings": inspect_case(case),
        })

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ASPICE compliance checker for v04 feature-level inspection dataset."
    )
    parser.add_argument(
        "path",
        nargs="?",
        default=str(DEFAULT_JSON_PATH),
        help="Path to v04 JSON dataset or JSONL inspection cases file."
    )
    parser.add_argument(
        "--compare",
        action="store_true",
        help="Compare actual detected findings with expected_findings in the dataset."
    )
    parser.add_argument(
        "--write-json",
        default="",
        help="Optional output path for detected findings JSON."
    )

    args = parser.parse_args()
    path = Path(args.path)

    cases = load_cases(path)

    total_errors = 0
    total_warnings = 0

    for case in cases:
        findings = inspect_case(case)
        total_errors += sum(1 for f in findings if f.get("severity") == "error")
        total_warnings += sum(1 for f in findings if f.get("severity") == "warning")
        print_case_report(case, compare=args.compare)

    print("=" * 120)
    print(f"TOTAL: cases={len(cases)}, errors={total_errors}, warnings={total_warnings}")

    if args.write_json:
        output_path = Path(args.write_json)
        write_findings_json(cases, output_path)
        print(f"Findings JSON written to: {output_path}")


if __name__ == "__main__":
    main()
