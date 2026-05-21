# Constructor for ASPICE Scenario Modeling

## Core Idea

It is possible to build a **scenario constructor for ASPICE modeling**.

The goal is not just to create a diagram editor, but to model how a specific engineering artifact moves through the **ASPICE V-model** and how it is connected to requirements, architecture, tests, defects, risks, evidence, and reviews.

A better product positioning would be:

> Scenario-based ASPICE lifecycle modeling and gap analysis tool.

Or in Russian:

> Конструктор сценариев жизненного цикла ASPICE-артефактов с проверкой traceability, evidence, verification и process gaps.

---

## Why This Is Useful

ASPICE is based on a process model. It describes engineering processes, expected outcomes, work products, and evidence.

A scenario constructor can help simulate and validate how a specific artifact passes through the ASPICE lifecycle.

For example:

```text
Customer Need
  → Stakeholder Requirement
  → SYS.2 System Requirement
  → SYS.3 System Architecture Element
  → SWE.1 Software Requirement
  → SWE.2 Software Architecture Element
  → SWE.3 Detailed Design / Unit
  → SWE.4 Unit Verification
  → SWE.5 Software Integration Test
  → SYS.5 System Qualification Test
```

The constructor should not only draw this chain, but also check whether the scenario is complete and compliant enough.

---

## What the System Should Check

For each modeled scenario, the system can verify:

```text
Is there traceability?
Is there a verification link?
Is there source evidence?
Is there review / approval status?
Is there consistency between artifacts?
Is there test evidence?
Is there a gap between SYS and SWE levels?
```

This makes the constructor useful not only for visualization, but also for **ASPICE-oriented gap analysis**.

---

## Three-Layer Model

The scenario constructor can be organized into three layers:

```text
1. Process Model Layer
2. Scenario Layer
3. Compliance / Gap Check Layer
```

---

## 1. Process Model Layer

The **Process Model Layer** describes the ASPICE process template.

Example for `SYS.2`:

```json
{
  "process": "SYS.2",
  "name": "System Requirements Analysis",
  "input_items": [
    "stakeholder_requirements",
    "system_context",
    "constraints"
  ],
  "output_items": [
    "system_requirements",
    "system_requirements_traceability",
    "verification_criteria"
  ],
  "required_checks": [
    "consistency_with_stakeholder_requirements",
    "verifiability",
    "traceability",
    "review_status"
  ]
}
```

This layer defines what must exist for a process to be considered complete enough.

---

## 2. Scenario Layer

The **Scenario Layer** describes a concrete lifecycle scenario.

Example:

```json
{
  "scenario_id": "SCN-LOW-BATTERY-001",
  "title": "Low battery warning requirement lifecycle",
  "steps": [
    {
      "step": 1,
      "artifact_type": "stakeholder_requirement",
      "action": "capture_need",
      "output": "STK-REQ-001"
    },
    {
      "step": 2,
      "artifact_type": "system_requirement",
      "aspice_process": "SYS.2",
      "action": "derive_system_requirement",
      "input": ["STK-REQ-001"],
      "output": "SYS-REQ-001"
    },
    {
      "step": 3,
      "artifact_type": "test_case",
      "aspice_process": "SYS.5",
      "action": "define_qualification_test",
      "input": ["SYS-REQ-001"],
      "output": "SYS-TC-001"
    }
  ]
}
```

This layer describes how a specific requirement, defect, test, risk, or change request moves through the process.

---

## 3. Compliance / Gap Check Layer

The **Compliance / Gap Check Layer** evaluates the scenario against rules.

Example:

```json
{
  "scenario_id": "SCN-LOW-BATTERY-001",
  "checks": [
    {
      "rule": "Every SYS.2 requirement must be derived from stakeholder input",
      "status": "passed"
    },
    {
      "rule": "Every SYS.2 requirement must have verification criteria",
      "status": "failed",
      "gap": "SYS-REQ-001 has no verification criteria"
    },
    {
      "rule": "Every system requirement must be linked to a system qualification test",
      "status": "warning",
      "gap": "SYS-REQ-001 has no linked SYS.5 test case"
    }
  ]
}
```

