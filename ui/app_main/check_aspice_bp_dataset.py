import json
from pathlib import Path
from collections import Counter

DATASET_PATH = Path("aspice_pipeline_dataset_with_base_practices.jsonl")

def load_jsonl(path: Path):
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)

def validate_record(record: dict) -> list[str]:
    errors = []

    artifact_ids = {a["id"] for a in record.get("expected_artifacts", []) if "id" in a}
    step_ids = {s["step_id"] for s in record.get("expected_pipeline_steps", []) if "step_id" in s}

    for step in record.get("expected_pipeline_steps", []):
        if not step.get("base_practice_refs"):
            errors.append(f"{record['sample_id']}: step {step.get('step_id')} has no base_practice_refs")

    for bp in record.get("base_practice_coverage", []):
        for step_id in bp.get("covered_by_steps", []):
            if step_id not in step_ids:
                errors.append(f"{record['sample_id']}: BP {bp['bp_id']} refers to unknown step {step_id}")

    for link in record.get("expected_trace_links", []):
        # Trace links may refer to baseline artifacts not listed in expected_artifacts.
        # Therefore this checker only warns when both sides are unknown and no baseline input exists.
        if link.get("from") not in artifact_ids and link.get("to") not in artifact_ids:
            errors.append(f"{record['sample_id']}: trace link {link.get('id')} has both endpoints outside expected_artifacts")

    return errors

def main():
    records = list(load_jsonl(DATASET_PATH))
    print(f"Loaded records: {len(records)}")

    status_counter = Counter()
    all_errors = []

    for record in records:
        for bp in record.get("base_practice_coverage", []):
            status_counter[bp.get("coverage_status", "unknown")] += 1
        all_errors.extend(validate_record(record))

    print("BP coverage status summary:")
    for status, count in status_counter.items():
        print(f"  {status}: {count}")

    if all_errors:
        print("\nValidation warnings/errors:")
        for err in all_errors:
            print(f"  - {err}")
    else:
        print("\nNo structural validation errors found.")

if __name__ == "__main__":
    main()
