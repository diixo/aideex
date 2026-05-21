# ASPICE Pipeline JSONL Dataset With Base Practices

This dataset is intended for testing an AI / rules-based pipeline for ASPICE lifecycle scenario modeling.

Each line is one scenario JSON object.

## Main Fields

- `sample_id`: unique scenario ID
- `scenario_type`: scenario category
- `title`: human-readable scenario title
- `aspice_version`: intentionally set to `configurable`; BP IDs are project-defined aliases
- `process_scope`: ASPICE processes involved
- `input_evidence`: source material such as Confluence text, code excerpts, change requests, or ALM baselines
- `expected_pipeline_steps`: expected pipeline actions
- `base_practice_refs`: Base Practice IDs covered by each pipeline step
- `expected_artifacts`: expected generated artifacts
- `created_by_base_practice`: BP responsible for creating the artifact
- `expected_trace_links`: expected links between artifacts
- `base_practice_coverage`: expected BP coverage status
- `information_items`: ASPICE-style information/evidence items represented by artifacts
- `assessment_indicators`: simplified assessment indicators
- `expected_gap_checks`: expected validation results

## Suggested Pipeline

```text
load_jsonl
  → parse input_evidence
  → execute expected_pipeline_steps
  → generate candidate artifacts
  → generate trace links
  → map artifacts to information_items
  → evaluate base_practice_coverage
  → run gap checks
  → compare actual vs expected outputs
```

## Important Note About BP IDs

The BP IDs in this dataset are intentionally readable aliases, for example:

```text
SYS.2.BP_SPECIFY_SYSTEM_REQUIREMENTS
SYS.2.BP_ANALYZE_REQUIREMENTS
SYS.2.BP_ESTABLISH_TRACEABILITY
```

This avoids hard-coding a specific ASPICE version or exact numbering scheme.  
In a production implementation, you can replace these aliases with official or internal BP IDs.

## Basic Python Loading Example

```python
import json
from pathlib import Path

path = Path("aspice_pipeline_dataset_with_base_practices.jsonl")

records = []
with path.open("r", encoding="utf-8") as f:
    for line in f:
        records.append(json.loads(line))

print(len(records))
print(records[0]["sample_id"])
print(records[0]["base_practice_coverage"][0])
```

## Example BP Coverage Check

```python
def summarize_bp_coverage(record):
    result = {}
    for bp in record.get("base_practice_coverage", []):
        result[bp["bp_id"]] = bp["coverage_status"]
    return result

for record in records:
    print(record["sample_id"], summarize_bp_coverage(record))
```
