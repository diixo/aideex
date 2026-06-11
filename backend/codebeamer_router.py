import logging
import json
import shutil
import subprocess
import sys
import threading
import time
import uuid as _uuid
from collections import defaultdict
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
_CB_ITEMS_IDX: dict[int, dict[int, dict]] = {}  # project_id → {item_id → raw_item}
_CB_TRACKER_PROCESS_MAP: dict[int, dict[int, str]] = {}  # project_id → {tracker_id → process_area}
_CB_CLASSIFYING: set[int] = set()  # projects currently being classified in background
_CB_LOAD_LOCKS: dict[int, threading.Lock] = {}   # one lock per project to prevent duplicate corpus loads
_CB_LOAD_STATE: dict[int, dict] = {}             # project_id → {stage, file_size_mb, items}


def _tracker_map_cache_path(project_id: int) -> Path:
    return CORPUS_ROOT / str(project_id) / "tracker_process_map.json"


def _load_tracker_map_cache(project_id: int) -> dict[int, str] | None:
    p = _tracker_map_cache_path(project_id)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return {int(k): v for k, v in raw.items()}
    except Exception:
        return None


def _save_tracker_map_cache(project_id: int, mapping: dict[int, str]) -> None:
    p = _tracker_map_cache_path(project_id)
    try:
        p.write_text(json.dumps({str(k): v for k, v in mapping.items()}, indent=2), encoding="utf-8")
    except Exception as exc:
        _CB_LOG.warning("could not save tracker map cache: %s", exc)


def _claude_argv(prompt: str) -> tuple[list[str], str]:
    """Return (argv, stdin_input) for a claude subprocess call.

    The prompt is passed via stdin to avoid Windows command-line length limits
    (~8191 chars).  Claude Code enters non-interactive print mode automatically
    when its stdin is a pipe rather than a TTY.
    On Windows the .cmd wrapper requires cmd.exe.
    """
    if sys.platform == "win32":
        return ["cmd", "/c", "claude"], prompt
    return ["claude"], prompt