This layer allows the tool to detect missing links, missing evidence, missing reviews, weak verification, or incomplete traceability.

---

## Connection With an AI Pipeline

For an AI-based requirements generation pipeline, the scenario can act as a controlling template.

Instead of simply generating requirement text, the AI can follow a process-oriented path:

```text
1. Find evidence in Confluence / code
2. Extract candidate stakeholder need
3. Generate SYS.2 requirement
4. Check abstraction level
5. Link requirement to source evidence
6. Suggest verification criteria
7. Suggest test case
8. Detect gaps
9. Prepare an ALM-ready work item
```

This means the scenario becomes a **workflow template for an AI agent**.

The AI does not just create text. It creates an **ALM / ASPICE-compatible chain of artifacts**.

---

## Possible Scenario Templates

The MVP can include several predefined scenario templates:

```text
Requirement derivation scenario
Change request impact scenario
Defect-to-requirement scenario
Test failure scenario
Safety-relevant feature scenario
Architecture allocation scenario
Release readiness scenario
Traceability gap scenario
```

Example for change request impact analysis:

```text
Change Request
  → impacted stakeholder requirements
  → impacted SYS.2 requirements
  → impacted SWE.1 requirements
  → impacted tests
  → impacted release
  → required re-review
```

This is closer to ALM / ASPICE lifecycle logic than a simple task workflow.

---

## Difference From a Normal Workflow Constructor

A normal workflow constructor usually describes states:

```text
Draft → Review → Approved
```

An ASPICE scenario constructor should describe engineering completeness:

```text
This SYS.2 requirement is valid only if:
- it is derived from stakeholder input
- it has rationale or evidence
- it is verifiable
- it is reviewed
- it is traceable downstream
- it is covered by verification
- changes are controlled
```

So the constructor should validate not only workflow status, but also the engineering quality and completeness of artifacts.

---

## MVP Data Model

A minimal MVP can include the following entities:

```text
Scenario
Process Step
Artifact Type
ASPICE Process
Input Artifact
Output Artifact
Trace Link
Evidence Source
Validation Rule
Gap
Recommendation
```

---

## Main Artifact Types

The first version can support these artifact types:

```text
Stakeholder Requirement
System Requirement
System Architecture Element
Software Requirement
Software Architecture Element
Detailed Design Item
Code Evidence
Test Case
Test Run
Defect
Change Request
Risk
Release
```

---

## Example ALM / ASPICE Item

A generated item can look like this:

```json
{
  "type": "system_requirement",
  "aspice_process": "SYS.2",
  "id": "SYS-REQ-001",
  "statement": "The system shall display a low-battery warning when the battery state of charge is below the configured threshold.",
  "derived_from": ["STK-REQ-001"],
  "linked_architecture": ["SYS-ARCH-001"],
  "verified_by": ["SYS-TC-001"],
  "status": "Draft",
  "review_state": "Not reviewed",
  "safety_relevance": "TBD",
  "evidence_sources": [
    "confluence: battery warning feature description",
    "code: display warning handler"
  ]
}
```

This is more useful than a plain generated requirement because it contains:

```text
artifact type
ASPICE process
V-model level
source evidence
traceability
verification link
review state
status
safety relevance
```

---

## Product-Level Summary

A constructor for ASPICE scenario modeling can become the central part of a larger product:

```text
ProcessGrid / Codebeamer-like model
  + AI requirements generation pipeline
  + ASPICE gap checker
  + traceability validation
  + ALM-ready work item generation
```

The tool should not be positioned as a simple diagram editor.

A stronger positioning is:

> Scenario-based ASPICE lifecycle modeling and gap analysis tool.

Or:

> ASPICE scenario constructor for lifecycle artifact modeling, traceability validation, and AI-assisted requirements generation.
