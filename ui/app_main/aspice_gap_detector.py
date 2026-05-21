import json
from pathlib import Path
from typing import Any


DATASET_PATH = Path("aspice_pipeline_dataset_with_base_practices.jsonl")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    with path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue

            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_no}: {exc}") from exc

    return records


def collect_artifacts(record: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        artifact["id"]: artifact
        for artifact in record.get("expected_artifacts", [])
        if "id" in artifact
    }


def collect_trace_links(record: dict[str, Any]) -> list[dict[str, Any]]:
    links = []

    for link in record.get("expected_trace_links", []):
        links.append(link)

    for artifact in record.get("expected_artifacts", []):
        if artifact.get("type") == "trace_link":
            links.append(artifact)

    return links


def has_outgoing_link(trace_links: list[dict[str, Any]], artifact_id: str, link_type: str | None = None) -> bool:
    for link in trace_links:
        if link.get("from") != artifact_id:
            continue
        if link_type is None or link.get("link_type") == link_type:
            return True
    return False


def has_incoming_link(trace_links: list[dict[str, Any]], artifact_id: str, link_type: str | None = None) -> bool:
    for link in trace_links:
        if link.get("to") != artifact_id:
            continue
        if link_type is None or link.get("link_type") == link_type:
            return True
    return False