def _classify_trackers_claude(trackers: list[dict]) -> dict[int, str]:
    """Use claude CLI to classify trackers to ASPICE process areas.

    Calls `claude -p <prompt>` as a subprocess and parses the JSON response.
    Returns {tracker_id: process_area} or empty dict on failure (caller falls
    back to name-based heuristics).
    """
    tracker_list = [
        {
            "id": t.get("id"),
            "name": t.get("name", ""),
            "type": (t.get("type") or {}).get("name", ""),
        }
        for t in trackers
        if t.get("id")
    ]
    if not tracker_list:
        return {}

    prompt = (
        "You are an ASPICE process expert. Classify each Codebeamer tracker to an ASPICE process area.\n"
        "Process areas:\n"
        "  SYS.4    – System Integration Test\n"
        "  SYS.5    – System Qualification Test / Acceptance Test\n"
        "  SWE.4    – Software Unit Verification / Component Verification\n"
        "  SWE.5    – Software Integration Verification\n"
        "  SEC      – Security / CySec testing\n"
        "  UNMAPPED – cannot determine or not a test tracker\n\n"
        "Trackers (JSON list):\n"
        + json.dumps(tracker_list, ensure_ascii=False)
        + "\n\nReturn ONLY a valid JSON object mapping tracker id (string key) to process area string.\n"
        "Example: {\"123\": \"SYS.4\", \"456\": \"UNMAPPED\"}\n"
        "No explanation, no markdown, just the JSON object."
    )

    try:
        argv, stdin_data = _claude_argv(prompt)
        result = subprocess.run(
            argv,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0:
            _CB_LOG.warning("claude classify exited %d: %s", result.returncode, result.stderr[:200])
            return {}
        text = result.stdout.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end <= start:
            _CB_LOG.warning("claude classify: no JSON in response: %s", text[:200])
            return {}
        mapping = json.loads(text[start:end])
        return {int(k): str(v) for k, v in mapping.items() if v}
    except Exception as exc:
        _CB_LOG.warning("claude classify failed: %s", exc)
        return {}


def _analyze_gaps_claude(
    summary: dict,
    process_breakdown: list,
    gaps: list,
    sample_items: Optional[list] = None,
) -> str:
    """Use claude CLI to produce a text analysis of ASPICE traceability gaps.

    sample_items: list of {id, name, description} for requirements lacking test cases.
    When provided, Claude also checks for missing behavioral elements (triggers,
    timing constraints, acceptance criteria, exceptions) in the requirement texts.
    """
    from collections import Counter
    gap_counts = dict(Counter(g["gap_code"] for g in gaps))
    gap_by_process: dict[str, list] = {}
    for g in gaps:
        gap_by_process.setdefault(g.get("process_area", "UNMAPPED"), []).append(g["gap_code"])

    prompt = (
        "You are an ASPICE test coverage expert. Analyze the following test coverage data "
        "for a Codebeamer project and produce a concise actionable assessment.\n\n"
        "## Summary metrics\n"
        + json.dumps(summary, indent=2)
        + "\n\n## Process breakdown\n"
        + json.dumps(process_breakdown, indent=2)
        + "\n\n## Gap counts by type\n"
        + json.dumps(gap_counts, indent=2)
        + "\n\n## Gap counts by process area\n"
        + json.dumps({k: dict(Counter(v)) for k, v in gap_by_process.items()}, indent=2)
        + "\n\n## Sample gaps (first 25)\n"
        + json.dumps(gaps[:25], indent=2)
    )

    if sample_items:
        prompt += (
            "\n\n## Sample requirements lacking test cases (for behavioral analysis)\n"
            + json.dumps(sample_items, indent=2, ensure_ascii=False)
            + "\n\nFor each sample requirement above, identify which behavioral elements are missing:\n"
            "trigger, preconditions, system actions, timing constraints, exceptions, acceptance criteria.\n"
            "A requirement that lacks these elements cannot be reliably tested."
        )

    prompt += (
        "\n\nProvide your analysis in plain text (no markdown headers, no bullet symbols):\n"
        "1. Overall ASPICE test coverage assessment — 2-3 sentences.\n"
        "2. Top 3 critical traceability gap chains to fix, with process area context.\n"
        "3. Behavioral completeness issues found in sample requirements (if provided).\n"
        "4. Prioritized recommendations — what to do first to improve the score.\n"
        "Be concise, specific, and focused on ASPICE compliance."
    )

    try:
        argv, stdin_data = _claude_argv(prompt)
        result = subprocess.run(
            argv,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            _CB_LOG.warning("claude gap analysis exited %d: %s", result.returncode, result.stderr[:200])
            return ""
        return result.stdout.strip()
    except Exception as exc:
        _CB_LOG.warning("claude gap analysis failed: %s", exc)
        return ""


def _extract_behavior_scenario_claude(item: dict) -> dict:
    """Use claude CLI to extract a structured behavior scenario from a requirement.

    Returns a dict with keys: actors, trigger, preconditions, system_actions,
    states, outputs, exceptions, timing_constraints, acceptance_criteria,
    test_case_ideas. Empty dict on failure.
    """
    name = (item.get("name") or "").strip()
    desc = (item.get("description") or "").strip()
    if not name and not desc:
        return {}

    prompt = (
        "You are an ASPICE/systems engineering expert. "
        "Extract a structured behavior scenario from the following requirement.\n\n"
        f"Requirement name: {name}\n"
        f"Description: {desc[:1200]}\n\n"
        "Return ONLY a valid JSON object with exactly these keys:\n"
        "{\n"
        '  "actors": ["list of actors involved"],\n'
        '  "trigger": "what event or condition initiates this behavior",\n'
        '  "preconditions": ["conditions that must hold before the behavior starts"],\n'
        '  "system_actions": ["ordered list of system actions"],\n'
        '  "states": ["system states involved"],\n'
        '  "outputs": ["outputs, signals or results produced"],\n'
        '  "exceptions": ["error or alternative behaviors"],\n'
        '  "timing_constraints": ["any timing or performance requirements"],\n'
        '  "acceptance_criteria": ["verifiable pass/fail criteria"],\n'
        '  "test_case_ideas": ["concrete test case ideas derived from this behavior"]\n'
        "}\n"
        "If a field cannot be determined from the text, use an empty list (or empty string for trigger).\n"
        "No explanation, no markdown — only the JSON object."
    )

    try:
        argv, stdin_data = _claude_argv(prompt)
        result = subprocess.run(
            argv,
            input=stdin_data,
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0:
            _CB_LOG.warning("claude behavior scenario exited %d", result.returncode)
            return {}
        text = result.stdout.strip()
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end <= start:
            return {}
        return json.loads(text[start:end])
    except Exception as exc:
        _CB_LOG.warning("claude behavior scenario failed: %s", exc)
        return {}


def _item_display_type(iid: int, item_tracker: dict, tracker_meta: dict) -> str:
    """Classify an item into a display type for the traceability graph."""
    tid = item_tracker.get(iid)
    if tid is None:
        return "unknown"
    meta = tracker_meta.get(int(tid), {})
    t = (meta.get("type_name") or "").lower()
    n = (meta.get("name") or "").lower()
    if t == "testrun":
        return "testrun"
    if t in ("testcase", "testset", "testconf"):
        return "testcase"
    if t in ("bug", "issue", "defect"):
        return "defect"
    if t == "requirement":
        if any(k in n for k in ("stakeholder", "customer", "user story", "epic")):
            return "stakeholder"
        if any(k in n for k in ("software", "swe", "component", "unit")):
            return "software_req"
        if any(k in n for k in ("system", "sys")):
            return "system_req"
        return "requirement"
    return t or "unknown"


def _corpus_json_path(project_id: int) -> Path:
    p = CORPUS_ROOT / str(project_id) / "codebeamer.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _load_corpus(project_id: int) -> dict | None:
    """Return corpus for project_id from memory; load from disk on first access.

    Thread-safe: uses a per-project lock so concurrent requests don't trigger
    duplicate disk reads. Loading state is tracked in _CB_LOAD_STATE.
    """
    if project_id in _CB_CORPUS:
        return _CB_CORPUS[project_id]

    if project_id not in _CB_LOAD_LOCKS:
        _CB_LOAD_LOCKS[project_id] = threading.Lock()

    with _CB_LOAD_LOCKS[project_id]:
        # Double-check: another thread may have loaded it while we waited
        if project_id in _CB_CORPUS:
            return _CB_CORPUS[project_id]

        path = _corpus_json_path(project_id)
        if not path.exists():
            return None

        try:
            file_size_mb = round(path.stat().st_size / 1024 / 1024, 1)
            _CB_LOAD_STATE[project_id] = {"stage": "reading", "file_size_mb": file_size_mb, "items": 0}
            text = path.read_text(encoding="utf-8")

            _CB_LOAD_STATE[project_id] = {"stage": "parsing", "file_size_mb": file_size_mb, "items": 0}
            data = json.loads(text)

            items = [it for it in data.get("items", []) if it.get("id")]
            _CB_LOAD_STATE[project_id] = {"stage": "indexing", "file_size_mb": file_size_mb, "items": len(items)}
            _CB_ITEMS_IDX[project_id] = {it["id"]: it for it in items}
            _CB_ITEM_MAPS[project_id] = _build_item_map(data)
            _CB_CORPUS[project_id] = data

            _CB_LOAD_STATE[project_id] = {"stage": "ready", "file_size_mb": file_size_mb, "items": len(items)}
            return data
        except Exception as exc:
            _CB_LOAD_STATE[project_id] = {"stage": "error", "file_size_mb": 0, "items": 0, "error": str(exc)}
            return None


def _get_items_by_ids(project_id: int, item_ids: set) -> list[dict]:
    """Return full item objects for given ids using the pre-built index."""
    idx = _CB_ITEMS_IDX.get(project_id)
    if idx is None:
        return []
    return [idx[iid] for iid in item_ids if iid in idx]


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
        color = STATUS_COLORS.get(status, t_color) if status else STATUS_COLORS.get("New")
        upstream = [
            s["id"] for s in (item.subjects or [])
            if isinstance(s, dict) and s.get("id")
        ]
        for cf in (raw_item.get("customFields") or []):
            if cf.get("type") == "ChoiceFieldValue":
                for ref in (cf.get("values") or []):
                    if isinstance(ref, dict) and ref.get("id") and ref.get("type") == "TrackerItemReference" and ref.get("id") == ref.get("commonItemId"):
                        upstream.append(ref["id"])
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
            "project_id": saved.get("project_id"),
            "project_name": saved.get("project_name"),
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
        project_name = payload.get("project_name")
        if project_name:
            cur["project_name"] = project_name
        _secrets_path().write_text(json.dumps(cur, indent=2, ensure_ascii=False), encoding="utf-8")
        corpus = _load_corpus(project_id)
        if corpus:
            _CB_ITEM_MAPS[project_id] = _build_item_map(corpus)
            _CB_LOG.info("item_map built from corpus project_id=%d items=%d", project_id, len(_CB_ITEM_MAPS[project_id]))
        else:
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


@codebeamer_router.get("/codebeamer/trackers", tags=["codebeamer"])
async def codebeamer_trackers(project_id: int) -> Dict[str, Any]:
    """Return raw tracker list from corpus. Returns ok=False if corpus not available."""
    corpus = _load_corpus(project_id)
    if not corpus:
        return {"ok": False, "error": "Corpus not found — run Pull first"}
    return {"ok": True, "trackers": corpus.get("trackers", [])}


@codebeamer_router.get("/codebeamer/tracker/{tracker_id}/corpus", tags=["codebeamer"])
async def codebeamer_tracker_corpus(tracker_id: int, project_id: int) -> Dict[str, Any]:
    """Return tracker metadata + its full items from corpus, keyed by tracker_id."""
    corpus = _load_corpus(project_id)
    if not corpus:
        return {"ok": False, "error": "Corpus not found — run Pull first"}

    raw_tracker = next((t for t in corpus.get("trackers", []) if t.get("id") == tracker_id), None)
    if raw_tracker is None:
        return {"ok": False, "error": f"Tracker {tracker_id} not found in corpus"}

    item_ids = {ref["id"] for ref in raw_tracker.get("items", []) if ref.get("id")}
    items = _get_items_by_ids(project_id, item_ids)

    return {"ok": True, "tracker": raw_tracker, "items": items}


@codebeamer_router.get("/codebeamer/tree-view", tags=["codebeamer"])
async def codebeamer_tree_view(project_id: int) -> Dict[str, Any]:
    """Return raw tree nodes (from tree.json) + tracker metadata lookup (from corpus).
    Serialization is done on the caller side.
    """
    sync_dir = CORPUS_ROOT / str(project_id) / ".sync"
    try:
        raw_tree_doc = json.loads((sync_dir / "tree.json").read_text(encoding="utf-8"))
    except Exception:
        return {"ok": False, "error": "tree.json not found — run Evaluate first"}

    corpus = _load_corpus(project_id)
    tracker_meta: dict[str, dict] = {}
    if corpus:
        for t in corpus.get("trackers", []):
            tid = t.get("id")
            if tid:
                t_type = t.get("type") or {}
                tracker_meta[str(tid)] = {
                    "name":      t.get("name", ""),
                    "type_id":   t_type.get("id", -1),
                    "type_name": t_type.get("name"),
                    "color":     t.get("color") or "#6c757d",
                }

    return {
        "ok":           True,
        "project_name": raw_tree_doc.get("project_name", ""),
        "tree":         raw_tree_doc.get("tree", []),
        "tracker_meta": tracker_meta,
    }


@codebeamer_router.get("/codebeamer/local-projects", tags=["codebeamer"])
async def codebeamer_local_projects() -> Dict[str, Any]:
    """Return projects that have been pulled locally (exist in corpus/)."""
    projects = []
    if CORPUS_ROOT.exists():
        for p in sorted(CORPUS_ROOT.iterdir()):
            if not p.is_dir():
                continue
            try:
                project_id = int(p.name)
            except ValueError:
                continue
            corpus = _load_corpus(project_id)
            if corpus:
                projects.append({
                    "id":   project_id,
                    "name": corpus.get("project_name") or str(project_id),
                })
    return {"ok": True, "projects": projects}


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
    project_name = project.name

    # Fetch hierarchical tree — gives us Folder structure + flat tracker list in one call
    raw_tree: list[dict] = client._client.get("trackers/tree", params={"projectId": project_id})
    if not isinstance(raw_tree, list):
        raw_tree = []

    def _flatten_tree(nodes: list[dict]) -> list[dict]:
        result = []
        for node in nodes:
            if node.get("isFolder"):
                result.extend(_flatten_tree(node.get("children") or []))
            elif node.get("trackerId"):
                result.append({"id": node["trackerId"], "name": str(node.get("text") or node["trackerId"])})
        return result

    raw_trackers = _flatten_tree(raw_tree)
    total_trackers = len(raw_trackers)
    _CB_LOG.info("evaluate: project_id=%d name=%s trackers=%d", project_id, project_name, total_trackers)

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
        #time.sleep(0.6)
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

    # Enrich tree nodes with fetched item refs (in-place copy, preserving hierarchy)
    def _enrich_tree(nodes: list[dict]) -> list[dict]:
        result = []
        for node in nodes:
            if node.get("isFolder"):
                result.append({**node, "children": _enrich_tree(node.get("children") or [])})
            elif node.get("trackerId"):
                tid = node["trackerId"]
                _, refs = tracker_results.get(tid, ({}, []))
                result.append({**node, "items": refs})
            else:
                result.append(node)
        return result

    enriched_tree = _enrich_tree(raw_tree)

    # Assemble manifest and plan from flat tracker list
    current_manifest: dict[str, dict] = {}
    trackers_plan: list[dict] = []
    total_items = 0

    for tracker in raw_trackers:
        tid = tracker["id"]
        plan_entry, refs = tracker_results[tid]
        trackers_plan.append(plan_entry)
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
    tree = {
        "project_id":   project_id,
        "project_name": project_name,
        "tree":         enriched_tree,  # hierarchical nodes with items; flatten to get tracker list
    }

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
    _update_cb_status(req.project_id, "evaluate")
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


# Override map for specific tracker IDs from a known project.
# For other projects the name-based fallback in _tracker_process_area() is used.
_TEST_PROCESS_BY_TRACKER_ID = {
    87637: "SYS.5",  # System Verification Test Cases
    88271: "SYS.5",  # Acceptance Testing Test Cases
    87749: "SYS.4",  # System Integration Test Cases
    87848: "SWE.4",  # Software Verification Test Cases
    88020: "SWE.4",  # Software Component Verification Test Cases
    87934: "SWE.5",  # Software Integration Verification Test Cases
    146738: "SEC",   # CySec Validation Test Cases
    88425: "SYS.5",  # Test Runs Execution
    88370: "SYS.5",  # Test Sets
    88612: "SUP.9",  # Defects
}


def _tracker_process_area(tracker_id: int, tracker_name: str) -> str:
    if tracker_id in _TEST_PROCESS_BY_TRACKER_ID:
        return _TEST_PROCESS_BY_TRACKER_ID[tracker_id]

    name = (tracker_name or "").lower()
    if "cysec" in name or "security" in name:
        return "SEC"
    if "acceptance" in name:
        return "SYS.5"
    if "system integration" in name:
        return "SYS.4"
    if "system verification" in name:
        return "SYS.5"
    if "software integration" in name:
        return "SWE.5"
    if "component verification" in name or "software verification" in name:
        return "SWE.4"
    return "UNMAPPED"


def _item_status_name(raw_item: dict) -> str:
    st = raw_item.get("status")
    if isinstance(st, dict):
        return str(st.get("name") or "")
    if isinstance(st, str):
        return st
    return ""


def _item_timestamp(raw_item: dict) -> Optional[datetime]:
    for key in ("modifiedAt", "closedAt", "createdAt", "submittedAt"):
        val = raw_item.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        try:
            return datetime.fromisoformat(val.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _run_outcome(raw_item: dict) -> str:
    status = _item_status_name(raw_item).lower()
    if any(tok in status for tok in ("pass", "passed", "success", "ok", "finished")):
        return "passed"
    if any(tok in status for tok in ("fail", "failed", "error", "blocked")):
        return "failed"
    return "other"


def _normalize_feature_key(name: str) -> str:
    txt = (name or "").strip()
    if not txt:
        return "Uncategorized"
    if "-" in txt:
        return txt.split("-", 1)[0].strip() or txt
    if "/" in txt:
        return txt.split("/", 1)[0].strip() or txt
    return txt


def _guess_behavior_type(text: str) -> str:
    t = (text or "").lower()
    if any(k in t for k in ("error", "fault", "invalid", "fail", "diagnostic", "nrc")):
        return "error"
    if any(k in t for k in ("within", "timeout", "latency", "time", "kpi", "performance")):
        return "timing"
    if any(k in t for k in ("recover", "restore", "reconnect", "again", "after")):
        return "recovery"
    if any(k in t for k in ("user", "ack", "button", "select", "tap", "widget")):
        return "interaction"
    return "normal"


@codebeamer_router.get("/codebeamer/assessment/test-coverage", tags=["codebeamer"])
async def codebeamer_assessment_test_coverage(
    project_id: int,
    include_security: bool = True,
) -> Dict[str, Any]:
    """Return ASPICE-oriented assessment report from pulled Codebeamer corpus."""
    saved = _load_saved_pswd()
    cb_host = (saved or {}).get("codebeamer_host", "").rstrip("/")
    data = _load_corpus(project_id)
    if data is None:
        return {"ok": False, "error": "Corpus not found — run Pull first"}

    item_map = _CB_ITEM_MAPS.get(project_id)
    if not item_map:
        item_map = _build_item_map(data)
        _CB_ITEM_MAPS[project_id] = item_map

    items = data.get("items", [])
    item_by_id: dict[int, dict] = _CB_ITEMS_IDX.get(project_id) or {it["id"]: it for it in items if it.get("id")}

    if project_id not in _CB_TRACKER_PROCESS_MAP:
        cached = _load_tracker_map_cache(project_id)
        if cached is not None:
            _CB_LOG.info("Loaded tracker map from disk cache for project %d (%d trackers)", project_id, len(cached))
            _CB_TRACKER_PROCESS_MAP[project_id] = cached
        else:
            # Classify in background — page renders immediately with heuristic fallback.
            # Result saved to disk; next load uses the cache.
            _CB_TRACKER_PROCESS_MAP[project_id] = {}  # mark as "in progress" so we don't spawn twice
            trackers_snapshot = data.get("trackers", [])

            def _bg_classify(pid: int, trackers: list) -> None:
                _CB_CLASSIFYING.add(pid)
                try:
                    _CB_LOG.info("Background: classifying trackers via claude CLI for project %d", pid)
                    mapping = _classify_trackers_claude(trackers)
                    _CB_TRACKER_PROCESS_MAP[pid] = mapping
                    _save_tracker_map_cache(pid, mapping)
                    _CB_LOG.info("Background: tracker classification done for project %d (%d entries)", pid, len(mapping))
                finally:
                    _CB_CLASSIFYING.discard(pid)

            threading.Thread(target=_bg_classify, args=(project_id, trackers_snapshot), daemon=True).start()
    claude_map = _CB_TRACKER_PROCESS_MAP.get(project_id, {})

    tracker_meta: dict[int, dict] = {}
    item_tracker: dict[int, int] = {}
    for t in data.get("trackers", []):
        tid = int(t.get("id") or 0)
        if not tid:
            continue
        t_type = (t.get("type") or {}).get("name", "")
        process_area = claude_map.get(tid) or _tracker_process_area(tid, t.get("name", ""))
        if t_type.lower() in ("testrun", "testset", "testconf"):
            process_area = "SYS.5"
        tracker_meta[tid] = {
            "name": t.get("name", ""),
            "type_name": t_type,
            "process_area": process_area,
            "item_refs": len(t.get("items") or []),
        }
        for ref in t.get("items") or []:
            rid = ref.get("id")
            if rid:
                item_tracker[int(rid)] = tid

    requirement_tracker_ids = {
        tid for tid, meta in tracker_meta.items()
        if (meta.get("type_name") or "").lower() == "requirement"
    }
    testcase_tracker_ids = {
        tid for tid, meta in tracker_meta.items()
        if (meta.get("type_name") or "").lower() == "testcase"
    }
    testset_tracker_ids = {
        tid for tid, meta in tracker_meta.items()
        if (meta.get("type_name") or "").lower() == "testset"
    }
    testrun_tracker_ids = {
        tid for tid, meta in tracker_meta.items()
        if (meta.get("type_name") or "").lower() == "testrun"
    }
    testconf_tracker_ids = {
        tid for tid, meta in tracker_meta.items()
        if (meta.get("type_name") or "").lower() == "testconf"
    }
    defect_tracker_ids = {
        tid for tid, meta in tracker_meta.items()
        if (meta.get("type_name") or "").lower() in {"bug", "issue"}
    }

    def _is_folder_item(raw: dict) -> bool:
        type_name = (raw.get("typeName") or "").lower()
        if type_name in ("requirement", "testcase"):
            for cat in (raw.get("categories") or []):
                if (isinstance(cat, dict)
                        #and cat.get("id") == 1
                        and cat.get("name") == "Folder"
                        and cat.get("type") == "ChoiceOptionReference"):
                    return True
                if (isinstance(cat, dict)
                        #and cat.get("id") == 1
                        and cat.get("name") == "Heading"
                        and cat.get("type") == "ChoiceOptionReference"):
                    return True
        return False

    folder_ids = {iid for iid, raw in item_by_id.items() if _is_folder_item(raw)}

    # Recompute item_refs in tracker_meta excluding folders
    if folder_ids:
        tracker_to_items: dict[int, set] = {}
        for iid, tid in item_tracker.items():
            tracker_to_items.setdefault(tid, set()).add(iid)
        for tid, meta in tracker_meta.items():
            meta["item_refs"] = len(tracker_to_items.get(tid, set()) - folder_ids)

    draft_requirement_ids = {
        iid for iid, tid in item_tracker.items()
        if tid in requirement_tracker_ids
        and _item_status_name(item_by_id.get(iid, {})).lower() == "draft"
    }
    requirement_ids = {iid for iid, tid in item_tracker.items() if tid in requirement_tracker_ids} - folder_ids - draft_requirement_ids
    draft_testcase_ids = {
        iid for iid, tid in item_tracker.items()
        if tid in testcase_tracker_ids
        and _item_status_name(item_by_id.get(iid, {})).lower() == "draft"
    }
    testcase_ids = {iid for iid, tid in item_tracker.items() if tid in testcase_tracker_ids} - folder_ids - draft_testcase_ids
    testset_ids = {iid for iid, tid in item_tracker.items() if tid in testset_tracker_ids} - folder_ids
    testrun_ids = {iid for iid, tid in item_tracker.items() if tid in testrun_tracker_ids} - folder_ids
    testconf_ids = {iid for iid, tid in item_tracker.items() if tid in testconf_tracker_ids} - folder_ids
    defect_ids = {iid for iid, tid in item_tracker.items() if tid in defect_tracker_ids} - folder_ids

    if not include_security:
        sec_tracker_ids = {
            tid for tid, meta in tracker_meta.items()
            if meta.get("process_area") == "SEC"
        }
        sec_testcase_ids = {iid for iid, tid in item_tracker.items() if tid in sec_tracker_ids}
        testcase_ids -= sec_testcase_ids

    req_with_tests: set[int] = set()
    req_with_runs: set[int] = set()
    tests_with_runs: set[int] = set()
    gaps: list[dict] = []
    behavior_summary: defaultdict[str, int] = defaultdict(int)
    behavior_feature_summary: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))

    process_req_total: defaultdict[str, int] = defaultdict(int)
    process_req_with_tests: defaultdict[str, int] = defaultdict(int)
    process_req_with_runs: defaultdict[str, int] = defaultdict(int)
    process_tests_total: defaultdict[str, int] = defaultdict(int)
    process_runs_sets: defaultdict[str, set] = defaultdict(set)
    process_runs_passed_sets: defaultdict[str, set] = defaultdict(set)

    runs_passed_set: set[int] = set()
    runs_failed_set: set[int] = set()
    failed_runs_reported: set[int] = set()

    # Precompute testcase process area by tracker.
    testcase_process: dict[int, str] = {}
    for tcid in testcase_ids:
        tid = item_tracker.get(tcid)
        meta = tracker_meta.get(int(tid or 0), {})
        area = meta.get("process_area", "UNMAPPED")
        testcase_process[tcid] = area
        process_tests_total[area] += 1

    # Build testcase → runs mapping via TestSet custom field "Test Cases".
    # Codebeamer stores TC membership in TestSet via a TableFieldValue custom field:
    #   customField.type == "TableFieldValue"
    #   customField.values → [[cell, ...], [cell, ...]]  (rows)
    #   cell.values → [{id: TC_ID, type: TrackerItemReference}]
    # Chain: TestRun.subjects → TestSet.customField["Test Cases"] → TestCase
    testcase_to_runs: dict[int, set[int]] = defaultdict(set)
    for trid in testrun_ids:
        tr_up = item_map.get(trid, {}).get("upstream") or []
        for tsid in tr_up:
            if tsid not in testset_ids:
                continue
            ts_raw = item_by_id.get(tsid) or {}
            for cf in ts_raw.get("customFields") or []:
                if cf.get("type") != "TableFieldValue":
                    continue
                for row in (cf.get("values") or []):
                    for cell in (row if isinstance(row, list) else []):
                        for ref in (cell.get("values") or [] if isinstance(cell, dict) else []):
                            if isinstance(ref, dict) and ref.get("type") == "TrackerItemReference":
                                tc_id = ref.get("id")
                                if tc_id and tc_id in testcase_ids:
                                    testcase_to_runs[tc_id].add(trid)

    def _iname(iid: int) -> str:
        return str(item_by_id.get(iid, {}).get("name") or iid)

    # Testcase-level checks and run metrics.
    for tcid in testcase_ids:
        tc_entry = item_map.get(tcid, {})
        tc_up = set(tc_entry.get("upstream") or [])
        tc_down = set(tc_entry.get("downstream") or [])
        linked_requirements = (tc_up | tc_down) & requirement_ids
        linked_runs = testcase_to_runs.get(tcid, set())
        area = testcase_process.get(tcid, "UNMAPPED")

        if not linked_requirements:
            gaps.append({
                "gap_code": "TESTCASE_NO_REQUIREMENT",
                "severity": "warning",
                "process_area": area,
                "target_id": str(tcid),
                "target_name": _iname(tcid),
                "message": "Test case has no linked requirement.",
            })

        if linked_runs:
            tests_with_runs.add(tcid)
        else:
            gaps.append({
                "gap_code": "TESTCASE_NO_RUN",
                "severity": "warning",
                "process_area": area,
                "target_id": str(tcid),
                "target_name": _iname(tcid),
                "message": "Test case has no execution evidence.",
            })

        for rid in linked_runs:
            process_runs_sets[area].add(rid)
            raw_run = item_by_id.get(rid, {})
            outcome = _run_outcome(raw_run)
            if outcome == "passed":
                runs_passed_set.add(rid)
                process_runs_passed_sets[area].add(rid)
            elif outcome == "failed":
                runs_failed_set.add(rid)
                if rid not in failed_runs_reported:
                    failed_runs_reported.add(rid)
                    run_up = set(item_map.get(rid, {}).get("upstream") or [])
                    run_down = set(item_map.get(rid, {}).get("downstream") or [])
                    linked_defect = bool((run_up | run_down) & defect_ids)
                    if not linked_defect:
                        gaps.append({
                            "gap_code": "FAILED_NO_DEFECT",
                            "severity": "warning",
                            "process_area": area,
                            "target_id": str(rid),
                            "target_name": _iname(rid),
                            "message": "Failed test run has no linked defect/change item.",
                        })

    # Requirement-level checks.
    entities: list[dict] = []
    for req_id in requirement_ids:
        req_entry = item_map.get(req_id, {})
        req_down = set(req_entry.get("downstream") or [])
        linked_tests = req_down & testcase_ids
        linked_runs: set[int] = set()
        areas: set[str] = set()
        req_raw = item_by_id.get(req_id, {})
        req_name = str(req_raw.get("name") or req_id)
        req_tracker_id = item_tracker.get(req_id)
        req_tracker_name = tracker_meta.get(int(req_tracker_id or 0), {}).get("name", "")
        feature_key = _normalize_feature_key(req_tracker_name)
        behavior_type = _guess_behavior_type(req_name)
        behavior_summary[behavior_type] += 1
        behavior_feature_summary[feature_key][behavior_type] += 1

        for tcid in linked_tests:
            linked_runs |= testcase_to_runs.get(tcid, set())
            areas.add(testcase_process.get(tcid, "UNMAPPED"))

        if not areas:
            areas = {"UNMAPPED"}

        for area in areas:
            process_req_total[area] += 1
        if linked_tests:
            req_with_tests.add(req_id)
            for area in areas:
                process_req_with_tests[area] += 1
        else:
            gaps.append({
                "gap_code": "REQ_NO_TESTCASE",
                "severity": "error",
                "process_area": next(iter(areas)),
                "target_id": str(req_id),
                "target_name": req_name,
                "message": "Requirement has no linked test case.",
            })

        if linked_runs:
            req_with_runs.add(req_id)
            for area in areas:
                process_req_with_runs[area] += 1
        elif linked_tests:
            gaps.append({
                "gap_code": "REQ_NO_RUN_EVIDENCE",
                "severity": "warning",
                "process_area": next(iter(areas)),
                "target_id": str(req_id),
                "target_name": req_name,
                "message": "Requirement has tests, but no test run evidence.",
            })

        if len(entities) < 250:
            req_gaps = [g for g in gaps if g.get("target_id") == str(req_id)]
            entities.append({
                "entity_id": str(req_id),
                "entity_type": "requirement",
                "name": req_name,
                "feature": feature_key,
                "behavior_type": behavior_type,
                "metrics": {
                    "coverage": 1.0 if linked_tests else 0.0,
                    "execution": 1.0 if linked_runs else 0.0,
                },
                "gaps": req_gaps,
            })

    runs_passed = len(runs_passed_set)
    runs_failed = len(runs_failed_set)
    process_runs_total: dict[str, int] = {area: len(s) for area, s in process_runs_sets.items()}
    process_runs_passed: dict[str, int] = {area: len(s) for area, s in process_runs_passed_sets.items()}

    requirements_total = len(requirement_ids)
    tests_total = len(testcase_ids)
    runs_total = len(testrun_ids)

    # Diagnostic: log requirement coverage per tracker to help diagnose UNMAPPED
    for tid, meta in tracker_meta.items():
        if tid not in requirement_tracker_ids:
            continue
        t_total = sum(1 for iid, t2 in item_tracker.items() if t2 == tid and iid in requirement_ids)
        t_with_tests = sum(1 for iid, t2 in item_tracker.items() if t2 == tid and iid in req_with_tests)
        _CB_LOG.info(
            "req-tracker %d %-50s  total=%-6d covered=%-6d unmapped=%d",
            tid, meta["name"][:50], t_total, t_with_tests, t_total - t_with_tests,
        )

    coverage_ratio = (len(req_with_tests) / requirements_total) if requirements_total else 0.0
    execution_ratio = (len(req_with_runs) / requirements_total) if requirements_total else 0.0
    pass_rate = (runs_passed / runs_total) if runs_total else 0.0
    assessment_score = 0.45 * coverage_ratio + 0.30 * execution_ratio + 0.25 * pass_rate

    all_tracker_areas = {
        meta["process_area"]
        for meta in tracker_meta.values()
        if meta.get("process_area") and meta["process_area"] != "UNMAPPED"
    }
    process_areas = sorted(set(
        list(process_req_total.keys()) +
        list(process_tests_total.keys()) +
        list(process_runs_total.keys()) +
        list(all_tracker_areas)
    ))
    process_breakdown = []
    for area in process_areas:
        p_runs_total = process_runs_total.get(area, 0)
        p_runs_passed = process_runs_passed.get(area, 0)
        process_breakdown.append({
            "process_area": area,
            "requirements_total": process_req_total[area],
            "requirements_with_tests": process_req_with_tests[area],
            "requirements_with_runs": process_req_with_runs[area],
            "tests_total": process_tests_total[area],
            "runs_total": p_runs_total,
            "runs_passed": p_runs_passed,
            "pass_rate": (p_runs_passed / p_runs_total) if p_runs_total else 0.0,
        })

    # Build a compact graph for UI visualization.
    graph_nodes: dict[str, dict] = {}
    graph_edges: list[dict] = []
    def _item_label(iid: int, max_len: int = 48) -> str:
        name = (item_map.get(iid, {}).get("name") or "").strip()
        return (name[:max_len] + "…") if len(name) > max_len else (name or str(iid))

    max_root_reqs = 8
    for req_id in sorted(req_with_tests)[:max_root_reqs]:
        req_key = f"REQ:{req_id}"
        graph_nodes[req_key] = {"id": req_key, "label": _item_label(req_id), "item_id": req_id, "type": "requirement"}
        req_down = set(item_map.get(req_id, {}).get("downstream") or [])
        for tcid in sorted(req_down & testcase_ids)[:3]:
            tc_key = f"TC:{tcid}"
            graph_nodes[tc_key] = {"id": tc_key, "label": _item_label(tcid), "item_id": tcid, "type": "testcase"}
            graph_edges.append({"from": req_key, "to": tc_key, "type": "verified_by"})
            for rid in sorted(testcase_to_runs.get(tcid, set()))[:2]:
                run_key = f"TR:{rid}"
                graph_nodes[run_key] = {"id": run_key, "label": _item_label(rid), "item_id": rid, "type": "testrun"}
                graph_edges.append({"from": tc_key, "to": run_key, "type": "executed_as"})

    # Build Feature Behavior graph (Feature -> BehaviorType -> Requirement -> TestCase)
    behavior_nodes: dict[str, dict] = {}
    behavior_edges: list[dict] = []
    for ent in entities[:200]:
        if ent.get("entity_type") != "requirement":
            continue
        req_id = ent.get("entity_id")
        req_name = ent.get("name") or req_id
        feature_key = ent.get("feature") or "Uncategorized"
        behavior_type = ent.get("behavior_type") or "normal"

        feature_node = f"F:{feature_key}"
        behavior_node = f"B:{feature_key}:{behavior_type}"
        req_node = f"REQ:{req_id}"

        behavior_nodes[feature_node] = {"id": feature_node, "label": feature_key, "type": "feature"}
        behavior_nodes[behavior_node] = {
            "id": behavior_node,
            "label": f"{behavior_type} behavior",
            "type": "behavior",
        }
        behavior_nodes[req_node] = {"id": req_node, "label": str(req_name)[:52], "type": "requirement"}

        behavior_edges.append({"from": feature_node, "to": behavior_node, "type": "has_behavior"})
        behavior_edges.append({"from": behavior_node, "to": req_node, "type": "derived_as"})

        req_down = set(item_map.get(int(req_id), {}).get("downstream") or [])
        for tcid in sorted(req_down & testcase_ids)[:4]:
            tc_node = f"TC:{tcid}"
            behavior_nodes[tc_node] = {"id": tc_node, "label": str(tcid), "type": "testcase"}
            behavior_edges.append({"from": req_node, "to": tc_node, "type": "verified_by"})

    test_trackers = [
        {
            "tracker_id": tid,
            "tracker_name": meta["name"],
            "type_name": meta["type_name"],
            "process_area": meta["process_area"],
            "item_refs": meta["item_refs"],
        }
        for tid, meta in tracker_meta.items()
        if (meta.get("type_name") or "").lower() in {"testcase", "testrun", "testset", "testconf"}
    ]
    test_trackers.sort(key=lambda x: x["tracker_id"])

    # ── Full upstream chain graph: TestRun → TestCase → Req → ... → Stakeholder ──
    # Walks upstream through item_map subjects from test runs up to root artifacts.
    chain_nodes: dict[str, dict] = {}
    chain_edges: list[dict] = []
    chain_visited: set[int] = set()

    def _nkey(iid: int) -> str:
        return f"N{iid}"

    def _nlabel(iid: int) -> str:
        raw = item_by_id.get(iid, {})
        name = str(raw.get("name") or "").strip()
        return (name[:40] if name else str(iid)).replace('"', "'")

    def _walk_up(iid: int, child_key: str, depth: int) -> None:
        if depth > 5 or len(chain_nodes) >= 80 or iid in chain_visited:
            return
        chain_visited.add(iid)
        dtype = _item_display_type(iid, item_tracker, tracker_meta)
        nk = _nkey(iid)
        chain_nodes[nk] = {"id": nk, "label": _nlabel(iid), "type": dtype}
        chain_edges.append({"from": nk, "to": child_key})
        parents = sorted(set(item_map.get(iid, {}).get("upstream") or []))
        for pid in parents[:3]:
            _walk_up(pid, nk, depth + 1)

    # Invert testcase_to_runs for full chain traversal
    run_to_testcases: dict[int, set[int]] = defaultdict(set)
    for tcid, run_set in testcase_to_runs.items():
        for rid in run_set:
            run_to_testcases[rid].add(tcid)

    for rid in sorted(run_to_testcases.keys())[:12]:
        if len(chain_nodes) >= 80:
            break
        linked_tcs = run_to_testcases[rid]
        rk = _nkey(rid)
        chain_nodes[rk] = {"id": rk, "label": _nlabel(rid), "type": "testrun"}
        for tcid in sorted(linked_tcs)[:2]:
            if len(chain_nodes) >= 80:
                break
            tck = _nkey(tcid)
            chain_nodes[tck] = {"id": tck, "label": _nlabel(tcid), "type": "testcase"}
            chain_edges.append({"from": tck, "to": rk})
            tc_up = sorted(set(item_map.get(tcid, {}).get("upstream") or []))
            for req_id in tc_up[:3]:
                _walk_up(req_id, tck, 0)

    return {
        "ok": True,
        "project_id": project_id,
        "project_name": data.get("project_name", ""),
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "summary": {
            "requirements_total": requirements_total,
            "requirements_with_tests": len(req_with_tests),
            "requirements_with_runs": len(req_with_runs),
            "tests_total": tests_total,
            "tests_with_runs": len(tests_with_runs),
            "testsets_total": len(testset_ids),
            "testconf_total": len(testconf_ids),
            "runs_total": runs_total,
            "runs_passed": runs_passed,
            "runs_failed": runs_failed,
            "defects_total": len(defect_ids),
            "coverage_ratio": round(coverage_ratio, 4),
            "execution_ratio": round(execution_ratio, 4),
            "pass_rate": round(pass_rate, 4),
            "assessment_score": round(assessment_score, 4),
        },
        "process_breakdown": process_breakdown,
        "test_trackers": test_trackers,
        "req_tracker_breakdown": [
            {
                "tracker_id": tid,
                "tracker_name": meta["name"],
                "total": meta["item_refs"],
                "with_tests": sum(
                    1 for iid, t2 in item_tracker.items()
                    if t2 == tid and iid in req_with_tests
                ),
            }
            for tid, meta in tracker_meta.items()
            if tid in requirement_tracker_ids
        ],
        "entities": entities,
        "gaps": gaps,
        "behavior_summary": dict(behavior_summary),
        "behavior_feature_summary": {
            fk: dict(v) for fk, v in behavior_feature_summary.items()
        },
        "cb_host": cb_host,
        "graph": {
            "nodes": list(graph_nodes.values()),
            "edges": graph_edges,
        },
        "behavior_graph": {
            "nodes": list(behavior_nodes.values()),
            "edges": behavior_edges,
        },
        "full_chain_graph": {
            "nodes": list(chain_nodes.values()),
            "edges": chain_edges,
        },
    }


@codebeamer_router.get("/codebeamer/corpus/preload", tags=["codebeamer"])
async def codebeamer_corpus_preload(project_id: int) -> Dict[str, Any]:
    """Start loading corpus into memory in a background thread if not already loaded or loading."""
    if project_id in _CB_CORPUS:
        return {"status": "already_loaded"}
    stage = (_CB_LOAD_STATE.get(project_id) or {}).get("stage")
    if stage in ("reading", "parsing", "indexing"):
        return {"status": "loading"}
    def _bg():
        _load_corpus(project_id)
    threading.Thread(target=_bg, daemon=True, name=f"corpus-load-{project_id}").start()
    return {"status": "started"}


@codebeamer_router.get("/codebeamer/corpus/load-status", tags=["codebeamer"])
async def codebeamer_corpus_load_status(project_id: int) -> Dict[str, Any]:
    """Return current corpus loading state for a project."""
    if project_id in _CB_CORPUS:
        items = len(_CB_ITEMS_IDX.get(project_id) or {})
        path = _corpus_json_path(project_id)
        file_size_mb = round(path.stat().st_size / 1024 / 1024, 1) if path.exists() else 0
        return {"stage": "ready", "file_size_mb": file_size_mb, "items": items}
    state = _CB_LOAD_STATE.get(project_id)
    if state:
        return state
    path = _corpus_json_path(project_id)
    if path.exists():
        return {"stage": "idle", "file_size_mb": round(path.stat().st_size / 1024 / 1024, 1), "items": 0}
    return {"stage": "missing", "file_size_mb": 0, "items": 0}


@codebeamer_router.get("/codebeamer/assessment/classifier-status", tags=["codebeamer"])
async def codebeamer_classifier_status(project_id: int) -> Dict[str, Any]:
    """Return current tracker classification status for a project."""
    if project_id in _CB_CLASSIFYING:
        return {"status": "running"}
    if _CB_TRACKER_PROCESS_MAP.get(project_id):
        return {"status": "done"}
    if _tracker_map_cache_path(project_id).exists():
        return {"status": "done"}
    return {"status": "pending"}


@codebeamer_router.get("/codebeamer/assessment/behavior-scenario", tags=["codebeamer"])
async def codebeamer_behavior_scenario(project_id: int, item_id: int) -> Dict[str, Any]:
    """Extract structured behavior scenario from a requirement item using Claude CLI."""
    idx = _CB_ITEMS_IDX.get(project_id)
    if not idx:
        data = _load_corpus(project_id)
        if data is None:
            return {"ok": False, "error": "Corpus not found"}
    item = (_CB_ITEMS_IDX.get(project_id) or {}).get(item_id)
    if not item:
        return {"ok": False, "error": f"Item {item_id} not found in corpus"}
    scenario = _extract_behavior_scenario_claude(item)
    return {"ok": True, "item_id": item_id, "scenario": scenario}


@codebeamer_router.get("/codebeamer/assessment/ai-analysis", tags=["codebeamer"])
async def codebeamer_assessment_ai_analysis(
    project_id: int,
    include_security: bool = True,
) -> Dict[str, Any]:
    """Run Claude gap analysis on assessment data and return plain-text result."""
    result = await codebeamer_assessment_test_coverage(
        project_id=project_id,
        include_security=include_security,
    )
    if not result.get("ok"):
        return {"ok": False, "error": result.get("error", "Assessment unavailable")}

    # Collect sample requirements that lack test cases for behavioral analysis
    sample_items: list[dict] = []
    corpus = _CB_CORPUS.get(project_id)
    if corpus:
        idx = _CB_ITEMS_IDX.get(project_id) or {}
        gap_req_ids = [
            int(g["target_id"]) for g in result["gaps"]
            if g["gap_code"] == "REQ_NO_TESTCASE"
        ][:10]
        for rid in gap_req_ids:
            raw = idx.get(rid, {})
            if raw:
                sample_items.append({
                    "id": rid,
                    "name": raw.get("name", ""),
                    "description": (raw.get("description") or "")[:500],
                })

    analysis = _analyze_gaps_claude(
        summary=result["summary"],
        process_breakdown=result["process_breakdown"],
        gaps=result["gaps"],
        sample_items=sample_items or None,
    )
    return {"ok": True, "analysis": analysis}


@codebeamer_router.get("/codebeamer/items-by-type", tags=["codebeamer"])
async def codebeamer_items_by_type(
    project_id: int,
    type_name: str,
) -> Dict[str, Any]:
    """Return items whose typeName matches type_name (case-insensitive), read from corpus JSON."""
    data = _load_corpus(project_id)
    if data is None:
        return {"ok": False, "error": "Corpus not found — run Pull first", "items": []}

    type_name_lower = type_name.lower()
    result = [
        {"id": item["id"], "name": item.get("name", "")}
        for item in data.get("items", [])
        if item.get("id") and (item.get("typeName") or "").lower() == type_name_lower
    ]
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

    def _flatten_tree(nodes: list[dict]) -> list[dict]:
        result = []
        for node in nodes:
            if node.get("isFolder"):
                result.extend(_flatten_tree(node.get("children") or []))
            elif node.get("trackerId"):
                result.append({
                    "id":    node["trackerId"],
                    "name":  str(node.get("text") or node["trackerId"]),
                    "items": node.get("items", []),
                })
        return result

    trackers_info = _flatten_tree(tree.get("tree", []))
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
    _CB_ITEMS_IDX[project_id] = {it["id"]: it for it in data.get("items", []) if it.get("id")}
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
    _update_cb_status(req.project_id, "pull")
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

