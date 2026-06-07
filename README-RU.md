# AI-delix — Описание проекта

## Общая архитектура

Проект состоит из двух серверов, работающих одновременно:

| Сервер | Порт | Технология | Роль |
|--------|------|-----------|------|
| Django | 8000 | Django 5.x | UI, шаблоны, маршрутизация |
| FastAPI | 8001 | FastAPI | Бизнес-логика, интеграция с CB, фоновые задачи |

Django **не обращается к Codebeamer напрямую** — все запросы к CB идут через FastAPI. Django вызывает FastAPI через `backend_get` / `backend_post` (модуль `ui/app_main/api.py`), которые используют `aiohttp` + `asgiref.sync.async_to_sync`.

---

## Модули

```
C:\git\aideex\
├── backend\
│   ├── server.py              # FastAPI app, CORS, подключение роутеров
│   └── codebeamer_router.py   # Все /codebeamer/* endpoints
├── pybeamer\                  # Библиотека для работы с CB REST API
│   ├── tracker.py             # Класс Tracker (поддержка offline mode)
│   ├── tracker_item.py        # Класс TrackerItem (поддержка offline mode)
│   ├── projects.py            # Класс Project, get_tree()
│   ├── folder.py              # Класс Folder
│   ├── render.py              # STATUS_COLORS, PRIORITY_COLORS, TRACKER_ICONS
│   ├── tree.py                # build_tree(), tree_to_rows()
│   └── fields.py              # FieldDefinition (lazy loading)
├── ui\app_main\
│   ├── views.py               # Django views
│   ├── urls.py                # URL маршруты
│   ├── api.py                 # HTTP-мост Django→FastAPI
│   └── templates\app_main\   # HTML шаблоны
├── corpus\                    # Локальные данные проектов (по project_id)
│   └── {project_id}\
│       ├── codebeamer.json    # Полный дамп данных проекта
│       └── .sync\
│           ├── tree.json      # Иерархия трекеров с item refs
│           ├── plan.json      # Сводка по трекерам (item_count)
│           ├── delta.json     # Изменения с прошлого evaluate
│           ├── manifest.json  # Снимок item_id→name для delta
│           └── selection.json # Выбранные/исключённые item IDs
├── secrets\
│   └── codebeamer.json        # Учётные данные CB (host, user, pswd, project_id)
├── logs\
│   └── codebeamer_status_{project_id}.json  # Статус текущей задачи
└── tests\
    ├── test_build_item_map.py
    ├── test_cb_pull.py
    └── test_tree.py
```

---

## Структура corpus/{project_id}/codebeamer.json

Главный файл данных проекта. Создаётся при **Pull**.

```json
{
  "project_id": 25,
  "project_name": "My Project",

  "trackers": [
    {
      "id": 123,
      "name": "Requirements",
      "type": {"id": 2, "name": "Requirement", "type": "TrackerTypeReference"},
      "color": "#4e9af1",
      "keyName": "REQ",
      "items": [
        {"id": 1001, "name": "REQ-1"},
        {"id": 1002, "name": "REQ-2"}
      ]
    }
  ],

  "items": [
    {
      "id": 1001,
      "name": "REQ-1",
      "typeName": "Functional",
      "status": {"id": 1, "name": "New", "type": "ChoiceOptionReference"},
      "subjects": [
        {"id": 2001, "name": "SYS-1", "type": "TrackerItemReference"}
      ]
    }
  ],

  "tracker_fields": {
    "123": [
      {
        "id": 10, "name": "Summary", "type": "TextFieldValue",
        "hidden": false, "multipleValues": false,
        "options": [], "trackerItemField": "summary"
      }
    ]
  }
}
```

**Ключевые поля:**
- `trackers[].items` — лёгкие ссылки `{id, name}` (refs), НЕ полные объекты
- `items[]` — полные объекты из `GET /items/{id}`, включая `subjects` (upstream-связи)
- `tracker_fields` — поля каждого трекера, ключ = `str(tracker_id)`

---

## Структура .sync/tree.json

Создаётся при **Evaluate**. Содержит иерархию с папками.

```json
{
  "project_id": 25,
  "project_name": "My Project",
  "tree": [
    {
      "isFolder": true,
      "text": "Requirements",
      "children": [
        {
          "trackerId": 123,
          "text": null,
          "items": [
            {"id": 1001, "name": "REQ-1"},
            {"id": 1002, "name": "REQ-2"}
          ]
        }
      ]
    },
    {
      "trackerId": 456,
      "text": null,
      "items": [{"id": 2001, "name": "SYS-1"}]
    }
  ]
}
```

**Важно:** `text` у tracker-нод может отсутствовать — имя трекера берётся из `codebeamer.json["trackers"]`.

---

## In-memory кэши (FastAPI, теряются при рестарте)