def detect_base_practice_gaps(record: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []

    for bp in record.get("base_practice_coverage", []):
        status = bp.get("coverage_status")

        if status in {"not_covered", "missing"}:
            gaps.append({
                "severity": "error",
                "category": "base_practice",
                "bp_id": bp.get("bp_id"),
                "message": bp.get("gap") or f"Base Practice {bp.get('bp_id')} is not covered.",
            })

        elif status == "partially_covered":
            gaps.append({
                "severity": "warning",
                "category": "base_practice",
                "bp_id": bp.get("bp_id"),
                "message": bp.get("gap") or f"Base Practice {bp.get('bp_id')} is only partially covered.",
            })

    return gaps


def detect_information_item_gaps(record: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []

    for item in record.get("information_items", []):
        represented_by = item.get("represented_by", [])

        if not represented_by:
            gaps.append({
                "severity": "error",
                "category": "information_item",
                "item_id": item.get("item_id"),
                "message": f"Information item '{item.get('name')}' is missing represented artifacts.",
            })

    return gaps


def detect_requirement_gaps(record: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []
    artifacts = collect_artifacts(record)
    trace_links = collect_trace_links(record)

    for artifact_id, artifact in artifacts.items():
        artifact_type = artifact.get("type")

        if artifact_type not in {"system_requirement", "software_requirement"}:
            continue

        source_evidence = artifact.get("source_evidence", [])
        if not source_evidence:
            gaps.append({
                "severity": "error",
                "category": "evidence",
                "artifact_id": artifact_id,
                "message": f"{artifact_id} has no source evidence.",
            })

        if artifact_type == "system_requirement":
            derived_from = artifact.get("derived_from", [])
            if not derived_from and not has_incoming_link(trace_links, artifact_id):
                gaps.append({
                    "severity": "error",
                    "category": "traceability",
                    "artifact_id": artifact_id,
                    "message": f"{artifact_id} has no upstream stakeholder/source traceability.",
                })

            verification_criteria = artifact.get("verification_criteria", "")
            has_test_link = has_outgoing_link(trace_links, artifact_id, "verified_by")
            if not verification_criteria and not has_test_link:
                gaps.append({
                    "severity": "error",
                    "category": "verification",
                    "artifact_id": artifact_id,
                    "message": f"{artifact_id} has no verification criteria and no linked test case.",
                })

            safety_relevance = artifact.get("safety_relevance")
            if safety_relevance == "TBD":
                gaps.append({
                    "severity": "warning",
                    "category": "safety",
                    "artifact_id": artifact_id,
                    "message": f"{artifact_id} safety relevance is TBD.",
                })

            if safety_relevance == "Yes":
                review_state = artifact.get("review_state")
                if review_state != "Reviewed":
                    gaps.append({
                        "severity": "warning",
                        "category": "review",
                        "artifact_id": artifact_id,
                        "message": f"{artifact_id} is safety-relevant but review_state is '{review_state}'.",
                    })

        if artifact_type == "software_requirement":
            derived_from = artifact.get("derived_from", [])
            if not derived_from and not has_incoming_link(trace_links, artifact_id):
                gaps.append({
                    "severity": "error",
                    "category": "traceability",
                    "artifact_id": artifact_id,
                    "message": f"{artifact_id} has no upstream system requirement traceability.",
                })

    return gaps


def detect_assessment_indicator_gaps(record: dict[str, Any]) -> list[dict[str, Any]]:
    gaps = []

    for indicator in record.get("assessment_indicators", []):
        status = indicator.get("status")

        if status == "missing":
            gaps.append({
                "severity": "error",
                "category": "assessment_indicator",
                "indicator_id": indicator.get("indicator_id"),
                "message": f"Assessment indicator {indicator.get('indicator_id')} is missing.",
            })

        elif status == "partially_covered":
            gaps.append({
                "severity": "warning",
                "category": "assessment_indicator",
                "indicator_id": indicator.get("indicator_id"),
                "message": f"Assessment indicator {indicator.get('indicator_id')} is partially covered.",
            })

    return gaps


def detect_expected_gap_checks(record: dict[str, Any]) -> list[dict[str, Any]]:
    """
    This function reads expected_gap_checks from the dataset.
    It is useful for test mode: you can compare your actual checker output
    against these expected labels.
    """
    gaps = []

    for check in record.get("expected_gap_checks", []):
        expected_status = check.get("expected_status")

        if expected_status == "failed":
            gaps.append({
                "severity": "error",
                "category": "expected_gap_check",
                "rule_id": check.get("rule_id"),
                "bp_id": check.get("base_practice_ref"),
                "message": check.get("expected_gap") or check.get("description"),
            })

        elif expected_status == "warning":
            gaps.append({
                "severity": "warning",
                "category": "expected_gap_check",
                "rule_id": check.get("rule_id"),
                "bp_id": check.get("base_practice_ref"),
                "message": check.get("expected_gap") or check.get("description"),
            })

    return gaps


def detect_gaps(record: dict[str, Any], include_expected_checks: bool = False) -> list[dict[str, Any]]:
    gaps = []

    gaps.extend(detect_base_practice_gaps(record))
    gaps.extend(detect_information_item_gaps(record))
    gaps.extend(detect_requirement_gaps(record))
    gaps.extend(detect_assessment_indicator_gaps(record))

    if include_expected_checks:
        gaps.extend(detect_expected_gap_checks(record))

    return gaps


def print_report(records: list[dict[str, Any]], include_expected_checks: bool = False) -> None:
    total_errors = 0
    total_warnings = 0

    for record in records:
        sample_id = record.get("sample_id")
        title = record.get("title")
        gaps = detect_gaps(record, include_expected_checks=include_expected_checks)

        errors = [g for g in gaps if g["severity"] == "error"]
        warnings = [g for g in gaps if g["severity"] == "warning"]

        total_errors += len(errors)
        total_warnings += len(warnings)

        print("=" * 100)
        print(f"{sample_id}: {title}")
        print(f"Errors: {len(errors)} | Warnings: {len(warnings)}")

        if not gaps:
            print("No gaps detected.")
            continue

        for gap in gaps:
            label = "ERROR" if gap["severity"] == "error" else "WARNING"
            target = gap.get("artifact_id") or gap.get("bp_id") or gap.get("item_id") or gap.get("indicator_id") or gap.get("rule_id") or "-"
            print(f"[{label}] {gap['category']} | {target}")
            print(f"  {gap['message']}")

    print("=" * 100)
    print(f"TOTAL: errors={total_errors}, warnings={total_warnings}")


def main() -> None:
    records = load_jsonl(DATASET_PATH)

    # with open("data.json", "w", encoding="utf-8") as f:
    #     items = list(load_jsonl(Path("aspice_pipeline_dataset.jsonl")))
    #     json.dump(items, f, ensure_ascii=False, indent=2)


    # include_expected_checks=False means:
    #   run only generic rule-based checks over the dataset structure.
    #
    # include_expected_checks=True means:
    #   also print gaps explicitly listed in expected_gap_checks.
    print_report(records, include_expected_checks=False)


if __name__ == "__main__":
    main()
