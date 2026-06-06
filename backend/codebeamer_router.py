import logging
import json
import shutil
import threading
import time
import uuid as _uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Set
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from pybeamer import Codebeamer
from pybeamer.tracker import Tracker
from pybeamer.tracker_item import TrackerItem
from pybeamer.tree import build_tree, tree_to_rows
from pybeamer.render import STATUS_COLORS, TRACKER_ICONS


APP_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = APP_ROOT / "corpus"

_CB_LOG = logging.getLogger("backend.codebeamer")

codebeamer_router = APIRouter(tags=["codebeamer"])

_CB_TASKS: dict[int, dict[str, Any]] = {}
_CB_ITEM_MAPS: dict[int, dict] = {}         # project_id → full item map (in-memory cache)
_CB_CORPUS: dict[int, dict] = {}            # project_id → parsed codebeamer.json (in-memory cache)


def _corpus_json_path(project_id: int) -> Path:
    p = CORPUS_ROOT / str(project_id) / "codebeamer.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_corpus(project_id: int) -> dict | None:
    """Return corpus for project_id from memory; load from disk on first access."""
    if project_id in _CB_CORPUS:
        return _CB_CORPUS[project_id]
    path = _corpus_json_path(project_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        _CB_CORPUS[project_id] = data
        return data
    except Exception:
        return None


def _build_item_map(payload: dict) -> dict:
    """Build {item_id: {id, name, icon, color, upstream, downstream}} from raw codebeamer.json.

    Expected payload structure:
      {
        "trackers": [{id, name, type, color, "items": [{id, name}, ...]}, ...],
        "items":    [{full TrackerItem response}, ...]   ← top-level, from GET /items/{id}
      }
    Uses TrackerItem(client=None) offline mode — _load() skips all API calls.
    """
    # tracker metadata keyed by the item refs stored inside each tracker
    tracker_by_item: dict[int, tuple[str, str]] = {}  # item_id → (icon, tracker_color)
    for raw_tracker in payload.get("trackers", []):
        tracker = Tracker(**raw_tracker, client=None)
        icon    = TRACKER_ICONS.get(tracker.type_id, "label")
        t_color = tracker.color or "#6c757d"
        for ref in raw_tracker.get("items", []):
            iid = ref.get("id")
            if iid:
                tracker_by_item[iid] = (icon, t_color)

    # full item objects keyed by id
    items_by_id: dict[int, dict] = {}
    for raw_item in payload.get("items", []):
        iid = raw_item.get("id")
        if iid:
            items_by_id[iid] = raw_item

    item_map: dict[int, dict] = {}
    for iid, (icon, t_color) in tracker_by_item.items():
        raw_item = items_by_id.get(iid)
        if raw_item is None:
            continue
        name = raw_item.get("name", "")
        item = TrackerItem(**raw_item, client=None)
        # offline: item.status is ChoiceValue (not Field wrapper)
        status_val = item.status
        status = status_val.name if status_val else None
        color = STATUS_COLORS.get(status, t_color) if status else t_color
        upstream = [
            s["id"] for s in (item.subjects or [])
            if isinstance(s, dict) and s.get("id")
        ]
        item_map[iid] = {
            "id": iid, "name": name,
            "icon": icon, "color": color,
            "upstream": upstream, "downstream": [],
        }

    for iid, entry in item_map.items():
        for uid in entry["upstream"]:
            if uid in item_map:
                item_map[uid]["downstream"].append(iid)
    return item_map


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class CodebeamerPswdRequest(BaseModel):
    codebeamer_host: str
    username: str
    pswd: str


class CodebeamerPswdSaveRequest(BaseModel):
    codebeamer_host: str
    username: str
    pswd: Optional[str] = None
    project_id: Optional[int] = None


class CodebeamerSelectionRequest(BaseModel):
    project_id: int
    include_ids: list[int]
    exclude_ids: Optional[list[int]] = None


class CodebeamerEvaluateRequest(BaseModel):
    project_id: int
    workers: Optional[int] = 4


class CodebeamerPullRequest(BaseModel):
    project_id: int
    workers: Optional[int] = 4


# ---------------------------------------------------------------------------
# Credentials helpers
# ---------------------------------------------------------------------------

def _secrets_path() -> Path:
    p = APP_ROOT / "secrets" / "codebeamer.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_saved_pswd() -> dict | None:
    try:
        data = json.loads(_secrets_path().read_text(encoding="utf-8"))
        host = str(data.get("codebeamer_host") or "").strip()
        username = str(data.get("username") or "").strip()
        pswd = str(data.get("pswd") or "").strip()
        if host and username and pswd:
            result = {"codebeamer_host": host, "username": username, "pswd": pswd}
            if data.get("project_id"):
                result["project_id"] = data["project_id"]
            return result
    except Exception:
        return None
    return None


def _make_client(saved: dict) -> Codebeamer:
    return Codebeamer(
        url=saved["codebeamer_host"],
        username=saved["username"],
        password=saved["pswd"],
        api_root="api/v3",
    )


# ---------------------------------------------------------------------------
# Credentials endpoints
# ---------------------------------------------------------------------------

@codebeamer_router.post("/codebeamer/pswd/test", tags=["codebeamer"])
async def codebeamer_pswd_test(req: CodebeamerPswdRequest) -> Dict[str, Any]:
    host = req.codebeamer_host.strip()
    username = req.username.strip()
    pswd = req.pswd.strip()
    try:
        cb = Codebeamer(url=host, username=username, password=pswd, api_root="api/v3")
        user = cb.get_user(username)
        if user is None:
            return {"ok": False, "error": "Authentication failed or user not found"}
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@codebeamer_router.get("/codebeamer/pswd", tags=["codebeamer"])
async def codebeamer_pswd_get() -> Dict[str, Any]:
    saved = _load_saved_pswd()
    if saved is not None:
        host = str(saved.get("codebeamer_host") or "").strip()
        username = str(saved.get("username") or "").strip()
        has_pswd = bool(saved.get("pswd") or "")
        return {
            "ok": True,
            "codebeamer_host": host,
            "username": username,
            "has_pswd": has_pswd,
            "pswd": saved.get("pswd", ""),
            "project_id": saved.get("project_id")
            }
    return {"ok": False, "error": "No saved credentials found"}



@codebeamer_router.post("/codebeamer/pswd/save", tags=["codebeamer"])
async def codebeamer_pswd_save(req: CodebeamerPswdSaveRequest) -> Dict[str, Any]:
    cur = _load_saved_pswd() or {}
    host = req.codebeamer_host.strip()
    username = req.username.strip()
    pswd = (req.pswd or "").strip()
    if not host:
        raise HTTPException(status_code=400, detail="codebeamer_host required")
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    if not pswd:
        pswd = str(cur.get("pswd") or "").strip()
    data = {"codebeamer_host": host, "username": username, "pswd": pswd}
    if req.project_id is not None:
        data["project_id"] = req.project_id
    elif cur.get("project_id"):
        data["project_id"] = cur["project_id"]
    _secrets_path().write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Project / tree endpoints
# ---------------------------------------------------------------------------

def _fetch_and_build_item_map(project_id: int, saved: dict) -> None:
    """Fetch all trackers+items from Codebeamer API and build item_map in memory."""
    try:
        cb = _make_client(saved)
        project = cb.get_project(project_id)
        trackers_data = []
        for t in project.get_trackers():
            items_data = []
            for item in t.get_items():
                status = None
                try:
                    status = item.status.value.name if item.status and item.status.value else None
                except Exception:
                    pass
                subjects = []
                try:
                    subjects = [{"id": s.get("id"), "name": s.get("name", "")}
                                for s in (item.subjects or []) if isinstance(s, dict) and s.get("id")]
                except Exception:
                    pass
                items_data.append({
                    "id": item.id, "name": item.name,
                    "status": status, "subjects": subjects,
                })
            trackers_data.append({
                "id": t.id, "name": t.name,
                "type_id": t.type_id, "color": t.color or "#6c757d",
                "items": items_data,
            })
        data = {"project_id": project_id, "trackers": trackers_data}
        _CB_ITEM_MAPS[project_id] = _build_item_map(data)
        _CB_LOG.info("item_map built project_id=%d items=%d", project_id, len(_CB_ITEM_MAPS[project_id]))
    except Exception as exc:
        _CB_LOG.exception("fetch_and_build_item_map failed project_id=%s err=%s", project_id, exc)


@codebeamer_router.post("/codebeamer/project", tags=["codebeamer"])
async def codebeamer_set_project(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Set or clear current project_id. On set — triggers async item_map build from API."""
    project_id = payload.get("project_id")
    cur = json.loads(_secrets_path().read_text(encoding="utf-8")) if _secrets_path().exists() else {}
    if project_id:
        cur["project_id"] = project_id
        _secrets_path().write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        saved = _load_saved_pswd()
        if saved:
            t = threading.Thread(target=_fetch_and_build_item_map, args=(project_id, saved), daemon=True)
            t.start()
    else:
        cur.pop("project_id", None)
        _secrets_path().write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"ok": True}


@codebeamer_router.get("/codebeamer/projects", tags=["codebeamer"])
async def codebeamer_projects() -> Dict[str, Any]:
    _CB_LOG.info("GET /codebeamer/projects")
    saved = _load_saved_pswd()
    if not saved:
        raise HTTPException(status_code=400, detail="Password not configured")
    try:
        client = _make_client(saved)
        projects = client.get_projects() or []
        nodes = [{"id": p.id, "name": p.name} for p in projects]
        _CB_LOG.info("projects resolved count=%d", len(nodes))
        return {"ok": True, "projects": nodes}
    except Exception as exc:
        _CB_LOG.exception("projects error err=%s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@codebeamer_router.post("/codebeamer/tracker/{tracker_id}/items", tags=["codebeamer"])
async def codebeamer_push_items(tracker_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Bulk-create tracker items in the given tracker.

    Body shape:
        {
          "items": [
            {"name": "...", "description": "...", "description_format": "Markdown"},
            ...
          ]
        }

    Returns: ``{ok, created: [{id, name}, ...], failed: [{name, error}, ...]}``.
    Each item is created independently — one failure does not abort the rest.
    """
    items = (payload or {}).get("items") or []
    _CB_LOG.info("POST /codebeamer/tracker/%s/items count=%d", tracker_id, len(items))

    saved = _load_saved_pswd()
    if not saved:
        raise HTTPException(status_code=400, detail="Password not configured")

    try:
        client = _make_client(saved)
        tracker = client.get_tracker(tracker_id)
        if tracker is None:
            raise HTTPException(status_code=404, detail=f"Tracker {tracker_id} not found")
    except HTTPException:
        raise
    except Exception as exc:
        _CB_LOG.exception("push_items: tracker resolution failed tracker_id=%s err=%s", tracker_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    created: list[dict] = []
    failed: list[dict] = []
    for it in items:
        name = (it.get("name") or "").strip()
        if not name:
            failed.append({"name": "", "error": "name is required"})
            continue
        try:
            new_item = tracker.create_tracker_item(
                name=name,
                description=it.get("description") or "--",
                type_name=it.get("type_name"),
                description_format=it.get("description_format") or "PlainText",
            )
            created.append({"id": getattr(new_item, "id", None), "name": name})
        except Exception as exc:  # noqa: BLE001 — we surface per-item errors
            _CB_LOG.warning("push_items: failed to create item '%s' err=%s", name, exc)
            failed.append({"name": name, "error": str(exc)})

    _CB_LOG.info(
        "push_items done tracker_id=%s created=%d failed=%d",
        tracker_id, len(created), len(failed),
    )
    return {"ok": True, "created": created, "failed": failed}


# ---------------------------------------------------------------------------
# Last-project endpoint
# ---------------------------------------------------------------------------

@codebeamer_router.get("/codebeamer/last_project", tags=["codebeamer"])
async def codebeamer_last_project() -> Dict[str, Any]:
    """Return the project whose plan.json was modified most recently."""
    latest_path: Path | None = None
    latest_mtime = 0.0
    for plan_path in CORPUS_ROOT.glob("*/.sync/plan.json"):
        try:
            mtime = plan_path.stat().st_mtime
            if mtime > latest_mtime:
                latest_mtime = mtime
                latest_path = plan_path
        except Exception:
            continue
    if latest_path is None:
        return {"ok": False}
    try:
        data = json.loads(latest_path.read_text(encoding="utf-8"))
        return {
            "ok": True,
            "project_id": data.get("project_id"),
            "project_name": data.get("project_name"),
        }
    except Exception:
        return {"ok": False}


# ---------------------------------------------------------------------------
# Selection endpoints
# ---------------------------------------------------------------------------

@codebeamer_router.post("/codebeamer/selection", tags=["codebeamer"])
async def codebeamer_selection(req: CodebeamerSelectionRequest) -> Dict[str, Any]:
    _CB_LOG.info("/codebeamer/selection request project_id=%s include_ids=%d exclude_ids=%d",
                   req.project_id, len(req.include_ids), len(req.exclude_ids or []))
    path = CORPUS_ROOT / str(req.project_id) / ".sync"
    path.mkdir(parents=True, exist_ok=True)
    sel = {"include_ids": req.include_ids, "exclude_ids": req.exclude_ids or []}
    (path / "selection.json").write_text(json.dumps(sel, indent=2), encoding="utf-8")
    return {"ok": True}


@codebeamer_router.get("/codebeamer/selection", tags=["codebeamer"])
async def codebeamer_selection_get(project_id: int) -> Dict[str, Any]:
    _CB_LOG.info("/codebeamer/selection get request project_id=%s", project_id)
    sel_path = CORPUS_ROOT / str(project_id) / ".sync" / "selection.json"
    try:
        data = json.loads(sel_path.read_text(encoding="utf-8"))
        return {
            "ok": True,
            "include_ids": list(data.get("include_ids") or []),
            "exclude_ids": list(data.get("exclude_ids") or []),
        }
    except Exception:
        return {"ok": True, "include_ids": [], "exclude_ids": []}


# ---------------------------------------------------------------------------
# Background task infrastructure
# ---------------------------------------------------------------------------

class _PullProgress:
    """Tracks done/failures counters and throttles status file writes to ≤1 per 500ms."""
    def __init__(self, project_id: int, total: int, started_wall: float):
        self.project_id  = project_id
        self.total       = total
        self.started_wall = started_wall
        self.done        = 0
        self.failures    = 0
        self._last_write = 0.0

    def tick(self) -> None:
        self.done += 1
        now = time.time()
        if now - self._last_write >= 0.5 or self.done == self.total:
            self._last_write = now
            elapsed = now - self.started_wall
            eta = (self.total - self.done) * (elapsed / self.done) if self.done > 0 and self.total > self.done else 0.0
            _update_cb_status(self.project_id, "pull", {
                "phase": "fetch",
                "progress": {"total": self.total, "done": self.done, "failed": self.failures},
                "started_at": self.started_wall,
                "eta_sec": max(0.0, float(eta)),
            })


def _update_cb_status(project_id: int, stage: str, extra: Dict[str, Any] | None = None) -> None:
    st: Dict[str, Any] = {"project_id": project_id, "stage": stage, "ts": time.time()}
    if extra:
        st.update(extra)
    out = APP_ROOT / "logs" / f"codebeamer_status_{project_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(st), encoding="utf-8")


def _run_cb_task(project_id: int, fn, *args, **kwargs) -> None:
    try:
        _CB_TASKS.setdefault(project_id, {})
        _update_cb_status(project_id, kwargs.pop("__stage", "running"))
        fn(*args, **kwargs)
        _update_cb_status(project_id, "idle")
    except SystemExit as exc:
        _update_cb_status(project_id, "error", {"error": str(exc)})
    except Exception as exc:
        _update_cb_status(project_id, "error", {"error": str(exc)})
    finally:
        try:
            _CB_TASKS[project_id].pop("thread", None)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Pipeline functions (run in background threads)
# ---------------------------------------------------------------------------

def _cb_evaluate(project_id: int, saved: dict, workers: int = 4, _stage: str = "evaluate") -> None:
    """Build plan/tree/manifest for a Codebeamer project.

    Phase 1 — traverse: get project + all its trackers.
    Phase 2 — items: fetch itemRefs per tracker in parallel (lightweight, no descriptions).
    Writes plan.json, delta.json, manifest.json, tree.json.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    started_wall = time.time()
    sync_dir = CORPUS_ROOT / str(project_id) / ".sync"
    sync_dir.mkdir(parents=True, exist_ok=True)

    # Load previous manifest for delta computation
    manifest_path = sync_dir / "manifest.json"
    try:
        prev_manifest: dict[str, dict] = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception:
        prev_manifest = {}

    # Phase 1: resolve project and trackers
    _update_cb_status(project_id, _stage, {
        "phase": "traverse",
        "progress": {"total": 0, "done": 0},
        "started_at": started_wall,
    })

    client = _make_client(saved)

    # Resolve project name
    project = client.get_project(int(project_id))
    # project_resp = client._client.get(f"projects/{project_id}")
    # if not isinstance(project_resp, dict):
    #     raise SystemExit(f"Project {project_id} not found")
    project_name = project.name

    # GET /projects/{id}/trackers returns {"trackers": [...], "total": N, ...}
    trackers_resp = project.get_ref_trackers()

    if isinstance(trackers_resp, list):
        raw_trackers = trackers_resp
    else:
        raw_trackers = []

    total_trackers = len(raw_trackers)
    _CB_LOG.info("evaluate: project_id=%d name=%s trackers=%d", project_id, project_name, total_trackers)
    time.sleep(0.5)

    _update_cb_status(project_id, _stage, {
        "phase": "items",
        "progress": {"total": total_trackers, "done": 0},
        "started_at": started_wall,
    })

    # Phase 2: fetch itemRefs per tracker in parallel (each thread owns its own client)
    def _fetch_tracker_items(tracker: dict) -> tuple[dict, list[dict]]:
        tc = _make_client(saved)
        tracker_id = tracker["id"]
        tracker_name = str(tracker.get("name") or tracker_id)
        refs: list[dict] = []
        page, page_size = 1, 100
        while True:
            resp = tc._client.get(
                f"trackers/{tracker_id}/items",
                params={"page": page, "pageSize": page_size},
            )
            if not isinstance(resp, dict):
                break
            for ref in resp.get("itemRefs") or []:
                item_id = ref.get("id")
                if item_id is not None:
                    refs.append({"id": item_id, "name": str(ref.get("name") or "")})
            if page * page_size >= int(resp.get("total") or 0):
                break
            page += 1
        plan_entry = {"id": tracker_id, "name": tracker_name, "item_count": len(refs)}
        time.sleep(0.6)
        return plan_entry, refs

    workers = max(1, min(int(workers or 4), 16))
    tracker_results: dict[int, tuple[dict, list[dict]]] = {}
    done_trackers = 0

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_fetch_tracker_items, t): t for t in raw_trackers}
        for fut in as_completed(futures):
            t = futures[fut]
            try:
                plan_entry, refs = fut.result()
            except Exception as exc:
                _CB_LOG.warning("evaluate: tracker %s failed: %s", t.get("id"), exc)
                plan_entry = {"id": t["id"], "name": str(t.get("name") or t["id"]), "item_count": 0}
                refs = []
            tracker_results[t["id"]] = (plan_entry, refs)
            done_trackers += 1
            _update_cb_status(project_id, _stage, {
                "phase": "items",
                "progress": {"total": total_trackers, "done": done_trackers},
                "started_at": started_wall,
            })
            _CB_LOG.info("evaluate: tracker %d (%s) items=%d", t["id"], t.get("name"), len(refs))

    # Assemble results in original tracker order
    current_manifest: dict[str, dict] = {}
    trackers_plan: list[dict] = []
    tree_trackers: list[dict] = []
    total_items = 0

    for tracker in raw_trackers:
        tid = tracker["id"]
        plan_entry, refs = tracker_results[tid]
        trackers_plan.append(plan_entry)
        tree_trackers.append({"id": tid, "name": str(tracker.get("name") or tid), "items": refs})
        for ref in refs:
            current_manifest[str(ref["id"])] = {"tracker_id": tid, "name": ref["name"]}
        total_items += plan_entry["item_count"]

    # Compute delta vs previous manifest
    prev_ids = set(prev_manifest.keys())
    curr_ids = set(current_manifest.keys())
    changed_ids = [
        int(iid)
        for iid in curr_ids & prev_ids
        if prev_manifest[iid].get("name") != current_manifest[iid].get("name")
    ]
    delta = {
        "added":   sorted(int(i) for i in curr_ids - prev_ids),
        "changed": sorted(changed_ids),
        "deleted": sorted(int(i) for i in prev_ids - curr_ids),
    }

    plan = {
        "project_id": project_id,
        "project_name": project_name,
        "trackers": trackers_plan,
        "total_items": total_items,
    }
    tree = {"project_id": project_id, "project_name": project_name, "trackers": tree_trackers}

    (sync_dir / "plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    (sync_dir / "delta.json").write_text(json.dumps(delta, indent=2), encoding="utf-8")
    manifest_path.write_text(json.dumps(current_manifest, indent=2), encoding="utf-8")
    (sync_dir / "tree.json").write_text(json.dumps(tree, indent=2, ensure_ascii=False), encoding="utf-8")

    _CB_LOG.info(
        "evaluate done: project_id=%d name=%s total_items=%d added=%d changed=%d deleted=%d",
        project_id, project_name, total_items, len(delta["added"]), len(delta["changed"]), len(delta["deleted"]),
    )


# ---------------------------------------------------------------------------
# Pipeline endpoints
# ---------------------------------------------------------------------------

@codebeamer_router.post("/codebeamer/evaluate", tags=["codebeamer"])
def codebeamer_evaluate(req: CodebeamerEvaluateRequest) -> Dict[str, Any]:
    _CB_LOG.info("POST /codebeamer/evaluate project_id=%d", req.project_id)

    saved = _load_saved_pswd()
    if not saved:
        raise HTTPException(status_code=400, detail="Password not configured")

    existing = _CB_TASKS.get(req.project_id, {}).get("thread")
    if existing and existing.is_alive():
        raise HTTPException(status_code=409, detail="Evaluate already running for this project")

    run_id = str(_uuid.uuid4())
    t = threading.Thread(
        target=_run_cb_task,
        args=(req.project_id, _cb_evaluate, req.project_id, saved),
        kwargs={"workers": int(req.workers or 4), "__stage": "evaluate"},
        daemon=True,
    )
    _CB_TASKS.setdefault(req.project_id, {})["thread"] = t
    t.start()
    return {"ok": True, "run_id": run_id}




@codebeamer_router.get("/codebeamer/item-map", tags=["codebeamer"])
async def codebeamer_item_map(project_id: int) -> Dict[str, Any]:
    """Return full item map from memory. Fallback to corpus JSON if not yet loaded."""
    if project_id in _CB_ITEM_MAPS:
        return {"ok": True, "item_map": _CB_ITEM_MAPS[project_id]}
    data = _load_corpus(project_id)
    if data is None:
        return {"ok": False, "error": "Item map not ready yet"}
    _CB_ITEM_MAPS[project_id] = _build_item_map(data)
    return {"ok": True, "item_map": _CB_ITEM_MAPS[project_id]}


@codebeamer_router.get("/codebeamer/items-by-tracker-type", tags=["codebeamer"])
async def codebeamer_items_by_tracker_type(
    project_id: int,
    type_name: str,
    type_id: Optional[int] = None,
) -> Dict[str, Any]:
    """Return items from trackers matching type_name (case-insensitive) or type_id, read from corpus JSON."""
    data = _load_corpus(project_id)
    if data is None:
        return {"ok": False, "error": "Corpus not found — run Pull first", "items": []}

    type_name_lower = type_name.lower()
    items_by_id: dict[int, dict] = {
        raw["id"]: raw for raw in data.get("items", []) if raw.get("id")
    }

    result: list[dict] = []
    for t in data.get("trackers", []):
        t_type = t.get("type") or {}
        match = (
            (t_type.get("name") or "").lower() == type_name_lower
            or (type_id is not None and t_type.get("id") == type_id)
        )
        if not match:
            continue
        for ref in t.get("items", []):
            iid = ref.get("id")
            if not iid:
                continue
            full = items_by_id.get(iid)
            result.append({"id": iid, "name": full.get("name", ref.get("name", "")) if full else ref.get("name", "")})

    return {"ok": True, "items": result}


@codebeamer_router.get("/codebeamer/tree/{item_id}", tags=["codebeamer"])
async def codebeamer_tree_item(
    item_id: int,
    project_id: int,
    levels: int = 2,
    enrich_ids: str = "",  # comma-separated IDs to enrich (subjects + versions)
) -> Dict[str, Any]:
    """Build upstream/downstream trees and enrich specific item IDs."""
    item_map = _CB_ITEM_MAPS.get(project_id)
    if not item_map:
        data = _load_corpus(project_id)
        if data is None:
            return {"ok": False, "error": "Item map not ready"}
        item_map = _build_item_map(data)
        _CB_ITEM_MAPS[project_id] = item_map

    entry = item_map.get(item_id, {})

    # Upstream tree
    up_ids = entry.get("upstream", [])
    up_tree = build_tree(up_ids, 1, levels, item_map, direction="upstream")

    # Downstream tree
    dn_ids = entry.get("downstream", [])
    dn_tree = build_tree(dn_ids, 1, levels, item_map, direction="downstream", visited={item_id})

    # Enrich only requested IDs — no full map transfer
    enriched = {}
    if enrich_ids:
        for sid in enrich_ids.split(","):
            try:
                iid = int(sid.strip())
                e = item_map.get(iid)
                if e:
                    enriched[iid] = {"icon": e["icon"], "color": e["color"]}
            except ValueError:
                pass

    return {
        "ok": True,
        "upstream_rows": tree_to_rows(up_tree, levels),
        "downstream_rows": tree_to_rows(dn_tree, levels),
        "cols": levels,
        "enriched": enriched,
    }


@codebeamer_router.get("/codebeamer/status", tags=["codebeamer"])
def codebeamer_status(project_id: int) -> Dict[str, Any]:
    _CB_LOG.info("GET /codebeamer/status project_id=%d", project_id)

    st_path = APP_ROOT / "logs" / f"codebeamer_status_{project_id}.json"
    raw_status: Dict[str, Any] = {}
    stage = "idle"
    try:
        raw_status = json.loads(st_path.read_text(encoding="utf-8")) or {}
        stage = str(raw_status.get("stage") or "idle")
    except Exception:
        raw_status = {}

    sync_dir = CORPUS_ROOT / str(project_id) / ".sync"
    plan: Dict[str, Any] = {}
    delta: Dict[str, Any] = {}
    try:
        plan = json.loads((sync_dir / "plan.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        delta = json.loads((sync_dir / "delta.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    # Reset stale non-idle stage when thread is dead and status is not fresh
    try:
        t = (_CB_TASKS.get(project_id) or {}).get("thread")
        recent = False
        try:
            ts_val = float(raw_status.get("ts") or 0)
            recent = (time.time() - ts_val) < 10.0
        except Exception:
            pass
        if (not t or not t.is_alive()) and not recent:
            stage = "idle"
    except Exception:
        stage = "idle"

    resp: Dict[str, Any] = {"project_id": project_id, "stage": stage, "plan": plan, "delta": delta}

    if isinstance(raw_status, dict):
        if raw_status.get("phase"):
            resp["phase"] = raw_status["phase"]
        if raw_status.get("error"):
            resp["error"] = raw_status["error"]
        progress = raw_status.get("progress")
        if isinstance(progress, dict):
            resp["progress"] = {
                "total": int(progress.get("total") or 0),
                "done": int(progress.get("done") or 0),
                "failed": int(progress.get("failed") or 0),
            }
        if raw_status.get("eta_sec") is not None:
            resp["eta_sec"] = float(raw_status["eta_sec"])
        try:
            started_at = float(raw_status["started_at"])
            resp["started_at"] = started_at
            resp["elapsed_sec"] = raw_status.get("elapsed_sec") or max(0.0, time.time() - started_at)
        except Exception:
            pass

    return resp



def _cb_pull(project_id: int, saved: dict, workers: int = 4) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    started_wall = time.time()
    sync_dir = CORPUS_ROOT / str(project_id) / ".sync"

    # Load tree.json; run evaluate automatically if missing or stale
    def _load_tree() -> dict:
        try:
            return json.loads((sync_dir / "tree.json").read_text(encoding="utf-8"))
        except Exception:
            return {}

    tree = _load_tree()
    if tree.get("project_id") != project_id:
        _CB_LOG.info("pull: tree.json missing/stale — running evaluate first project_id=%d", project_id)
        _cb_evaluate(project_id, saved, workers)
        tree = _load_tree()
        if tree.get("project_id") != project_id:
            raise SystemExit("evaluate did not produce a valid tree.json")

    trackers_info = tree.get("trackers", [])
    total_trackers = len(trackers_info)
    total_items = sum(len(t.get("items", [])) for t in trackers_info)
    total = total_trackers + total_items
    workers = max(1, min(int(workers or 4), 16))

    _CB_LOG.info("pull: project_id=%d trackers=%d items=%d", project_id, total_trackers, total_items)
    _update_cb_status(project_id, "pull", {
        "phase": "fetch",
        "progress": {"total": total, "done": 0, "failed": 0},
        "started_at": started_wall, "eta_sec": 0.0,
    })

    cb = _make_client(saved)
    progress = _PullProgress(project_id, total, started_wall)

    def _fetch_raw(url: str) -> dict:
        raw = cb._client.get(url)
        return raw if isinstance(raw, dict) else {}

    def _fetch_list(url: str) -> list:
        raw = cb._client.get(url)
        return raw if isinstance(raw, list) else []

    # Fetch trackers + their items in parallel per tracker
    trackers_data = []
    all_items: list[dict] = []
    tracker_fields: dict[str, list] = {}   # str(tracker_id) → list of full field dicts
    for t_info in trackers_info:
        tid = t_info["id"]
        try:
            raw_tracker = _fetch_raw(f"trackers/{tid}")
        except Exception as exc:
            _CB_LOG.warning("pull: tracker %s failed: %s", tid, exc)
            progress.failures += 1
            raw_tracker = {"id": tid, "name": t_info.get("name", ""), "type": {"id": -1, "name": "Unknown", "type": "TrackerTypeReference"}, "color": "#6c757d"}

        # Fetch full field definitions for this tracker in parallel
        try:
            field_refs = _fetch_list(f"trackers/{tid}/fields")
            field_ids = [r.get("id") for r in field_refs if r.get("id")]
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futs = {ex.submit(_fetch_raw, f"trackers/{tid}/fields/{fid}"): fid for fid in field_ids}
                raw_fields = []
                for fut in as_completed(futs):
                    try:
                        raw_fields.append(fut.result())
                    except Exception as exc:
                        _CB_LOG.warning("pull: field %s/%s failed: %s", tid, futs[fut], exc)
            tracker_fields[str(tid)] = raw_fields
        except Exception as exc:
            _CB_LOG.warning("pull: fields for tracker %s failed: %s", tid, exc)
            tracker_fields[str(tid)] = []

        progress.tick()  # tracker done: object + all fields fetched

        item_ids = [item["id"] for item in t_info.get("items", [])]

        if len(item_ids) > 0:
            _CB_LOG.info("pull: tracker_id=%s items=%d fetching details with %d workers", tid, len(item_ids), workers)

        # Fetch full item objects for this tracker in parallel
        items_data = []
        with ThreadPoolExecutor(max_workers=workers) as ex:
            fut_map = {ex.submit(_fetch_raw, f"items/{iid}"): iid for iid in item_ids}
            for fut in as_completed(fut_map):
                iid = fut_map[fut]
                try:
                    raw_item = fut.result()
                    items_data.append(raw_item)
                except Exception as exc:
                    _CB_LOG.warning("pull: item %s failed: %s", iid, exc)
                    progress.failures += 1
                    items_data.append({"id": iid, "name": str(iid)})
                progress.tick()

        # tracker keeps only lightweight refs; full objects go to top-level items
        raw_tracker["items"] = [{"id": it.get("id"), "name": it.get("name", "")} for it in items_data]
        trackers_data.append(raw_tracker)
        all_items.extend(items_data)

    # Save to codebeamer.json and rebuild item_map
    data = {
        "project_id": project_id,
        "project_name": tree.get("project_name", ""),
        "trackers": trackers_data,
        "items": all_items,
        "tracker_fields": tracker_fields
    }
    _corpus_json_path(project_id).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    _CB_CORPUS[project_id] = data
    _CB_ITEM_MAPS[project_id] = _build_item_map(data)

    _CB_LOG.info("pull done: project_id=%d items=%d failed=%d", project_id, total_items, progress.failures)
    _update_cb_status(project_id, "pull", {
        "phase": "done",
        "progress": {"total": total, "done": total, "failed": progress.failures},
        "started_at": started_wall,
        "elapsed_sec": round(time.time() - started_wall, 3),
        "eta_sec": 0.0,
    })


@codebeamer_router.post("/codebeamer/pull", tags=["codebeamer"])
def codebeamer_pull(req: CodebeamerPullRequest) -> Dict[str, Any]:
    _CB_LOG.info("POST /codebeamer/pull project_id=%d", req.project_id)

    saved = _load_saved_pswd()
    if not saved:
        raise HTTPException(status_code=400, detail="Password not configured")

    existing = _CB_TASKS.get(req.project_id, {}).get("thread")
    if existing and existing.is_alive():
        raise HTTPException(status_code=409, detail="Pull already running for this project")

    run_id = str(_uuid.uuid4())
    t = threading.Thread(
        target=_run_cb_task,
        args=(req.project_id, _cb_pull, req.project_id, saved),
        kwargs={"workers": int(req.workers or 4), "__stage": "pull"},
        daemon=True,
    )
    _CB_TASKS.setdefault(req.project_id, {})["thread"] = t
    t.start()
    return {"ok": True, "run_id": run_id}


@codebeamer_router.get("/codebeamer/tracker-fields/{tracker_id}", tags=["codebeamer"])
def codebeamer_tracker_fields(tracker_id: int, project_id: Optional[int] = None) -> Dict[str, Any]:
    """Return raw field definitions for a tracker.
    Reads from corpus JSON if project_id provided and pull was run; falls back to live CB API.
    Caller is responsible for field mapping/filtering.
    """
    if project_id is not None:
        data = _load_corpus(project_id)
        if data is not None:
            raw_fields = data.get("tracker_fields", {}).get(str(tracker_id))
            if raw_fields is not None:
                return {"ok": True, "fields": raw_fields, "source": "corpus"}

    # Fallback: live CB API — fetch raw field data directly without FieldDefinition lazy loading
    saved = _load_saved_pswd()
    if not saved:
        raise HTTPException(status_code=400, detail="Password not configured")
    try:
        client = _make_client(saved)
        field_refs = client._client.get(f"trackers/{tracker_id}/fields")
        if not isinstance(field_refs, list):
            return {"ok": True, "fields": [], "source": "api"}
        from concurrent.futures import ThreadPoolExecutor, as_completed
        raw_fields = []
        with ThreadPoolExecutor(max_workers=8) as ex:
            futs = {ex.submit(client._client.get, f"trackers/{tracker_id}/fields/{r.get('id')}"): r.get("id")
                    for r in field_refs if r.get("id")}
            for fut in as_completed(futs):
                try:
                    raw_fields.append(fut.result())
                except Exception:
                    pass
        return {"ok": True, "fields": raw_fields, "source": "api"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

