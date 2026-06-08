# ASPICE Notes

Этот файл — рабочая заметка по ASPICE assessment/compliance checker проекту.

## Цель

Собрать и уточнить фичи, сценарии, артефакты и правила проверки для Codebeamer-based ASPICE assessment/compliance checker.

## Контекст проекта

- Источник данных: Codebeamer
- Основные серверы: Django UI и FastAPI backend
- Фокус: ALM traceability, lifecycle артефактов, assessment/compliance, evidence, reporting

## Что будем фиксировать

- Идеи фич
- Процессные сценарии
- Модель артефактов
- Traceability rules
- Compliance checks
- Evidence and reporting
- Открытые вопросы и принятые решения

## Черновой список направлений

### 1. Ingest / Sync
- Pull данных из Codebeamer
- Синхронизация проекта, трекеров, items, полей и связей
- Построение локального представления проекта
- Версионирование снимков данных для сравнения между sync runs
- Detect stale / missing / partial data after sync
- Поддержка selective sync по project / tracker / item scope

### 2. Analyze / Model
- Построение normalized item model для требований, архитектуры, тестов и дефектов
- Классификация work items по типам ASPICE артефактов
- Извлечение upstream / downstream связей
- Автоматическое построение trace graph
- Поиск missing links, orphan items, duplicate items и inconsistent statuses
- Подготовка assessment facts для последующих compliance checks

### 3. Traceability
- Stakeholder Requirement → SYS.2 → SWE.1 → SWE.2 → SWE.3 → Test
- Upstream / downstream links
- Coverage matrix
- Gap detection
- Coverage by requirement level and by artifact type
- Trace path explanation for a selected item
- Impact analysis when a requirement changes
- Traceability matrix export

### 4. Compliance
- Проверка обязательных артефактов
- Проверка статусов review / approval
- Проверка полноты связей между уровнями
- Поиск нарушений process flow
- Rule-based compliance engine
- Process area specific checks
- Evidence of review / approval / verification
- Checklist view per project / tracker / item
- Severity scoring for non-compliance findings
- Compliance delta between baseline and current state

### 5. Evidence
- Сбор подтверждений проверки
- История изменений
- Audit-friendly snapshots
- Baseline support
- Immutable assessment snapshot per run
- Link findings to source items and source fields
- Exportable evidence bundle for audit / review
- Timeline of changes for each finding
- Retention of assessment history

### 6. Reporting
- Compliance dashboard
- Traceability matrix
- Gap reports
- Export для ревью и аудита
- Summary by project / tracker / process area
- Findings grouped by severity and status
- Drill-down from dashboard to source item
- PDF / HTML / JSON export options
- Executive summary and engineer-facing detail views
- Trend charts across runs

## Открытые вопросы

- Какие ASPICE process areas приоритетны в первой версии?
- Какие типы work items в Codebeamer считаются каноническими?
- Что именно считаем compliance: структурную полноту, процессную корректность или оба уровня?
- Нужен ли baseline / version comparison в первой итерации?

## Решения

- Пока пусто

## Как маппить features на `codebeamer.json`

В текущем проекте `codebeamer.json` не хранит отдельную сущность `feature` как first-class object. Вместо этого feature лучше трактовать как логический контейнер, который группирует:

- source evidence
- stakeholder requirement
- system requirement / SYS.2
- software requirement / SWE.1
- architecture / design items
- test cases / test runs
- compliance findings

### Каноническое соответствие

| ASPICE feature-level concept | Codebeamer representation |
|---|---|
| Feature | Логическая группа, обычно tracker folder / tracker set / parent work item |
| Stakeholder requirement | `item` с типом requirement или custom requirement type |
| SYS.2 | `item` с системным requirement type, связанный с stakeholder requirement через `subjects` / trace link |
| SWE.1 | `item` с software requirement type, связанный upstream с SYS.2 |
| Architecture / design | Отдельные `items` или tracker branch, привязанные к SWE.1 |
| Test case | `item` в test tracker, связанный downstream с SYS.2 / SWE.1 |
| Evidence | `items`, attachments, descriptions, linked docs, status history |
| Finding / compliance issue | Derived artifact, generated outside Codebeamer or stored as dedicated tracker item |

### Как это ложится на текущий `codebeamer.json`

- `trackers[]` = верхний уровень организации иерархии.
- `trackers[].items[]` = ссылки на work items, которые входят в этот tracker.
- `items[]` = полный payload work item, включая `subjects`.
- `subjects[]` = upstream traceability, то есть то, от чего item зависит.
- `item_map` = производная модель, где из `subjects` строится `upstream`, а обратной инверсией строится `downstream`.
- `tree.json` = иерархия tracker/folder, где можно восстановить feature grouping.

### Практический способ моделирования features

1. Feature создаётся как стабильная группа артефактов, а не как отдельный обязательный тип сущности.
2. У feature есть root requirement, обычно stakeholder requirement или SYS.2 depending on project convention.
3. Все дочерние артефакты связываются через trace links и/или `subjects`.
4. Для отчетности feature определяется по набору связанных items, а не только по одному tracker.
5. Для compliance checks feature — это scope unit, внутри которого проверяются coverage, review state, verification, and trace completeness.

### Рекомендуемая схема для проекта 24

Если в `corpus/24/codebeamer.json` уже есть иерархия SYS → SWE → Test, то feature лучше маппить так:

- Feature = корневая группа или parent tracker branch
- STK requirement = исходное пользовательское / stakeholder требование
- SYS.2 = системное требование, derived from STK
- SWE.1 = software requirement, derived from SYS.2
- Architecture / design = downstream artifacts from SWE.1
- Test case = verification artifact, linked from SYS.2 / SWE.1
- Evidence = source docs, code excerpts, review notes, test execution results

### Что важно для будущей автоматизации

- Нужен стабильный `feature_id`, который связывает все артефакты одной feature-группы.
- Если в Codebeamer нет явного feature tracker, его можно вычислять по root requirement + trace closure.
- Compliance engine должен работать не только по отдельным items, но и по feature closure.
- Отчёт должен уметь показывать feature-level health: coverage, traceability, verification, evidence, findings.

### Что можно извлекать автоматически

Да, конструктор сценариев может извлекать и нормализовать такие поля:

- `Scenario`
- `Process Step`
- `Artifact Type`
- `ASPICE Process`
- `Input Artifact`
- `Output Artifact`
- `Trace Link`
- `Evidence Source`
- `Validation Rule`
- `Gap`
- `Recommendation`

### Предлагаемая структура scenario record

```json
{
	"scenario_id": "SCN-LOW-BATTERY-001",
	"feature_id": "FEAT-LOW-BATTERY",
	"scenario": "Low Battery Warning lifecycle",
	"process_steps": [
		{
			"step": "capture_stakeholder_need",
			"artifact_type": "stakeholder_requirement",
			"aspice_process": "SYS.1",
			"input_artifact": ["CONFLUENCE-001"],
			"output_artifact": ["STK-REQ-001"],
			"trace_link": ["derived_by"],
			"evidence_source": ["Confluence", "code excerpt"],
			"validation_rule": ["source evidence exists"]
		},
		{
			"step": "derive_system_requirement",
			"artifact_type": "system_requirement",
			"aspice_process": "SYS.2",
			"input_artifact": ["STK-REQ-001"],
			"output_artifact": ["SYS-REQ-001"],
			"trace_link": ["derived_by"],
			"evidence_source": ["Confluence", "Codebeamer item history"],
			"validation_rule": ["upstream traceability exists", "verification criteria exists"]
		}
	],
	"gaps": [
		{
			"gap": "SYS-REQ-001 has no verification criteria",
			"recommendation": "Add a testable verification criterion or link a SYS.5 test case"
		}
	]
}
```

### Как это использовать в проекте

- Сценарий становится единицей анализа для Feature / Capability.
- Каждый шаг сценария соответствует одному переходу в ALM-трассировке.
- Из `process_steps` можно автоматически строить:
	- trace graph
	- compliance findings
	- evidence bundle
	- report table
- `Gap` и `Recommendation` лучше генерировать отдельно от самих артефактов, чтобы не смешивать исходные данные и выводы анализатора.

## Следующий шаг

После обсуждения сюда можно дописывать конкретные feature candidates и разбивать их на MVP / later.

## Текущий статус реализации (MVP)

Добавлены инструменты для извлечения и нормализации сценариев:

- `ui/app_main/aspice_scenario_extractor.py`
	- Извлекает scenario records из feature dataset.
	- Формирует поля: `Scenario`, `Process Step`, `Validation Rule`, `Gap`, `Recommendation`.
	- Сохраняет результат в JSON для дальнейшего анализа/отчётности.

- `ui/app_main/aspice_scenario_builder.py`
	- Строит normalized scenario records на уровне feature + process steps + gaps.
	- Поддерживает выделение behavior scenarios (если они есть в feature payload).
	- Добавляет derived gaps и рекомендации.

- `tests/test_aspice_scenario_builder.py`
	- Базовые тесты для маппинга feature payload в scenario record.

### Примеры запуска

```bash
py -3.10 ui/app_main/aspice_scenario_extractor.py
```

```bash
py -3.10 ui/app_main/aspice_scenario_builder.py data/aspice/aspice_compliance_inspection_dataset_with_features.json --write-json data/aspice/aspice_scenarios_built_v1.json
```

### Backend API (FastAPI)

- `POST /codebeamer/aspice/scenarios/build`
	- Собирает scenario records из feature dataset.
	- Параметры: `project_id?`, `source_dataset_path?`, `output_path?`.

- `GET /codebeamer/aspice/scenarios?project_id=`
	- Возвращает сохранённые scenario records.

### Django API proxies

- `GET /api/codebeamer/aspice-scenarios?project_id=`
	- Проксирует чтение records из backend.

- `POST /api/codebeamer/aspice-scenarios/build`
	- Проксирует сборку records в backend.

## Новые структурные выводы из ABOUT-RU.md

Повторное чтение ABOUT-RU.md показало, что для целевой модели важно явно разделить два режима:

1. `Behavior View`
	- Показывает поведение фичи: trigger, preconditions, main flow, alternative flow, postconditions.

2. `ASPICE View`
	- Показывает инженерную цепочку артефактов и проверок: Requirement → Architecture → Test → Result.

Также важно поддержать иерархию сценариев:

- `Feature Scenario` как верхний контейнер.
- `Requirement Trace Scenario` как дочерние трассировочные сценарии.
- Дополнительные специализированные сценарии: test coverage, change impact, defect resolution.

Для ProcessGrid это означает:

- Нужен текущий MVP-слой табличного представления steps/gaps.
- Нужен следующий слой drill-down по типу сценария (`feature_lifecycle`, `requirement_trace`).
- Нужна будущая визуализация graph/branch path для behavior scenarios.
