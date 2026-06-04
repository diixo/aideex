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
from .sync_utils import SyncPaths, SourceSpec, Plan, ROOT, _load_json, _write_json, _append_perf, _utc_now
from pybeamer import Codebeamer

APP_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = APP_ROOT / "corpus"

_CONF_LOG = logging.getLogger("backend.codebeamer")

codebeamer_router = APIRouter(tags=["codebeamer"])

_CB_TASKS: dict[int, dict[str, Any]] = {}


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


class CodebeamerBuildRequest(BaseModel):
    project_id: int


class CodebeamerActivateRequest(BaseModel):
    project_id: int


# ---------------------------------------------------------------------------
# Credentials helpers
# ---------------------------------------------------------------------------

def _secrets_path() -> Path:
    p = APP_ROOT / "secrets" / "codebeamer.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write_project_auth(project_id: int, host: str, pswd: str) -> Path:
    payload = {
        "servers": {"confluence": {"env": {"CODEBEAMER_HOST": host, "CODEBEAMER_PSWD": pswd}}}
    }
    path = (CORPUS_ROOT / str(project_id) / ".sync")
    path.mkdir(parents=True, exist_ok=True)
    auth_path = path / "auth.json"
    auth_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return auth_path


def _load_saved_pswd() -> dict[str, str] | None:
    try:
        data = json.loads(_secrets_path().read_text(encoding="utf-8"))
        host = str(data.get("codebeamer_host") or "").strip()
        username = str(data.get("username") or "").strip()
        pswd = str(data.get("pswd") or "").strip()
        if host and username and pswd:
            return {"codebeamer_host": host, "username": username, "pswd": pswd}
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
# Background task infrastructure
# ---------------------------------------------------------------------------

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


def _load_selection(project_id: int) -> list[int]:
    sel_path = CORPUS_ROOT / str(project_id) / ".sync" / "selection.json"
    data = _load_json(sel_path) or {}
    return list(data.get("include_ids") or [])


# ---------------------------------------------------------------------------
# Pipeline functions (run in background threads)
# ---------------------------------------------------------------------------

