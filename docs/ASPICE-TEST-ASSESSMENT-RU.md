# ASPICE Test Assessment для проекта 10 (AMBER)

## Цель

Сформировать единый контракт для проверки соответствия ASPICE по тестовым артефактам проекта Codebeamer:

- требования -> тест-кейсы -> тест-раны
- покрытие по уровням SYS/SWE
- анализ пробелов (gaps)
- поддержка двух видов сценариев поведения


## 1. Источник данных

База для анализа:

- corpus/10/.sync/tree.json
- corpus/10/codebeamer.json
- API: GET /codebeamer/tree-view?project_id=10


## 2. Нужные тестовые трекеры (project_id=10)

Выделяются по type_name: Testcase, Testset, Testrun, Testconf.

| tracker_id | tracker_name | type_name | ASPICE lane |
|---|---|---|---|
| 87637 | System Verification Test Cases | Testcase | SYS.5 |
| 88271 | Acceptance Testing Test Cases | Testcase | SYS.5 |
| 87749 | System Integration Test Cases | Testcase | SYS.4 |
| 87848 | Software Verification Test Cases | Testcase | SWE.4 |
| 87934 | Software Integration Verification Test Cases | Testcase | SWE.5 |
| 88020 | Software Component Verification Test Cases | Testcase | SWE.4 |
| 146738 | CySec Validation Test Cases | Testcase | SEC lane (mapped to SYS/SWE by context) |
| 88370 | Test Sets | Testset | Execution scope |
| 88425 | Test Runs | Testrun | Verification evidence |
| 19102 | Test Configurations | Testconf | Reproducibility evidence |

Примечание: трекер Defects (Bug) может участвовать как дополнительный evidence-path, но не входит в минимальный тестовый набор.


## 3. Два вида behaviour (dual-view)

### 3.1 Feature Behavior Scenario

Отвечает на вопрос: как система должна себя вести.

Структура шага:

- trigger
- preconditions
- system_action
- expected_output
- timing_constraints
- exceptions

### 3.2 ASPICE Lifecycle Scenario

Отвечает на вопрос: доказано ли engineering-прохождение артефактов.

Базовая цепочка:

- Feature behavior
- Stakeholder/System/Software requirement
- Test case
- Test run
- (optional) defect/change


## 4. Нормализация тестовых сущностей

### 4.1 Testcase category

Правило определения process_area:

- tracker_id in {87637, 88271} -> SYS.5
- tracker_id in {87749} -> SYS.4
- tracker_id in {87848, 88020} -> SWE.4
- tracker_id in {87934} -> SWE.5
- tracker_id in {146738} -> SEC

### 4.2 Execution evidence

Test run считается валидным evidence, если:

- имеет связанный test case
- имеет статус результата (pass/fail/blocked/inconclusive)
- имеет время выполнения или завершения


## 5. Contract endpoint (proposal)

Endpoint:

- GET /codebeamer/assessment/test-coverage?project_id=10

Параметры:

- project_id (required)
- include_security (optional, default=true)
- freshness_days (optional, default=90)
- by (optional: feature | process | tracker, default=feature)

Ответ:

{
  "ok": true,
  "project_id": 10,
  "project_name": "AMBER",
  "generated_at": "2026-06-08T10:00:00Z",
  "summary": {
    "requirements_total": 0,
    "requirements_with_tests": 0,
    "requirements_with_runs": 0,
    "tests_total": 0,
    "tests_with_runs": 0,
    "runs_total": 0,
    "runs_passed": 0,
    "runs_failed": 0,
    "coverage_ratio": 0.0,
    "execution_ratio": 0.0,
    "pass_rate": 0.0,
    "freshness_ratio": 0.0,
    "assessment_score": 0.0
  },
  "process_breakdown": [
    {
      "process_area": "SYS.5",
      "requirements_total": 0,
      "requirements_with_tests": 0,
      "requirements_with_runs": 0,
      "tests_total": 0,
      "runs_total": 0,
      "pass_rate": 0.0
    }
  ],
  "entities": [
    {
      "entity_id": "FEAT-OR-REQ-001",
      "entity_type": "feature",
      "behavior_links": {
        "behavior_steps": ["BHV-001", "BHV-002"],
        "lifecycle_links": ["REQ-100", "TC-200", "TR-300"]
      },
      "metrics": {
        "coverage": 0.0,
        "execution": 0.0,
        "pass_rate": 0.0,
        "freshness": 0.0,
        "score": 0.0
      },
      "gaps": [
        {
          "gap_code": "REQ_NO_TESTCASE",
          "severity": "error",
          "process_area": "SYS.5",
          "target_id": "REQ-100",
          "message": "Requirement has no linked test case."
        }
      ]
    }
  ],
  "gaps": [
    {
      "gap_code": "TESTCASE_NO_RUN",
      "severity": "warning",
      "process_area": "SWE.4",
      "target_id": "TC-200",
      "message": "Test case has no execution evidence in freshness window."
    }
  ]
}


## 6. JSON Schema (MVP, draft)

{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "aspice-test-assessment-report.schema.json",
  "title": "ASPICE Test Assessment Report",
  "type": "object",
  "required": ["ok", "project_id", "summary", "process_breakdown", "entities", "gaps"],
  "properties": {
    "ok": {"type": "boolean"},
    "project_id": {"type": "integer"},
    "project_name": {"type": "string"},
    "generated_at": {"type": "string"},
    "summary": {
      "type": "object",
      "required": [
        "requirements_total", "requirements_with_tests", "requirements_with_runs",
        "tests_total", "tests_with_runs", "runs_total", "runs_passed", "runs_failed",
        "coverage_ratio", "execution_ratio", "pass_rate", "freshness_ratio", "assessment_score"
      ],
      "properties": {
        "requirements_total": {"type": "integer", "minimum": 0},
        "requirements_with_tests": {"type": "integer", "minimum": 0},
        "requirements_with_runs": {"type": "integer", "minimum": 0},
        "tests_total": {"type": "integer", "minimum": 0},
        "tests_with_runs": {"type": "integer", "minimum": 0},
        "runs_total": {"type": "integer", "minimum": 0},
        "runs_passed": {"type": "integer", "minimum": 0},
        "runs_failed": {"type": "integer", "minimum": 0},
        "coverage_ratio": {"type": "number", "minimum": 0, "maximum": 1},
        "execution_ratio": {"type": "number", "minimum": 0, "maximum": 1},
        "pass_rate": {"type": "number", "minimum": 0, "maximum": 1},
        "freshness_ratio": {"type": "number", "minimum": 0, "maximum": 1},
        "assessment_score": {"type": "number", "minimum": 0, "maximum": 1}
      }
    },
    "process_breakdown": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["process_area", "requirements_total", "requirements_with_tests", "requirements_with_runs", "tests_total", "runs_total", "pass_rate"],
        "properties": {
          "process_area": {"type": "string", "enum": ["SYS.4", "SYS.5", "SWE.4", "SWE.5", "SEC"]},
          "requirements_total": {"type": "integer", "minimum": 0},
          "requirements_with_tests": {"type": "integer", "minimum": 0},
          "requirements_with_runs": {"type": "integer", "minimum": 0},
          "tests_total": {"type": "integer", "minimum": 0},
          "runs_total": {"type": "integer", "minimum": 0},
          "pass_rate": {"type": "number", "minimum": 0, "maximum": 1}
        }
      }
    },
    "entities": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["entity_id", "entity_type", "metrics", "gaps"],
        "properties": {
          "entity_id": {"type": "string"},
          "entity_type": {"type": "string", "enum": ["feature", "requirement"]},
          "behavior_links": {
            "type": "object",
            "properties": {
              "behavior_steps": {"type": "array", "items": {"type": "string"}},
              "lifecycle_links": {"type": "array", "items": {"type": "string"}}
            }
          },
          "metrics": {
            "type": "object",
            "required": ["coverage", "execution", "pass_rate", "freshness", "score"],
            "properties": {
              "coverage": {"type": "number", "minimum": 0, "maximum": 1},
              "execution": {"type": "number", "minimum": 0, "maximum": 1},
              "pass_rate": {"type": "number", "minimum": 0, "maximum": 1},
              "freshness": {"type": "number", "minimum": 0, "maximum": 1},
              "score": {"type": "number", "minimum": 0, "maximum": 1}
            }
          },
          "gaps": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["gap_code", "severity", "process_area", "target_id", "message"],
              "properties": {
                "gap_code": {"type": "string"},
                "severity": {"type": "string", "enum": ["error", "warning", "info"]},
                "process_area": {"type": "string", "enum": ["SYS.4", "SYS.5", "SWE.4", "SWE.5", "SEC"]},
                "target_id": {"type": "string"},
                "message": {"type": "string"}
              }
            }
          }
        }
      }
    },
    "gaps": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["gap_code", "severity", "process_area", "target_id", "message"],
        "properties": {
          "gap_code": {"type": "string"},
          "severity": {"type": "string", "enum": ["error", "warning", "info"]},
          "process_area": {"type": "string", "enum": ["SYS.4", "SYS.5", "SWE.4", "SWE.5", "SEC"]},
          "target_id": {"type": "string"},
          "message": {"type": "string"}
        }
      }
    }
  }
}