| Переменная | Тип | Содержимое |
|------------|-----|-----------|
| `_CB_CORPUS` | `dict[int, dict]` | `project_id → codebeamer.json` |
| `_CB_ITEM_MAPS` | `dict[int, dict]` | `project_id → item_map` |
| `_CB_TASKS` | `dict[int, dict]` | `project_id → {"thread": Thread}` |

`_CB_CORPUS` — загружается с диска при первом обращении, обновляется при Pull.
`_CB_ITEM_MAPS` — строится из corpus вызовом `_build_item_map(data)`, используется для трейсабилити.

---

## Item Map — структура и трейсабилити

`_build_item_map(payload)` строит словарь `item_id → entry`:

```python
{
  1001: {
    "id":         1001,
    "name":       "REQ-1",
    "icon":       "lightbulb",        # из TRACKER_ICONS по type_id трекера
    "color":      "#dc3545",          # из STATUS_COLORS по статусу item
    "upstream":   [2001],             # item.subjects[].id
    "downstream": [3001, 3002],       # обратные ссылки (кто ссылается на этот item)
  }
}
```

**Upstream** = `subjects` из CB API (`item.subjects` — на что ссылается этот item).
**Downstream** = инверсия upstream: кто ссылается на данный item.

Трейсабилити-дерево строится функцией `build_tree(ids, level, max_levels, item_map, direction)` из `pybeamer/tree.py`.

---

## Цепочка: Evaluate → Pull → Item Map

```
Evaluate:
  GET /trackers/tree?projectId=     → raw_tree (иерархия с Folder/Tracker)
  _flatten_tree(raw_tree)           → список трекеров [{id, name}]
  GET /trackers/{id}/items          → item refs [{id, name}] для каждого трекера
  Обогащение нод дерева (items)
  Запись .sync/tree.json, plan.json, delta.json, manifest.json

Pull:
  Чтение .sync/tree.json            → список трекеров с item refs
  GET /trackers/{id}                → полный объект трекера
  GET /trackers/{id}/fields         → список field refs
  GET /trackers/{id}/fields/{fid}   → полный объект поля (параллельно)
  GET /items/{id}                   → полный объект item (параллельно)
  Запись corpus/{id}/codebeamer.json
  _CB_CORPUS[project_id] = data
  _CB_ITEM_MAPS[project_id] = _build_item_map(data)
```

Pull автоматически запускает Evaluate если `tree.json` отсутствует или устарел (другой `project_id`).

---

## FastAPI endpoints (/codebeamer/*)

### Учётные данные
| Метод | URL | Описание |
|-------|-----|---------|
| GET | `/codebeamer/pswd` | Текущие сохранённые credentials |
| POST | `/codebeamer/pswd/save` | Сохранить credentials |
| POST | `/codebeamer/pswd/test` | Проверить подключение |
| POST | `/codebeamer/project` | Установить активный project_id |

### Проекты и трекеры
| Метод | URL | Описание |
|-------|-----|---------|
| GET | `/codebeamer/projects` | Список проектов с CB сервера |
| GET | `/codebeamer/local-projects` | Список локальных проектов (из corpus/) |
| GET | `/codebeamer/trackers?project_id=` | Трекеры из corpus (raw) |
| GET | `/codebeamer/tracker-fields/{id}?project_id=` | Поля трекера из corpus / CB API |
| GET | `/codebeamer/tree-view?project_id=` | Иерархия tree.json + tracker_meta |

### Данные / items
| Метод | URL | Описание |
|-------|-----|---------|
| GET | `/codebeamer/item-map?project_id=` | Полный item_map из памяти/corpus |
| GET | `/codebeamer/tree/{item_id}?project_id=&levels=&enrich_ids=` | Upstream/downstream деревья |
| GET | `/codebeamer/items-by-type?project_id=&type_name=` | Items по typeName из corpus |
| POST | `/codebeamer/tracker/{id}/items` | Bulk-create items в трекере |

### Фоновые задачи
| Метод | URL | Описание |
|-------|-----|---------|
| POST | `/codebeamer/evaluate` | Запустить evaluate (план + дерево) |
| POST | `/codebeamer/pull` | Запустить pull (полный дамп данных) |
| GET | `/codebeamer/status?project_id=` | Статус текущей задачи |

### Выборки
| Метод | URL | Описание |
|-------|-----|---------|
| POST | `/codebeamer/selection` | Сохранить выборку item IDs |
| GET | `/codebeamer/selection?project_id=` | Получить текущую выборку |

---

## Django URL маршруты

| URL | View | Назначение |
|-----|------|-----------|
| `/codebeamer` | `codebeamer` | Главная страница: Work/Config Items |
| `/codebeamer/select-project` | `codebeamer_select_project` | Выбор проекта, Add Project, Pull/Evaluate |
| `/codebeamer/tree` | `codebeamer_tree` | Tree View трекеров |
| `/codebeamer/tracker/{id}` | `codebeamer_tracker` | Список items трекера + форма создания |
| `/codebeamer/item/{id}` | `codebeamer_item` | Карточка item с трейсабилити |
| `/codebeamer/evaluate` | `codebeamer_evaluate` | POST → FastAPI /evaluate |
| `/codebeamer/pull` | `codebeamer_pull_view` | POST → FastAPI /pull |
| `/api/codebeamer/status/{project_id}` | прокси | Прокси статуса для JS polling |
| `/api/codebeamer/local-projects` | прокси | Прокси локальных проектов |
| `/api/codebeamer/server-projects` | прокси | Прокси серверных проектов |