def _cb_evaluate(project_id: int, saved: dict, workers: int = 4) -> None:
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
    _update_cb_status(project_id, "evaluate", {
        "phase": "traverse",
        "progress": {"total": 0, "done": 0},
        "started_at": started_wall,
    })

    client = _make_client(saved)

    # Resolve project name
    project_resp = client._client.get(f"projects/{project_id}")
    if not isinstance(project_resp, dict):
        raise SystemExit(f"Project {project_id} not found")
    project_name = str(project_resp.get("name") or project_id)

    # GET /projects/{id}/trackers returns {"trackers": [...], "total": N, ...}
    trackers_resp = client._client.get(f"projects/{project_id}/trackers")
    if isinstance(trackers_resp, dict):
        raw_trackers: list[dict] = trackers_resp.get("trackers") or []
    elif isinstance(trackers_resp, list):
        raw_trackers = trackers_resp
    else:
        raw_trackers = []

    total_trackers = len(raw_trackers)
    _CONF_LOG.info("evaluate: project_id=%d name=%s trackers=%d", project_id, project_name, total_trackers)
    time.sleep(0.8)

    _update_cb_status(project_id, "evaluate", {
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
        page, page_size = 1, 500
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
                _CONF_LOG.warning("evaluate: tracker %s failed: %s", t.get("id"), exc)
                plan_entry = {"id": t["id"], "name": str(t.get("name") or t["id"]), "item_count": 0}
                refs = []
            tracker_results[t["id"]] = (plan_entry, refs)
            done_trackers += 1
            _update_cb_status(project_id, "evaluate", {
                "phase": "items",
                "progress": {"total": total_trackers, "done": done_trackers},
                "started_at": started_wall,
            })
            _CONF_LOG.info("evaluate: tracker %d (%s) items=%d", t["id"], t.get("name"), len(refs))

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

    _CONF_LOG.info(
        "evaluate done: project_id=%d name=%s total_items=%d added=%d changed=%d deleted=%d",
        project_id, project_name, total_items, len(delta["added"]), len(delta["changed"]), len(delta["deleted"]),
    )



def _fetch_item_content(item_id: int, item_name: str, staging_dir: Path, saved: dict, retries: int = 3) -> dict:
    for attempt in range(1, retries + 1):
        try:
            tc = _make_client(saved)
            data = tc._client.get(f"items/{item_id}")
            if not isinstance(data, dict):
                raise ValueError(f"unexpected response type: {type(data)}")
            (staging_dir / f"{item_id}.json").write_text(
                json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
            time.sleep(0.5)
            return {"ok": True, "item_id": item_id, "name": item_name, "attempts": attempt}
        except Exception as exc:
            if attempt == retries:
                return {"ok": False, "item_id": item_id, "name": item_name, "error": str(exc), "attempts": attempt}
    return {"ok": False, "item_id": item_id, "name": item_name, "error": "max retries", "attempts": retries}


def _cb_pull(project_id: int, saved: dict, workers: int = 4) -> None:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    started_wall = time.time()
    sync_dir = CORPUS_ROOT / str(project_id) / ".sync"
    staging_dir = CORPUS_ROOT / str(project_id) / "staging"

    # Ensure plan exists
    plan: dict = {}
    try:
        plan = json.loads((sync_dir / "plan.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    if not plan or plan.get("project_id") != project_id:
        raise SystemExit("plan.json missing or stale; run evaluate first")

    # Load selection
    selection_ids: set[int] = set()
    try:
        sel = json.loads((sync_dir / "selection.json").read_text(encoding="utf-8"))
        selection_ids = {int(x) for x in (sel.get("include_ids") or [])}
    except Exception:
        pass

    # Load delta
    delta_ids: set[int] = set()
    try:
        delta = json.loads((sync_dir / "delta.json").read_text(encoding="utf-8"))
        delta_ids = {int(x) for x in (delta.get("added") or []) + (delta.get("changed") or [])}
    except Exception:
        pass

    # Load manifest
    manifest: dict[str, dict] = {}
    try:
        manifest = json.loads((sync_dir / "manifest.json").read_text(encoding="utf-8"))
    except Exception:
        pass

    # selection > delta > full manifest
    if selection_ids:
        ids_to_fetch = selection_ids
        mode = "selection"
    elif delta_ids:
        ids_to_fetch = delta_ids
        mode = "delta"
    else:
        ids_to_fetch = {int(k) for k in manifest}
        mode = "manifest"

    _CONF_LOG.info("pull: project_id=%d mode=%s ids=%d", project_id, mode, len(ids_to_fetch))

    if not ids_to_fetch:
        _CONF_LOG.info("pull: nothing to fetch")
        return

    staging_dir.mkdir(parents=True, exist_ok=True)
    item_meta = [(iid, (manifest.get(str(iid)) or {}).get("name") or str(iid)) for iid in sorted(ids_to_fetch)]
    total = len(item_meta)
    workers = max(1, min(int(workers or 4), 16))
    successes = 0
    failures = 0
    failed_items: list[dict] = []
    print(f"Starting pull with {workers} worker(s) — {total} page(s)…", flush=True)

    _update_cb_status(project_id, "pull", {
        "phase": "fetch",
        "progress": {"total": total, "done": 0, "failed": 0},
        "started_at": started_wall,
        "eta_sec": 0.0,
    })

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = [ex.submit(_fetch_item_content, iid, name, staging_dir, saved) for iid, name in item_meta]
        for fut in as_completed(futures):
            res = fut.result()
            if res.get("ok"):
                successes += 1
            else:
                failures += 1
                failed_items.append(res)
                _CONF_LOG.warning("pull: FAILED item_id=%s error=%s", res.get("item_id"), res.get("error"))
            done = successes + failures
            if done % 5 == 0 or done == total:
                elapsed = time.time() - started_wall
                eta = (total - done) * (elapsed / done) if done > 0 and total > done else 0.0
                _update_cb_status(project_id, "pull", {
                    "phase": "fetch",
                    "progress": {"total": total, "done": done, "failed": failures},
                    "started_at": started_wall,
                    "eta_sec": max(0.0, float(eta)),
                })

    if failed_items:
        fail_path = APP_ROOT / "logs" / f"codebeamer_pull_failed_{project_id}.json"
        fail_path.parent.mkdir(parents=True, exist_ok=True)
        fail_path.write_text(json.dumps(failed_items, indent=2, ensure_ascii=False), encoding="utf-8")

    _CONF_LOG.info("pull done: project_id=%d fetched=%d failed=%d mode=%s", project_id, successes, failures, mode)
    _update_cb_status(project_id, "pull", {
        "phase": "done",
        "progress": {"total": total, "done": total, "failed": failures},
        "started_at": started_wall,
        "elapsed_sec": round(time.time() - started_wall, 3),
        "eta_sec": 0.0,
    })



def _cb_build(project_id: int, include_ids: list[int], saved: dict) -> None:
    """Fetch all tracker items and write chunks.staging.json.

    Uses /items/query directly (raw REST) to avoid the N+1 get_fields() call
    that TrackerItem._load() would trigger when constructing TrackerItem objects.
    """
    client = _make_client(saved)
    project_dir = CORPUS_ROOT / str(project_id)
    project_dir.mkdir(parents=True, exist_ok=True)

    chunks: list[dict] = []
    for tracker_id in include_ids:
        tracker = client.get_tracker(tracker_id)
        tracker_name = tracker.name if tracker else str(tracker_id)

        page = 1
        page_size = 100
        while True:
            resp = client._client.post(
                "items/query",
                json_={
                    "queryString": f"tracker.id IN ({tracker_id})",
                    "page": page,
                    "pageSize": page_size,
                },
            )
            if not isinstance(resp, dict):
                break
            raw_items: list[dict] = resp.get("items") or []
            for item in raw_items:
                item_id = item.get("id")
                name = str(item.get("name") or "")
                description = str(item.get("description") or "")
                chunks.append({
                    "chunk_id": f"cb_{project_id}_{tracker_id}_{item_id}",
                    "item_id": item_id,
                    "tracker_id": tracker_id,
                    "tracker_name": tracker_name,
                    "heading_path": [tracker_name, name],
                    "content": f"{name}\n{description}".strip(),
                    "source": {"project_id": project_id, "tracker_id": tracker_id, "item_id": item_id},
                })
            total = int(resp.get("total") or 0)
            if page * page_size >= total:
                break
            page += 1

        _CONF_LOG.info("build: tracker %s (%s) fetched", tracker_id, tracker_name)

    staging = project_dir / "chunks.staging.json"
    staging.write_text(json.dumps(chunks, indent=2, ensure_ascii=False), encoding="utf-8")
    _CONF_LOG.info("build done: project_id=%s chunks=%d", project_id, len(chunks))


def _cb_activate(project_id: int) -> None:
    """Promote staging chunks to active."""
    project_dir = CORPUS_ROOT / str(project_id)
    staging = project_dir / "chunks.staging.json"
    active = project_dir / "chunks.json"
    if not staging.exists():
        raise SystemExit(f"Staging chunks not found; run 'build' first: {staging}")
    shutil.copy2(staging, active)
    _CONF_LOG.info("activate done: project_id=%s", project_id)


# ---------------------------------------------------------------------------
# Credentials endpoints
# ---------------------------------------------------------------------------

@codebeamer_router.post("/codebeamer/pswd/test", tags=["codebeamer"])
def codebeamer_pswd_test(req: CodebeamerPswdRequest) -> Dict[str, Any]:
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
def codebeamer_pswd_get() -> Dict[str, Any]:
    saved = _load_saved_pswd()
    if saved is not None:
        host = str(saved.get("codebeamer_host") or "").strip()
        username = str(saved.get("username") or "").strip()
        has_pswd = bool(saved.get("pswd") or "")
        return {"ok": True, "codebeamer_host": host, "username": username, "has_pswd": has_pswd}
    return {"ok": False, "error": "No saved credentials found"}



@codebeamer_router.post("/codebeamer/pswd/save", tags=["codebeamer"])
def codebeamer_pswd_save(req: CodebeamerPswdSaveRequest) -> Dict[str, Any]:
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
    _secrets_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"ok": True}


# ---------------------------------------------------------------------------
# Project / tree endpoints
# ---------------------------------------------------------------------------

@codebeamer_router.get("/codebeamer/projects", tags=["codebeamer"])
def codebeamer_projects() -> Dict[str, Any]:
    _CONF_LOG.info("GET /codebeamer/projects")
    saved = _load_saved_pswd()
    if not saved:
        raise HTTPException(status_code=400, detail="Password not configured")
    try:
        client = _make_client(saved)
        projects = client.get_projects() or []
        nodes = [{"id": p.id, "name": p.name} for p in projects]
        _CONF_LOG.info("projects resolved count=%d", len(nodes))
        return {"ok": True, "projects": nodes}
    except Exception as exc:
        _CONF_LOG.exception("projects error err=%s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@codebeamer_router.get("/codebeamer/tree", tags=["codebeamer"])
def codebeamer_tree(project_id: int) -> Dict[str, Any]:
    _CONF_LOG.info("GET /codebeamer/tree project_id=%s", project_id)
    saved = _load_saved_pswd()
    if not saved:
        raise HTTPException(status_code=400, detail="Password not configured")
    try:
        client = _make_client(saved)
        project = client.get_project(project_id)
        if project is None:
            raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
        trackers = project.get_trackers() or []
        nodes = [
            {
                "id": t.id,
                "name": t.name,
                "type_id": getattr(t, "type_id", None),
                "type_name": getattr(t, "type_name", None),
            }
            for t in trackers
        ]
        _CONF_LOG.info("tree resolved project_id=%s trackers=%d", project_id, len(nodes))
        return {"ok": True, "project_id": project_id, "trackers": nodes}
    except HTTPException:
        raise
    except Exception as exc:
        _CONF_LOG.exception("tree error project_id=%s err=%s", project_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@codebeamer_router.get("/codebeamer/tree/full", tags=["codebeamer"])
def codebeamer_tree_full(project_id: int) -> Dict[str, Any]:
    _CONF_LOG.info("GET /codebeamer/tree/full project_id=%s", project_id)
    tree_path = CORPUS_ROOT / str(project_id) / ".sync" / "tree.json"
    try:
        text = tree_path.read_text(encoding="utf-8")
        data = json.loads(text)
        trackers = data.get("trackers") or []
        _CONF_LOG.info("tree_full project_id=%s trackers=%d size=%dB", project_id, len(trackers), len(text.encode("utf-8")))
    except Exception:
        _CONF_LOG.warning("tree_full missing for project_id=%s", project_id)
        raise HTTPException(status_code=404, detail="Full tree not available; run Evaluate first")
    return {"ok": True, **data}


@codebeamer_router.post("/codebeamer/tracker/{tracker_id}/items", tags=["codebeamer"])
def codebeamer_push_items(tracker_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
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
    _CONF_LOG.info("POST /codebeamer/tracker/%s/items count=%d", tracker_id, len(items))

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
        _CONF_LOG.exception("push_items: tracker resolution failed tracker_id=%s err=%s", tracker_id, exc)
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
            _CONF_LOG.warning("push_items: failed to create item '%s' err=%s", name, exc)
            failed.append({"name": name, "error": str(exc)})

    _CONF_LOG.info(
        "push_items done tracker_id=%s created=%d failed=%d",
        tracker_id, len(created), len(failed),
    )
    return {"ok": True, "created": created, "failed": failed}


# ---------------------------------------------------------------------------
# Selection endpoints
# ---------------------------------------------------------------------------

@codebeamer_router.post("/codebeamer/selection", tags=["codebeamer"])
def codebeamer_selection(req: CodebeamerSelectionRequest) -> Dict[str, Any]:
    _CONF_LOG.info("/codebeamer/selection request project_id=%s include_ids=%d exclude_ids=%d",
                   req.project_id, len(req.include_ids), len(req.exclude_ids or []))
    path = CORPUS_ROOT / str(req.project_id) / ".sync"
    path.mkdir(parents=True, exist_ok=True)
    sel = {"include_ids": req.include_ids, "exclude_ids": req.exclude_ids or []}
    (path / "selection.json").write_text(json.dumps(sel, indent=2), encoding="utf-8")
    return {"ok": True}


@codebeamer_router.get("/codebeamer/selection", tags=["codebeamer"])
def codebeamer_selection_get(project_id: int) -> Dict[str, Any]:
    _CONF_LOG.info("/codebeamer/selection get request project_id=%s", project_id)
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
# Last-project endpoint
# ---------------------------------------------------------------------------

@codebeamer_router.get("/codebeamer/last_project", tags=["codebeamer"])
def codebeamer_last_project() -> Dict[str, Any]:
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
# Pipeline endpoints
# ---------------------------------------------------------------------------

@codebeamer_router.post("/codebeamer/evaluate", tags=["codebeamer"])
def codebeamer_evaluate(req: CodebeamerEvaluateRequest) -> Dict[str, Any]:
    _CONF_LOG.info("POST /codebeamer/evaluate project_id=%d", req.project_id)

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


@codebeamer_router.post("/codebeamer/pull", tags=["codebeamer"])
def codebeamer_pull(req: CodebeamerPullRequest) -> Dict[str, Any]:
    _CONF_LOG.info("POST /codebeamer/pull project_id=%d", req.project_id)

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



@codebeamer_router.post("/codebeamer/build", tags=["codebeamer"])
def codebeamer_build(req: CodebeamerBuildRequest) -> Dict[str, Any]:
    _CONF_LOG.info("POST /codebeamer/build project_id=%d", req.project_id)
    saved = _load_saved_pswd()
    if not saved:
        raise HTTPException(status_code=400, detail="Password not configured")
    try:
        include_ids = _load_selection(req.project_id)
    except Exception:
        raise HTTPException(status_code=400, detail="No selection found; call /codebeamer/selection first")
    if not include_ids:
        raise HTTPException(status_code=400, detail="include_ids is empty")

    existing = _CB_TASKS.get(req.project_id, {}).get("thread")
    if existing and existing.is_alive():
        raise HTTPException(status_code=409, detail="Build already running for this project")

    run_id = str(_uuid.uuid4())
    t = threading.Thread(
        target=_run_cb_task,
        args=(req.project_id, _cb_build, req.project_id, include_ids, saved),
        kwargs={"__stage": "build"},
        daemon=True,
    )
    _CB_TASKS.setdefault(req.project_id, {})["thread"] = t
    t.start()
    return {"ok": True, "run_id": run_id}


@codebeamer_router.post("/codebeamer/activate", tags=["codebeamer"])
def codebeamer_activate(req: CodebeamerActivateRequest) -> Dict[str, Any]:
    _CONF_LOG.info("POST /codebeamer/activate project_id=%d", req.project_id)
    run_id = str(_uuid.uuid4())
    t = threading.Thread(
        target=_run_cb_task,
        args=(req.project_id, _cb_activate, req.project_id),
        kwargs={"__stage": "activate"},
        daemon=True,
    )
    _CB_TASKS.setdefault(req.project_id, {})["thread"] = t
    t.start()
    return {"ok": True, "run_id": run_id}


@codebeamer_router.get("/codebeamer/status", tags=["codebeamer"])
def codebeamer_status(project_id: int) -> Dict[str, Any]:
    _CONF_LOG.info("GET /codebeamer/status project_id=%d", project_id)

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
