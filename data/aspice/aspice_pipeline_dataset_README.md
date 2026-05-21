# ASPICE Pipeline JSONL Dataset

This dataset is intended for testing an AI / rules-based pipeline for ASPICE lifecycle scenario modeling.

Each line is one JSON object with:

- `sample_id`: unique scenario ID
- `scenario_type`: type of scenario
- `title`: human-readable scenario title
- `process_scope`: ASPICE processes involved
- `input_evidence`: source material such as Confluence text, code excerpts, change requests, or ALM baselines
- `expected_pipeline_steps`: expected pipeline actions
- `expected_artifacts`: expected generated artifacts
- `expected_trace_links`: expected links between artifacts
- `expected_gap_checks`: expected validation results

Suggested pipeline stages:

```text
load_jsonl
  → parse input_evidence
  → generate candidate artifacts
  → generate trace links
  → run gap checks
  → compare with expected_artifacts / expected_trace_links / expected_gap_checks
```

Basic Python loading example:

```python
import json
from pathlib import Path

path = Path("aspice_pipeline_dataset.jsonl")

records = []
with path.open("r", encoding="utf-8") as f:
    for line in f:
        records.append(json.loads(line))

print(len(records))
print(records[0]["sample_id"])
```