---

## Offline mode в pybeamer

`Tracker(**raw, client=None)` и `TrackerItem(**raw, client=None)` — режим без API-вызовов.

При `client=None` в `_load()`:
- Пропускается `get_fields()`, `User`, `Project` lookups
- `status` хранится как `ChoiceValue` напрямую (не обёрнутый в `Field`)
- Используется в `_build_item_map` для построения item_map из corpus без CB

---

## Прогресс-бар и статус задач

Статус задачи пишется в `logs/codebeamer_status_{project_id}.json`:

```json
{
  "project_id": 25,
  "stage": "pull",
  "phase": "fetch",
  "ts": 1717800000.0,
  "started_at": 1717799990.0,
  "progress": {"total": 150, "done": 75, "failed": 0},
  "eta_sec": 45.0
}
```

**Стейджи:** `evaluate` → `pull` → `idle` | `error`

**Стале-детекция** в `/codebeamer/status`: если поток мёртв И файл старше 10 секунд → принудительно `"idle"`.

Прогресс-бар на `/select-project` использует JS polling раз в секунду через `GET /api/codebeamer/status/{project_id}`. После завершения (`idle`) обновляет список локальных проектов без перезагрузки.

---

## Классификация трекеров (Work / Config Items)

```python
_WORK_ITEM_TYPES = {
    "requirement", "bug", "epic", "issue", "user story",
    "task", "document", "testrun", "risk", "change request", "work log"
}
```

Трекеры чей `type_name.lower()` входит в этот набор → **Work Items**.
Остальные → **Config Items**.

---

## Иконки и цвета

**TRACKER_ICONS** (`pybeamer/render.py`) — маппинг `type_id → material_icon_name`:
- 1 → `fireplace`, 2 → `lightbulb` (требования), 6 → `bug_report`, 7 → `check_circle` (задачи), и т.д.

**STATUS_COLORS** — маппинг `status_name → hex_color`:
- `New` → `#dc3545` (красный)
- `In Progress` → `#0d6efd` (синий)
- `Resolved` → `#198754` (зелёный)
- `Closed` → `#146c43` (тёмно-зелёный)
- и др.

**PRIORITY_COLORS** — маппинг `priority_name → hex_color`.

---

## ASPICE compliance (модуль aispice)

Отдельная часть проекта — проверка соответствия ASPICE.

**Датасет:** `data/aspice-new/aspice_compliance_checker_test_dataset_v04.json`

Содержит:
- `inspection_cases` — кейсы для проверки (feature + requirements + trace_links + test_cases)
- `inspection_dimensions` — измерения проверки
- `rule_catalog` — правила ASPICE

**Функции:**
- `inspect_case(case)` — проверяет кейс, возвращает findings (errors/warnings)
- `validate_dataset(dataset)` — полная валидация датасета

URL: `/aispice`, `/aispice/{feature_id}/lifecycle`, `/aispice/{feature_id}/timeline`

---

## Ключевые цепочки данных

### Цепочка трейсабилити item
```
item.subjects → upstream item IDs → build_tree(upstream) → upstream tree rows
                                  → inverse → downstream IDs → downstream tree rows
```

### Цепочка загрузки трекера (/codebeamer/tracker/{id})
```
CB API: GET /trackers/{id}         → tracker object
FastAPI: GET /tracker-fields/{id}  → поля из corpus / CB API
CB API: tracker.get_items()        → список items (live)
_CB_ITEM_MAPS[project_id]         → icon + color для каждого item
```

### Цепочка загрузки главной страницы (/codebeamer)
```
FastAPI: GET /codebeamer/trackers  → raw trackers из corpus
  → Work Items / Config Items (классификация по type_name)
FastAPI: GET /codebeamer/item-map  → has_project_json флаг
```

---

## Файл secrets/codebeamer.json

```json
{
  "codebeamer_host": "https://dxc.codebeamer.com",
  "username": "user@example.com",
  "pswd": "password",
  "project_id": 25
}
```

Читается функцией `_load_saved_pswd()` в FastAPI. Django получает credentials через `GET /codebeamer/pswd`.

---

## Тесты

| Файл | Тесты | Покрытие |
|------|-------|---------|
| `test_build_item_map.py` | 14 | `_build_item_map`: статусы, цвета, upstream/downstream, иконки, offline mode |
| `test_cb_pull.py` | 10 | `_cb_pull`: структура JSON, поля, item map, auto-evaluate, ошибки |
| `test_tree.py` | 17 | `build_tree`, `tree_to_rows`: циклы, уровни, направления |