## 7. Gap rules (MVP)

### 7.1 Core traceability

- REQ_NO_TESTCASE (error): requirement не связан ни с одним test case.
- REQ_NO_RUN_EVIDENCE (warning): requirement связан с test case, но нет test run.
- TESTCASE_NO_REQUIREMENT (warning): test case не связан с requirement.
- TESTCASE_NO_RUN (warning): test case не имеет execution evidence.

### 7.2 Execution quality

- FAILED_NO_DEFECT (warning): fail run без связанного defect/change.
- STALE_EVIDENCE (warning): последний run старше freshness_days.
- LOW_PASS_RATE (warning): pass_rate ниже порога (например 0.8).

### 7.3 Dual-behavior consistency

- BEHAVIOR_NO_REQUIREMENT (error): шаг поведения не покрыт requirement.
- BEHAVIOR_NO_TEST (warning): шаг поведения покрыт requirement, но нет теста.
- BEHAVIOR_NO_EXECUTION (warning): тест есть, но нет свежего test run.


## 8. Assessment score

Итоговый score (0..1):

score = 0.35 * coverage + 0.25 * execution + 0.20 * pass_rate + 0.20 * freshness

где:

- coverage = requirements_with_tests / requirements_total
- execution = requirements_with_runs / requirements_total
- pass_rate = runs_passed / max(runs_total, 1)
- freshness = fresh_runs / max(runs_total, 1)


## 9. Фичи для продукта (roadmap)

1) Dual View
- Behavior View (продуктовое поведение)
- ASPICE View (процессная трассировка и evidence)

2) Process Heatmap
- SYS.4/SYS.5/SWE.4/SWE.5/SEC по coverage и execution

3) Gap Explorer
- фильтры по severity, process_area, tracker, feature

4) Evidence Timeline
- динамика test runs и деградация freshness

5) Audit Export
- JSON/CSV пакет для assessment и review

6) Rule Profiles
- strict/normal/light профили порогов


## 10. Минимальный шаг внедрения

1. Добавить endpoint /codebeamer/assessment/test-coverage.
2. Взять вход: project_id, freshness_days.
3. Нормализовать test trackers по таблице mapping.
4. Построить requirement->testcase->testrun цепочки.
5. Вернуть report по схеме из раздела 6.
6. Подключить выдачу в UI как новый ASPICE Assessment блок.
