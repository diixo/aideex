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

APP_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = APP_ROOT / "corpus"

_CB_LOG = logging.getLogger("backend.codebeamer")

codebeamer_router = APIRouter(tags=["codebeamer"])


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


# ---------------------------------------------------------------------------
# Credentials helpers
# ---------------------------------------------------------------------------

def _secrets_path() -> Path:
    p = APP_ROOT / "secrets" / "codebeamer.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


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
# Selection endpoints
# ---------------------------------------------------------------------------

@codebeamer_router.post("/codebeamer/selection", tags=["codebeamer"])
def codebeamer_selection(req: CodebeamerSelectionRequest) -> Dict[str, Any]:
    _CB_LOG.info("/codebeamer/selection request project_id=%s include_ids=%d exclude_ids=%d",
                   req.project_id, len(req.include_ids), len(req.exclude_ids or []))
    path = CORPUS_ROOT / str(req.project_id) / ".sync"
    path.mkdir(parents=True, exist_ok=True)
    sel = {"include_ids": req.include_ids, "exclude_ids": req.exclude_ids or []}
    (path / "selection.json").write_text(json.dumps(sel, indent=2), encoding="utf-8")
    return {"ok": True}


@codebeamer_router.get("/codebeamer/selection", tags=["codebeamer"])
def codebeamer_selection_get(project_id: int) -> Dict[str, Any]:
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

