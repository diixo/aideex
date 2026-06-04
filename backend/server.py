"""FastAPI application exposing async ASPICE assessment jobs."""
from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Tuple, Set

from fastapi import APIRouter, Body, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from datetime import datetime

from .codebeamer_router import codebeamer_router

from .config import settings
from .job_store import (
    delete_job,
    get_job_result,
    get_job_status,
    initialize_job,
    list_job_ids,
    list_jobs,
    register_listener,
    unregister_listener,
    clear_state as jobstore_clear_state,
)
from .scheduler import scheduler
from .orchestration import enqueue_process_passes, enqueue_project_pass
from rag_inference import AssessmentEngine, ASPICE_JSON
from .combined_schema import build_process_assessment_schema, build_project_assessment_schema
from retrieval import RetrievalEngine

# Ensure 'scripts/' is importable for Stage 1 CLI wrappers
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_DIR = _ROOT / "scripts"

import sys
sys.path.insert(0, str(_SCRIPTS_DIR))

from .confluence_sync import (
    evaluate_remote as cf_evaluate_remote,
    pull as cf_pull,
    build as cf_build,
    activate as cf_activate,
    ConfluenceClient as CfClient,
)  # type: ignore

import threading
import uuid as _uuid
import requests

APP_ROOT = Path(__file__).resolve().parent.parent
CORPUS_ROOT = APP_ROOT / "corpus"

_CONF_LOG = logging.getLogger("backend.confluence")

router = APIRouter()
ARTIFACT_ROOT = Path("assessment_runs")

_ENGINE: Optional[AssessmentEngine] = None

_AGGREGATE_LOGGER = logging.getLogger("backend.aggregate")
_AGG_STRATEGY_RAW = (os.environ.get("AGGREGATE_LOG_STRATEGY") or "info").strip().lower()
_AGG_STRATEGY = "info"
_AGG_RATE_SECONDS: float | None = None

if _AGG_STRATEGY_RAW.startswith("rate_limit:"):
    _AGG_STRATEGY = "rate_limit"
    try:
        _AGG_RATE_SECONDS = max(float(_AGG_STRATEGY_RAW.split(":", 1)[1]), 0.0)
    except Exception:
        _AGG_RATE_SECONDS = 30.0
elif _AGG_STRATEGY_RAW in {"info", "debug", "terminal"}:
    _AGG_STRATEGY = _AGG_STRATEGY_RAW
else:
    _AGG_STRATEGY = "info"

_AGG_LAST_STATS: Dict[str, str] = {}
_AGG_LAST_LOG_TIME: Dict[str, float] = {}
_AGG_LOCK = threading.Lock()
_BULK_LOG = logging.getLogger("backend.bulk_result")
_ARTIFACT_LOG = logging.getLogger("backend.artifacts")


def _aggregate_terminal(stats: Dict[str, Any]) -> bool:
    overall = stats.get("overall") if isinstance(stats, dict) else None
    if not isinstance(overall, dict):
        return False
    rating = overall.get("rating")
    process_count = overall.get("process_count") or 0
    bp_total = overall.get("bp_total") or 0
    return bool(rating) or (process_count > 0 and bp_total > 0)


def _emit_aggregate_log(group_id: str, stats: Dict[str, Any]) -> None:
    message = "aggregate group_id=%s stats=%s"
    if _AGG_STRATEGY == "debug":
        _AGGREGATE_LOGGER.debug(message, group_id, stats)
        return

    if _AGG_STRATEGY == "terminal":
        if _aggregate_terminal(stats):
            _AGGREGATE_LOGGER.info(message, group_id, stats)
        return

    stats_key = json.dumps(stats, sort_keys=True, default=str)
    now = time.monotonic()
    with _AGG_LOCK:
        if _AGG_LAST_STATS.get(group_id) == stats_key:
            return
        if _AGG_STRATEGY == "rate_limit":
            interval = _AGG_RATE_SECONDS or 0.0
            if interval > 0:
                last = _AGG_LAST_LOG_TIME.get(group_id)
                if last is not None and (now - last) < interval:
                    return
                _AGG_LAST_LOG_TIME[group_id] = now
        _AGG_LAST_STATS[group_id] = stats_key

    _AGGREGATE_LOGGER.info(message, group_id, stats)


def _get_engine() -> AssessmentEngine:
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = AssessmentEngine()
    return _ENGINE


class HealthResponse(BaseModel):
    status: Literal["ok"]


class CreateJobRequest(BaseModel):
    bp_id: str = Field(..., description="ASPICE base practice identifier, e.g., MAN.3.BP1 or SWE.1.BP3")
    top_k: int = Field(5, ge=1, le=20, description="Number of evidence chunks to retrieve")
    backend: Literal["xgrammar", "guidance"] = Field(
        "xgrammar", description="Structured decoding backend"
    )
    notes: Optional[str] = Field(
        None,
        description="Optional free-form notes captured alongside the job metadata",
    )
    manual_keywords: Optional[List[str]] = Field(
        default=None,
        description="Additional keyword hints used to refine retrieval queries.",
    )
    fusion_weights: Optional[Dict[str, float]] = Field(
        default=None,
        description="Optional weights for reference indexes (e.g., aspice_normative, aspice_guidelines).",
    )
    include_related_process_evidence: Optional[bool] = Field(
        default=None,
        description="If true, include project evidence for processes referenced by selected rules (e.g., SUP.9)",
    )
    related_process_k: Optional[int] = Field(
        default=None, ge=0, le=10,
        description="Max number of project evidence chunks per related process",
    )
    reasoning_enabled: Optional[bool] = Field(
        default=None,
        description="Enable model-native reasoning if supported by the selected model/profile.",
    )


class CreateJobResponse(BaseModel):
    job_id: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    status: str
    message: Optional[str]
    progress: Optional[float]
    backend: Optional[str]
    bp_id: Optional[str]
    top_k: Optional[int]
    submitted_at: Optional[str]
    updated_at: Optional[str]
    duration_ms: Optional[int]
    prompt_chars: Optional[int]
    prompt_tokens: Optional[int] = None
    error: Optional[str]
    notes: Optional[str]
    manual_keywords: Optional[List[str]]
    fusion_weights: Optional[Dict[str, float]]
    rerun_of: Optional[str] = None
    model_profile: Optional[str] = None
    model_id: Optional[str] = None
    quantization: Optional[str] = None
    tensor_parallel_size: Optional[int] = None
    gpu_memory_utilization: Optional[float] = None
    kv_cache_memory_bytes: Optional[int] = None
    group_id: Optional[str] = None
    parent_report_id: Optional[str] = None
    process: Optional[str] = None


class JobResultResponse(BaseModel):
    job_id: str
    status: Literal["completed"]
    result: Dict[str, Any]


class JobListResponse(BaseModel):
    jobs: List[JobStatusResponse]


class DeleteJobsRequest(BaseModel):
    job_ids: Optional[List[str]] = None
    delete_all: bool = False
    purge_artifacts: Optional[bool] = None


class DeleteJobsResponse(BaseModel):
    deleted_ids: List[str] = Field(default_factory=list)
    skipped_ids: Optional[List[str]] = None


class BasePractice(BaseModel):
    id: str
    title: Optional[str] = None
    description: Optional[str] = None
    notes: Optional[List[str]] = None
    process_id: Optional[str] = None


class EvidenceItem(BaseModel):
    score: float
    text: str
    metadata: Dict[str, Any]


class EvaluateDocumentsRequest(BaseModel):
    bp_ids: List[str] = Field(..., description="Selected base practice identifiers")
    top_k: int = Field(5, ge=1, le=20)
    manual_keywords: Optional[List[str]] = None
    stop_words: Optional[List[str]] = Field(
        default=None,
        description="Stop words/phrases applied to page titles during retrieval (case-insensitive).",
    )
    fusion_weights: Optional[Dict[str, float]] = Field(
        default=None,
        description="Optional weights for reference indexes (aspice_normative, aspice_guidelines, etc.)",
    )
    include_related_process_evidence: Optional[bool] = None
    related_process_k: Optional[int] = None


class EvaluateDocumentsResponse(BaseModel):
    bps: List[Dict[str, Any]]


class ProcessEntry(BaseModel):
    id: str
    name: Optional[str] = None
    bp_ids: List[str]


class ProcessesResponse(BaseModel):
    processes: List[ProcessEntry]


class BulkJobsRequest(BaseModel):
    bp_ids: List[str]
    backend: Literal["xgrammar", "guidance"] = "xgrammar"
    top_k: int = 7
    notes: Optional[str] = None
    manual_keywords: Optional[List[str]] = None
    stop_words: Optional[List[str]] = Field(
        default=None,
        description="Stop words/phrases applied to page titles during retrieval (case-insensitive).",
    )
    fusion_weights: Optional[Dict[str, float]] = Field(
        default=None,
        description="Optional weights for reference indexes (aspice_normative, aspice_guidelines, etc.)",
    )
    include_related_process_evidence: Optional[bool] = None
    related_process_k: Optional[int] = None
    reasoning_enabled: Optional[bool] = None
    parent_report_id: Optional[str] = Field(
        default=None,
        description="Optional parent report id to associate all created jobs with. If omitted, a new id is generated.",
    )


class BulkJobsResponse(BaseModel):
    group_id: str
    job_ids: List[str]
    parent_report_id: Optional[str] = None


class ProcessAssessRequest(BaseModel):
    reasoning_enabled: Optional[bool] = None


class ProjectAssessRequest(BaseModel):
    reasoning_enabled: Optional[bool] = None


class BulkGroupStatusResponse(BaseModel):
    group_id: str
    jobs: List[JobStatusResponse]
    completed: int
    total: int


class BulkProcessAggregate(BaseModel):
    process_id: str
    bp_ids: List[str]
    capability_rating: Optional[str] = None
    rationale: Optional[str] = None
    gaps: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    guideline_rules: Optional[List[Dict[str, Any]]] = None
    governance_checks: Optional[List[Dict[str, Any]]] = None
    contextual_warnings: Optional[List[Dict[str, Any]]] = None


class BulkProjectSummary(BaseModel):
    capability_rating: Optional[str] = None
    rationale: Optional[str] = None
    gaps: List[str] = Field(default_factory=list)
    recommendations: List[str] = Field(default_factory=list)
    included_processes: List[str] = Field(default_factory=list)
    process_summaries: Optional[List[Dict[str, Any]]] = None
    confidence: Optional[float] = None


class BulkGroupResultResponse(BaseModel):
    group_id: str
    processes: List[BulkProcessAggregate]
    overall_capability_rating: Optional[str] = None
    overall_rationale: Optional[str] = None
    stats: Optional[Dict[str, Any]] = None
    project_summary: Optional[BulkProjectSummary] = None
    combined_report: Optional[Dict[str, Any]] = None
    # Optional: exact performance JSON per README (served separately too)
    performance: Optional[Dict[str, Any]] = None


class RerunJobRequest(BaseModel):
    backend: Optional[Literal["xgrammar", "guidance"]] = Field(
        default=None,
        description="Override structured backend for the rerun.",
    )
    notes: Optional[str] = Field(
        default=None,
        description="Optional notes for the rerun (defaults to original job notes if omitted).",
    )
    top_k: Optional[int] = Field(
        default=None,
        ge=1,
        le=20,
        description="Override the evidence retrieval depth.",
    )
    manual_keywords: Optional[List[str]] = Field(
        default=None,
        description="Provide a new set of manual keywords (empty list clears them).",
    )
    fusion_weights: Optional[Dict[str, float]] = Field(
        default=None,
        description="Override fusion weights; omit to reuse the original."  # noqa: E501
    )
    include_related_process_evidence: Optional[bool] = Field(
        default=None,
        description="If true, include project evidence for related processes",
    )
    related_process_k: Optional[int] = Field(
        default=None, ge=0, le=10,
        description="Max number of project evidence chunks per related process",
    )
    reasoning_enabled: Optional[bool] = Field(
        default=None,
        description="Override reasoning enablement for rerun (omit to reuse original).",
    )
    group_id: Optional[str] = Field(
        default=None,
        description="Associate the rerun with this group (for cascades/report scoping).",
    )
    parent_report_id: Optional[str] = Field(
        default=None,
        description="Associate the rerun with this parent report (scoped Assessment Jobs).",
    )


def _sanitize_keywords(keywords: Optional[List[str]]) -> Optional[List[str]]:
    if keywords is None:
        return None
    sanitized = [kw.strip() for kw in keywords if kw and kw.strip()]
    return sanitized or []


def _sanitize_fusion_weights(weights: Optional[Dict[str, float]]) -> Optional[Dict[str, float]]:
    if weights is None:
        return None
    sanitized: Dict[str, float] = {}
    for key, value in weights.items():
        if not isinstance(value, (int, float)):
            continue
        sanitized[key] = float(value)
    return sanitized


def _load_job_metadata(job_id: str) -> Dict[str, Any]:
    metadata_path = ARTIFACT_ROOT / job_id / "metadata.json"
    if not metadata_path.exists():
        raise HTTPException(status_code=404, detail="Job artifacts not found for rerun")
    try:
        with metadata_path.open("r", encoding="utf-8") as handle:
            metadata = json.load(handle)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=500, detail=f"Failed to parse metadata.json: {exc}") from exc
    return metadata


async def _enqueue_job(payload: Dict[str, Any]) -> CreateJobResponse:
    job_id = payload["job_id"]
    initialize_job(job_id, payload)
    await scheduler.enqueue(payload)
    status = get_job_status(job_id)
    return CreateJobResponse(job_id=job_id, status=status["status"] if status else "queued")


@router.get("/health", response_model=HealthResponse, tags=["system"])
def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok")


@router.get("/queues", tags=["system"])
def queue_info() -> JSONResponse:
    return JSONResponse({
        "name": settings.queue_name,
        "size": scheduler.pending_items(),
    })


@router.get("/capabilities", tags=["system"])
def capabilities() -> JSONResponse:
    model_id = settings.model_id
    # Reasoning parser support (engine-level parsing only)
    lower = (model_id or "").lower()
    reasoning_parser = None
    if "qwen3" in lower and "thinking" in lower:
        reasoning_parser = "qwen3"
    supports_reasoning = bool(reasoning_parser)
    return JSONResponse({
        "model_id": model_id,
        "supports_reasoning": supports_reasoning,
        "reasoning_parser": reasoning_parser,
        "model_profile": getattr(settings, "model_profile", None),
    })


@router.get("/base_practices", response_model=List[BasePractice], tags=["aspice"])
def base_practices() -> List[BasePractice]:
    # Lightweight parse to avoid initializing retrieval/model
    try:
        data = json.loads(Path(ASPICE_JSON).read_text(encoding="utf-8"))
        root = data[0] if data else {}
        bps = root.get("base_practices", [])
        items: List[BasePractice] = []
        for bp in bps:
            items.append(
                BasePractice(
                    id=bp.get("id"),
                    title=bp.get("title"),
                    description=bp.get("description"),
                    notes=bp.get("notes", []),
                )
            )
        return items
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load base practices: {exc}")


@router.get("/base_practices/{bp_id}", response_model=BasePractice, tags=["aspice"])
def base_practice(bp_id: str) -> BasePractice:
    engine = _get_engine()
    try:
        bp = engine.get_base_practice(bp_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Base practice not found")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to load base practice: {exc}")
    return BasePractice(
        id=bp.get("id"),
        title=bp.get("title"),
        description=bp.get("description"),
        notes=bp.get("notes", []),
        process_id=bp.get("process_id"),
    )



@router.get("/processes", response_model=ProcessesResponse, tags=["aspice"])
def processes() -> ProcessesResponse:
    engine = _get_engine()
    mapping = engine.list_processes()
    entries: List[ProcessEntry] = []
    for proc_id in sorted(mapping.keys()):
        meta = mapping[proc_id]
        entries.append(
            ProcessEntry(
                id=proc_id,
                name=meta.get("process_name"),
                bp_ids=meta.get("bp_ids", []),
            )
        )
    return ProcessesResponse(processes=entries)


@router.post("/bulk_jobs", response_model=BulkJobsResponse, tags=["jobs"], status_code=201)
async def bulk_jobs(request: BulkJobsRequest) -> BulkJobsResponse:
    import uuid
    group_id = uuid.uuid4().hex
    parent_report_id = (request.parent_report_id or uuid.uuid4().hex)
    job_ids: List[str] = []
    engine = _get_engine()
    validated_bp_ids: List[str] = []
    for bp in request.bp_ids:
        try:
            engine.get_base_practice(bp)
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Unknown base practice id: {bp}")
        validated_bp_ids.append(bp)

    # Compute union of allowed page ids from saved selections (when present)
    allowed_page_ids: list[str] = []
    try:
        seen: set[str] = set()
        for sync_dir in (CORPUS_ROOT.glob("*/.sync")):
            sel_path = sync_dir / "selection.json"
            if not sel_path.exists():
                continue
            try:
                data = json.loads(sel_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            for pid in (data.get("include_ids") or []):
                spid = str(pid)
                if spid and spid not in seen:
                    seen.add(spid)
                    allowed_page_ids.append(spid)
    except Exception:
        allowed_page_ids = []

    for bp in validated_bp_ids:
        payload: Dict[str, Any] = {
            "job_id": uuid.uuid4().hex,
            "bp_id": bp,
            "top_k": int(request.top_k),
            "backend": request.backend,
            "notes": request.notes,
            "model_profile": getattr(settings, "model_profile", None),
            "model_id": settings.model_id,
            "quantization": getattr(settings, "quantization", None),
            "tensor_parallel_size": getattr(settings, "tensor_parallel_size", None),
            "gpu_memory_utilization": settings.gpu_memory_utilization,
            "kv_cache_memory_bytes": getattr(settings, "kv_cache_memory_bytes", None),
            "group_id": group_id,
            "parent_report_id": parent_report_id,
            "process": ".".join(bp.split(".")[:2]) if "." in bp else None,
        }
        if request.manual_keywords:
            payload["manual_keywords"] = [kw.strip() for kw in request.manual_keywords if kw and kw.strip()]
        if request.stop_words:
            payload["stop_words"] = [sw.strip() for sw in request.stop_words if sw and sw.strip()]
        if request.fusion_weights:
            payload["fusion_weights"] = {k: float(v) for k, v in request.fusion_weights.items() if isinstance(v, (int, float))}
        if request.include_related_process_evidence is not None:
            payload["include_related_process_evidence"] = bool(request.include_related_process_evidence)
        if request.related_process_k is not None:
            payload["related_process_k"] = int(request.related_process_k)
        if request.reasoning_enabled is not None:
            payload["reasoning_enabled"] = bool(request.reasoning_enabled)
        if allowed_page_ids:
            payload["allowed_page_ids"] = allowed_page_ids
        initialize_job(payload["job_id"], payload)
        await scheduler.enqueue(payload)
        job_ids.append(payload["job_id"])
    return BulkJobsResponse(group_id=group_id, job_ids=job_ids, parent_report_id=parent_report_id)


@router.get("/bulk_jobs/{group_id}", response_model=BulkGroupStatusResponse, tags=["jobs"])
def bulk_status(group_id: str, parent_report_id: Optional[str] = Query(None)) -> BulkGroupStatusResponse:
    jobs = list_jobs(limit=None)
    group_jobs_raw = [j for j in jobs if j.get('group_id') == group_id]
    if parent_report_id:
        group_jobs_raw = [j for j in group_jobs_raw if j.get('parent_report_id') == parent_report_id]
    group_jobs = [JobStatusResponse(**j) for j in group_jobs_raw]
    return BulkGroupStatusResponse(group_id=group_id, jobs=group_jobs, completed=sum(1 for j in group_jobs if (j.status or '').lower()== 'completed'), total=len(group_jobs))


def _aggregate_rating(ratings: List[str]) -> Optional[str]:
    order = {'N':0,'P':1,'L':2,'F':3}
    vals = [order.get(r) for r in ratings if r in order]
    if not vals:
        return None
    
    mean = sum(vals) / len(vals)
    
    if mean < 0.5:
        return 'N'
    elif mean < 1.5:
        return 'P'
    elif mean < 2.5:
        return 'L'
    else:
        return 'F' 


def _sanitize_rating_labels(text: Optional[str]) -> Optional[str]:
    """Replace incorrect spelled-out labels in free text (e.g., 'Limited' for L).

    Backend contract is letter-only for structured fields; this cleanup targets
    human-readable summaries that may include spelled-out labels from the model.
    """
    if not text or not isinstance(text, str):
        return text
    import re
    rep = {
        'N': 'Not Achieved',
        'P': 'Partially Achieved',
        'L': 'Largely Achieved',
        'F': 'Fully Achieved',
    }
    out = text
    # Normalize patterns like "Level L (Limited)" or "ASPICE Level L (Limited)"
    for k, label in rep.items():
        out = re.sub(rf"(ASPICE\s+)?Level\s*{k}\s*\([^)]*\)", f"Level {k} ({label})", out, flags=re.IGNORECASE)
    # Direct bare replacements of '(Limited)' when near 'L'
    out = re.sub(r"\((?i:limited)\)", "(Largely Achieved)", out)
    return out


@router.get("/bulk_jobs/{group_id}/result", response_model=BulkGroupResultResponse, tags=["jobs"])
def bulk_result(group_id: str, parent_report_id: Optional[str] = Query(None)) -> BulkGroupResultResponse:
    import json
    from pathlib import Path
    jobs = list_jobs(limit=None)
    group_jobs = [j for j in jobs if j.get('group_id') == group_id]
    if parent_report_id:
        group_jobs = [j for j in group_jobs if j.get('parent_report_id') == parent_report_id]
    proc_map: Dict[str, List[Dict[str, Any]]] = {}
    project_entries: List[Dict[str, Any]] = []
    top_project_entry: Optional[Dict[str, Any]] = None
    project_report_data: Optional[Dict[str, Any]] = None
    for j in group_jobs:
        job_id = j.get('job_id')
        # Prefer in-memory result
        result = get_job_result(job_id)
        kind = str((j.get("job_kind") or "bp")).lower()
        assessment_payload = None
        prompt_snapshot = None
        metadata = None
        if isinstance(result, dict) and 'assessment' in result:
            assessment_payload = result.get('assessment')
            metadata = result.get('metadata')
            prompt_snapshot = result.get('prompt_snapshot')
        elif isinstance(result, dict):
            assessment_payload = result
        if assessment_payload is None:
            # Load from disk based on job kind
            if kind == 'process':
                apath = Path('assessment_runs') / str(job_id) / 'process_assessment.json'
            elif kind == 'project':
                apath = Path('assessment_runs') / str(job_id) / 'project_assessment.json'
            else:
                apath = Path('assessment_runs') / str(job_id) / 'assessment.json'
            if apath.exists():
                try:
                    assessment_payload = json.loads(apath.read_text(encoding='utf-8'))
                except Exception:
                    assessment_payload = None
        # Ensure prompt_snapshot includes full evidence list when available on disk
        def _artifact_prompt_path(jid: str, k: str) -> Path:
            if k == 'process':
                return Path('assessment_runs') / str(jid) / 'process_prompt.json'
            if k == 'project':
                return Path('assessment_runs') / str(jid) / 'project_prompt.json'
            return Path('assessment_runs') / str(jid) / 'prompt.json'

        if prompt_snapshot is None:
            ppath = _artifact_prompt_path(job_id, kind)
            if ppath.exists():
                try:
                    prompt_snapshot = json.loads(ppath.read_text(encoding='utf-8'))
                    _BULK_LOG.debug("bulk_result: loaded artifact prompt job_id=%s kind=%s path=%s", job_id, kind, str(ppath))
                except Exception:
                    prompt_snapshot = None
        else:
            # Some in-memory snapshots omit `evidence` for brevity; upgrade from artifact if needed
            try:
                needs_upgrade = ('evidence' not in prompt_snapshot) and bool(prompt_snapshot.get('evidence_count'))
            except Exception:
                needs_upgrade = False
            if needs_upgrade:
                ppath = _artifact_prompt_path(job_id, kind)
                if ppath.exists():
                    try:
                        full_snapshot = json.loads(ppath.read_text(encoding='utf-8'))
                        if isinstance(full_snapshot, dict):
                            prompt_snapshot = full_snapshot
                            _BULK_LOG.info("bulk_result: upgraded prompt_snapshot with full evidence job_id=%s kind=%s", job_id, kind)
                    except Exception:
                        pass
        if assessment_payload is None:
            continue
        if kind == 'project':
            project_entries.append({
                'assessment': assessment_payload,
                'prompt_snapshot': prompt_snapshot,
                'metadata': metadata,
                'job': j,
            })
            continue
        proc_raw = j.get('process') or (str(j.get('bp_id', '')).rsplit('.', 1)[0])
        proc = str(proc_raw or '').strip().upper()
        if not proc or proc == 'NONE':
            continue
        proc_map.setdefault(proc, []).append({
            'assessment': assessment_payload,
            'prompt_snapshot': prompt_snapshot,
            'metadata': metadata,
            'job_kind': kind,
            'job': j,
        })
    project_summary: Optional[BulkProjectSummary] = None
    if project_entries:
        def _project_entry_key(entry: Dict[str, Any]) -> str:
            job = entry.get('job') or {}
            meta = entry.get('metadata') or {}
            return (
                job.get('updated_at')
                or job.get('submitted_at')
                or meta.get('completed_at')
                or meta.get('submitted_at')
                or ''
            )

        project_entries.sort(key=_project_entry_key, reverse=True)
        top_project_entry = project_entries[0]
        assess = top_project_entry.get('assessment') or {}
        # Sanitize human text fields for correct rating labels in summaries
        proc_summaries_raw = list(assess.get('process_summaries') or [])
        proc_summaries_sanitized = []
        for ps in proc_summaries_raw:
            try:
                proc_summaries_sanitized.append({
                    **ps,
                    'summary': _sanitize_rating_labels(ps.get('summary')),
                })
            except Exception:
                proc_summaries_sanitized.append(ps)
        project_summary = BulkProjectSummary(
            capability_rating=assess.get('capability_rating'),
            rationale=_sanitize_rating_labels(assess.get('rationale')),
            gaps=_clean_text_list(assess.get('gaps')),
            recommendations=_clean_text_list(assess.get('recommendations')),
            included_processes=list(assess.get('included_processes') or []),
            process_summaries=proc_summaries_sanitized or None,
            confidence=assess.get('confidence'),
        )
        project_report_data = {
            key: value for key, value in {
                'job_id': (top_project_entry.get('job') or {}).get('job_id'),
                # Return sanitized assessment in the result payload for UI
                'assessment': {
                    **assess,
                    'rationale': _sanitize_rating_labels(assess.get('rationale')),
                    'process_summaries': proc_summaries_sanitized or None,
                },
                'prompt_snapshot': top_project_entry.get('prompt_snapshot'),
                'metadata': top_project_entry.get('metadata'),
            }.items() if value is not None
        }

    processes: List[BulkProcessAggregate] = []
    all_ratings: List[str] = []
    agg_stats: Dict[str, Any] = {"processes": {}, "overall": {}}
    combined_process_reports: List[Dict[str, Any]] = []
    for proc, results in sorted(proc_map.items()):
        bp_ids: List[str] = []
        ratings: List[str] = []
        rules: Dict[str, Dict[str, Any]] = {}
        gov: Dict[str, Dict[str, Any]] = {}
        warns: Dict[str, Dict[str, Any]] = {}
        rationale_parts: List[str] = []
        process_rationale: Optional[str] = None
        process_gaps: Optional[List[str]] = None
        process_recs: Optional[List[str]] = None
        # size accounting
        assess_chars = 0
        rule_text_chars = 0
        gov_text_chars = 0
        warn_text_chars = 0
        bp_reports: List[Dict[str, Any]] = []
        process_report: Optional[Dict[str, Any]] = None
        for r in results:
            assess = r.get('assessment') or {}
            kind = (r.get('job_kind') or 'bp').lower()
            bid = assess.get('base_practice_id')
            if bid: bp_ids.append(bid)
            cr = assess.get('capability_rating')
            if cr: ratings.append(cr)
            rationale_text = assess.get('rationale')
            if rationale_text:
                if kind == 'process':
                    process_rationale = rationale_text
                    pg = _clean_text_list(assess.get('gaps'))
                    pr = _clean_text_list(assess.get('recommendations'))
                    process_gaps = pg or process_gaps
                    process_recs = pr or process_recs
                else:
                    label = bid or proc
                    rationale_parts.append(f"{label}: {rationale_text}")
            try:
                assess_chars += len(json.dumps(assess, ensure_ascii=False))
            except Exception:
                pass
            ps = r.get('prompt_snapshot') or {}
            for item in (ps.get('guideline_rules') or []):
                code = str(item.get('code') or '')
                rules[code or str(len(rules))] = item
                txt = str(item.get('text') or '')
                rule_text_chars += len(txt)
            for item in (ps.get('governance_checks') or []):
                code = str(item.get('code') or '')
                gov[code or str(len(gov))] = item
                txt = str(item.get('text') or '')
                gov_text_chars += len(txt)
            for item in (ps.get('contextual_warnings') or []):
                msg = str(item.get('message') or '')
                warns[msg or str(len(warns))] = item
                warn_text_chars += len(msg)
            job_info = r.get('job') or {}
            entry_payload = {
                key: value for key, value in {
                    'job_id': job_info.get('job_id'),
                    'job_kind': kind,
                    'bp_id': bid or job_info.get('bp_id'),
                    'assessment': assess,
                    'prompt_snapshot': r.get('prompt_snapshot'),
                    'metadata': r.get('metadata'),
                }.items() if value is not None
            }
            if kind == 'process':
                process_report = entry_payload
            else:
                bp_reports.append(entry_payload)
        proc_rating = _aggregate_rating(ratings)
        if proc_rating:
            all_ratings.append(proc_rating)
        if process_rationale:
            final_rationale = _sanitize_rating_labels(process_rationale)
        else:
            final_rationale = "; ".join(rationale_parts) if rationale_parts else None
        gaps_clean = process_gaps or []
        recs_clean = process_recs or []
        aggregate_entry = BulkProcessAggregate(
            process_id=proc,
            bp_ids=sorted(bp_ids),
            capability_rating=proc_rating,
            rationale=final_rationale,
            gaps=gaps_clean,
            recommendations=recs_clean,
            guideline_rules=list(rules.values()) or None,
            governance_checks=list(gov.values()) or None,
            contextual_warnings=list(warns.values()) or None,
        )
        processes.append(aggregate_entry)
        combined_process_reports.append({
            'aggregate': aggregate_entry.dict(exclude_none=True),
            'process_report': process_report,
            'base_practice_reports': bp_reports,
        })
        agg_stats["processes"][proc] = {
            "bp_count": len(bp_ids),
            "assessment_chars": assess_chars,
            "guideline_rules_count": len(rules),
            "governance_checks_count": len(gov),
            "contextual_warnings_count": len(warns),
            "guideline_text_chars": rule_text_chars,
            "governance_text_chars": gov_text_chars,
            "warning_text_chars": warn_text_chars,
        }
    overall = _aggregate_rating(all_ratings)
    overall_rationale = None
    if processes:
        parts = [f"{p.process_id}: {p.capability_rating or '?'}" for p in processes]
        overall_rationale = "; ".join(parts)
    # log and return stats
    agg_stats["overall"] = {
        "process_count": len(processes),
        "rating": overall,
        "bp_total": sum(v["bp_count"] for v in agg_stats["processes"].values()) if agg_stats["processes"] else 0,
        "chars_total": sum(v["assessment_chars"] + v["guideline_text_chars"] + v["governance_text_chars"] + v["warning_text_chars"] for v in agg_stats["processes"].values()) if agg_stats["processes"] else 0,
    }
    try:
        _emit_aggregate_log(group_id, agg_stats)
    except Exception:
        pass
    # Attach performance JSON if available via helper
    try:
        performance = _compute_group_performance(group_id)
    except Exception:
        performance = None
    combined_report: Dict[str, Any] = {
        'group_id': group_id,
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'overall_capability_rating': overall,
        'overall_rationale': overall_rationale,
        'project_summary': project_summary.dict(exclude_none=True) if project_summary else None,
        'project_report': project_report_data,
        'processes': combined_process_reports,
        'stats': agg_stats,
        'performance': performance,
    }

    if project_summary and top_project_entry:
        project_job = top_project_entry.get('job') or {}
        project_job_id = project_job.get('job_id')
        if project_job_id:
            combined_path = ARTIFACT_ROOT / str(project_job_id) / 'combined_report.json'
            try:
                combined_path.parent.mkdir(parents=True, exist_ok=True)
                combined_path.write_text(json.dumps(combined_report, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception:
                pass
            # Also emit strict export JSON alongside combined_report.json
            try:
                export_payload = _build_strict_combined_export(combined_report)
                export_path = ARTIFACT_ROOT / str(project_job_id) / 'combined_export.json'
                export_path.write_text(json.dumps(export_payload, ensure_ascii=False, indent=2), encoding='utf-8')
            except Exception:
                _BULK_LOG.exception("combined_export generation failed for project job_id=%s", project_job_id)

    return BulkGroupResultResponse(
        group_id=group_id,
        processes=processes,
        overall_capability_rating=overall,
        overall_rationale=overall_rationale,
        stats=agg_stats,
        project_summary=project_summary,
        combined_report=combined_report,
        performance=performance,
    )


def _safe_int(value: Any) -> Optional[int]:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _stage_format(stages: Dict[str, Any], label: Optional[str] = None) -> str:
    r = _safe_int(stages.get("retrieval_ms")) or 0
    i = _safe_int(stages.get("inference_ms")) or 0
    p = _safe_int(stages.get("persist_ms")) or 0
    t = _safe_int(stages.get("total_ms")) or (r + i + p)
    prefix = f"[{label} " if label else "[Stages "
    s = f"{prefix}retrieval:{r}ms | inference:{i}ms | persist:{p}ms | total:{t}ms]"
    a = _safe_int(stages.get("reasoning_pass_a_ms"))
    b = _safe_int(stages.get("reasoning_pass_b_ms"))
    if a is not None and b is not None:
        s = s[:-1] + f" | passA:{a}ms | passB:{b}ms]"
    return s


def _compute_group_performance(group_id: str) -> Dict[str, Any]:
    from pathlib import Path
    import json

    jobs = list_jobs(limit=None)
    group_jobs = [j for j in jobs if j.get("group_id") == group_id]

    # Process-level aggregation
    by_proc: Dict[str, List[Dict[str, Any]]] = {}
    project_nodes: List[Dict[str, Any]] = []
    for j in group_jobs:
        job_id = j.get("job_id")
        # Prefer in-memory result
        res = get_job_result(job_id)
        meta = None
        if res and isinstance(res, dict):
            meta = res.get("metadata")
        if meta is None:
            mpath = Path("assessment_runs") / str(job_id) / "metadata.json"
            if mpath.exists():
                try:
                    meta = json.loads(mpath.read_text(encoding="utf-8"))
                except Exception:
                    meta = None
        if not meta:
            continue
        proc = (j.get("process") or (str(j.get("bp_id", "")).rsplit(".", 1)[0]))
        bp_id = str(meta.get("bp_id") or j.get("bp_id") or "")
        kind = str((meta.get("job_kind") or j.get("job_kind") or "bp")).lower()
        # Input tokens (exact when recorded; fallback est via chars/4)
        prompt_tokens = meta.get("prompt_tokens")
        if prompt_tokens is None:
            ch = meta.get("prompt_chars") or 0
            try:
                prompt_tokens = int(ch // 4)
            except Exception:
                prompt_tokens = 0
        # Output tokens estimate from assessment size (chars/4)
        out_tokens = 0
        try:
            if kind == "process":
                apath = Path("assessment_runs") / str(job_id) / "process_assessment.json"
            elif kind == "project":
                apath = Path("assessment_runs") / str(job_id) / "project_assessment.json"
            else:
                apath = Path("assessment_runs") / str(job_id) / "assessment.json"
            if apath.exists():
                out_tokens = len(apath.read_text(encoding="utf-8")) // 4
        except Exception:
            out_tokens = 0
        # Timings
        stages = (meta.get("stage_timings") or {})
        total_ms = _safe_int(stages.get("total_ms")) or _safe_int(meta.get("duration_ms")) or 0
        t_sec = float(f"{(total_ms/1000.0):.1f}") if total_ms else 0.0
        label = bp_id
        if kind == "process":
            label = f"{proc}.summary" if proc else "Process.summary"
        elif kind == "project":
            label = "Project.summary"

        node = {
            "bp_id": label,
            "I_tokens": int(prompt_tokens or 0),
            "T_sec": t_sec,
            "O_tokens": int(out_tokens or 0),
            "format": f"[{label} I:{int(prompt_tokens or 0)}t > {t_sec}s > O:{int(out_tokens or 0)}t]",
            "job_kind": kind,
        }
        # Attach per-stage details if available
        if stages:
            node["stages"] = {
                k: _safe_int(v) for k, v in stages.items() if v is not None
            }
            node["stage_format"] = _stage_format(node["stages"], label=label)

        if kind == "project":
            project_nodes.append(node)
            continue

        by_proc.setdefault(str(proc), []).append(node)

    processes_payload: List[Dict[str, Any]] = []
    full_I = 0
    full_O = 0
    full_ms = 0
    full_stages: Dict[str, int] = {
        "retrieval_ms": 0,
        "inference_ms": 0,
        "persist_ms": 0,
        "total_ms": 0,
    }
    # Optional reasoning pass totals if present
    has_pass = False
    pass_a_sum = 0
    pass_b_sum = 0

    for proc, nodes in sorted(by_proc.items()):
        # Sort nodes by bp_id for stable output
        nodes.sort(key=lambda n: (0 if n.get("job_kind") == "process" else 1, n.get("bp_id", "")))
        sum_I = sum(int(n.get("I_tokens", 0)) for n in nodes)
        sum_O = sum(int(n.get("O_tokens", 0)) for n in nodes)
        sum_ms = 0
        sum_stages = {"retrieval_ms": 0, "inference_ms": 0, "persist_ms": 0, "total_ms": 0}
        a_sum = 0
        b_sum = 0
        for n in nodes:
            # seconds -> ms for summing
            sum_ms += int(float(n.get("T_sec", 0.0)) * 1000)
            st = n.get("stages") or {}
            sum_stages["retrieval_ms"] += _safe_int(st.get("retrieval_ms")) or 0
            sum_stages["inference_ms"] += _safe_int(st.get("inference_ms")) or 0
            sum_stages["persist_ms"] += _safe_int(st.get("persist_ms")) or 0
            sum_stages["total_ms"] += _safe_int(st.get("total_ms")) or 0
            a_sum += _safe_int(st.get("reasoning_pass_a_ms")) or 0
            b_sum += _safe_int(st.get("reasoning_pass_b_ms")) or 0
        if a_sum or b_sum:
            has_pass = True
        sum_node = {
            "I_tokens": sum_I,
            "T_sec": float(f"{(sum_ms/1000.0):.1f}"),
            "O_tokens": sum_O,
            "format": f"[{proc}.full I:{sum_I}t > {float(f'{(sum_ms/1000.0):.1f}')}s > O:{sum_O}t]",
            "stages": dict(sum_stages),
        }
        if has_pass:
            # Only include when present
            if a_sum:
                sum_node["stages"]["reasoning_pass_a_ms"] = a_sum
            if b_sum:
                sum_node["stages"]["reasoning_pass_b_ms"] = b_sum
        sum_node["stage_format"] = _stage_format(sum_node["stages"], label=f"{proc}.full")

        processes_payload.append({
            "process_id": proc,
            "sum": sum_node,
            "bps": nodes,
        })

        full_I += sum_I
        full_O += sum_O
        full_ms += sum_ms
        for k in ("retrieval_ms", "inference_ms", "persist_ms", "total_ms"):
            full_stages[k] += sum_stages.get(k, 0)
        pass_a_sum += a_sum
        pass_b_sum += b_sum

    project_payload = None
    if project_nodes:
        project_nodes.sort(key=lambda n: n.get("bp_id", ""))
        proj_I = sum(int(n.get("I_tokens", 0)) for n in project_nodes)
        proj_O = sum(int(n.get("O_tokens", 0)) for n in project_nodes)
        proj_ms = sum(int(float(n.get("T_sec", 0.0)) * 1000) for n in project_nodes)
        proj_stages = {"retrieval_ms": 0, "inference_ms": 0, "persist_ms": 0, "total_ms": 0}
        proj_a = 0
        proj_b = 0
        for n in project_nodes:
            st = n.get("stages") or {}
            proj_stages["retrieval_ms"] += _safe_int(st.get("retrieval_ms")) or 0
            proj_stages["inference_ms"] += _safe_int(st.get("inference_ms")) or 0
            proj_stages["persist_ms"] += _safe_int(st.get("persist_ms")) or 0
            proj_stages["total_ms"] += _safe_int(st.get("total_ms")) or 0
            proj_a += _safe_int(st.get("reasoning_pass_a_ms")) or 0
            proj_b += _safe_int(st.get("reasoning_pass_b_ms")) or 0
        if proj_a:
            proj_stages["reasoning_pass_a_ms"] = proj_a
        if proj_b:
            proj_stages["reasoning_pass_b_ms"] = proj_b
        project_sum_node = {
            "I_tokens": proj_I,
            "T_sec": float(f"{(proj_ms/1000.0):.1f}"),
            "O_tokens": proj_O,
            "format": f"[Project.full I:{proj_I}t > {float(f'{(proj_ms/1000.0):.1f}')}s > O:{proj_O}t]",
            "stages": proj_stages,
        }
        project_sum_node["stage_format"] = _stage_format(project_sum_node["stages"], label="Project.full")
        project_payload = {
            "process_id": "Project",
            "sum": project_sum_node,
            "bps": project_nodes,
        }

        full_I += proj_I
        full_O += proj_O
        full_ms += proj_ms
        for k in ("retrieval_ms", "inference_ms", "persist_ms", "total_ms"):
            full_stages[k] += project_sum_node["stages"].get(k, 0)
        pass_a_sum += _safe_int(project_sum_node["stages"].get("reasoning_pass_a_ms")) or 0
        pass_b_sum += _safe_int(project_sum_node["stages"].get("reasoning_pass_b_ms")) or 0

    full_scope = {
        "I_tokens": full_I,
        "T_sec": float(f"{(full_ms/1000.0):.1f}"),
        "O_tokens": full_O,
        "format": f"[FullScope.full I:{full_I}t > {float(f'{(full_ms/1000.0):.1f}')}s > O:{full_O}t]",
        "stages": dict(full_stages),
    }
    if pass_a_sum or pass_b_sum:
        full_scope["stages"]["reasoning_pass_a_ms"] = pass_a_sum
        full_scope["stages"]["reasoning_pass_b_ms"] = pass_b_sum
    full_scope["stage_format"] = _stage_format(full_scope["stages"], label="FullScope.full")

    return {
        "group_id": group_id,
        "full_scope": full_scope,
        "processes": processes_payload,
        "project": project_payload,
    }


@router.get("/bulk_jobs/{group_id}/performance", tags=["jobs"])
def bulk_performance(group_id: str) -> JSONResponse:
    """Return the exact Performance JSON Format for the given group.

    Keys and structure follow the README’s Performance JSON Format (exact).
    """
    payload = _compute_group_performance(group_id)
    return JSONResponse(payload)


# -----------------------------
# Human-readable Combined Export
# -----------------------------

def _load_confluence_host() -> Optional[str]:
    try:
        saved = _load_saved_pat()  # type: ignore[name-defined]
    except Exception:
        saved = None
    if isinstance(saved, dict):
        host = str(saved.get("confluence_host") or "").strip()
        if host:
            return host.rstrip("/")
    return None


def _build_confluence_url(md: Dict[str, Any], *, host: Optional[str]) -> Optional[str]:
    if not host:
        return None
    page_id = md.get("page_id") or md.get("pageId")
    if not page_id:
        return None
    heading = None
    try:
        hp = md.get("heading_path") or []
        if isinstance(hp, list) and hp:
            heading = str(hp[-1])
    except Exception:
        heading = None
    if not heading:
        heading = str(md.get("section_heading") or md.get("title") or "").strip() or None
    def _escape(s: str) -> str:
        return requests.utils.quote(" ".join(s.split())[:300], safe="")
    fragment = f"#:~:text={_escape(heading)}" if heading else ""
    return f"{host}/pages/viewpage.action?pageId={page_id}{fragment}"


def _collect_confluence_urls_from_snapshot(snapshot: Dict[str, Any], host: Optional[str]) -> List[str]:
    urls: List[str] = []
    seen: Set[str] = set()
    if not isinstance(snapshot, dict):
        return urls
    for item in (snapshot.get("evidence") or []):
        md = (item or {}).get("metadata") or {}
        url = _build_confluence_url(md, host=host)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _clean_text_list(values: Any) -> List[str]:
    out: List[str] = []
    if isinstance(values, list):
        for item in values:
            try:
                text = str(item).strip()
            except Exception:
                continue
            if text:
                out.append(text)
    return out


def _build_strict_combined_export(combined: Dict[str, Any]) -> Dict[str, Any]:
    """Build strict combined export JSON matching the required structure.

    Top-level object contains only { "project": { ... } }.
    Keys and casing are preserved as requested (e.g., 'Rationale', 'pam4.0').
    Arrays are present even when empty.
    """
    host = _load_confluence_host()

    def evidence_from_snapshot(snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        snap = snapshot or {}
        pam = [{'text': str(item.get('text') or '')} for item in (snap.get('reference_passages') or []) if isinstance(item, dict)]
        gl = [{'text': str(item.get('text') or '')} for item in (snap.get('guideline_rules') or []) if isinstance(item, dict)]
        docs: List[Dict[str, Any]] = []
        def _fragment_label(url: Optional[str]) -> Optional[str]:
            if not url or '#:~:text=' not in url:
                return None
            try:
                frag = url.split('#:~:text=', 1)[1]
                for sep in ['&', '#']:
                    if sep in frag:
                        frag = frag.split(sep, 1)[0]
                frag = frag.replace('+', ' ')
                return requests.utils.unquote(frag).strip()
            except Exception:
                return None

        for it in (snap.get('evidence') or []):
            if not isinstance(it, dict):
                continue
            md = (it.get('metadata') or {})
            # Prefer the most specific heading label for display over the page title.
            heading = None
            try:
                hp = md.get('heading_path') or []
                if isinstance(hp, list) and hp:
                    heading = str(hp[-1]).strip()
            except Exception:
                heading = None
            # Ignore generic headings like "Table of contents"
            if heading and str(heading).strip().lower() in {"table of contents", "contents", "toc"}:
                heading = None
            if not heading:
                heading = str(md.get('section_heading') or md.get('title') or '').strip() or None
            # If still missing, fall back to the snippet's first line of text
            label_from_text = None
            try:
                tv = str(it.get('text') or '')
                if tv:
                    label_from_text = tv.splitlines()[0].strip()[:120]
            except Exception:
                label_from_text = None
            # Final fallback: page title or provided document name
            page_title = str(md.get('page_title') or '').strip()
            doc_fallback = str(it.get('document') or '').strip()
            name = heading or label_from_text or page_title or doc_fallback or 'Document'
            url = _build_confluence_url(md, host=host)
            frag_label = _fragment_label(url)
            if frag_label:
                # Override when the fragment label differs from the current name
                low = name.strip().lower()
                if frag_label.strip().lower() not in low:
                    name = frag_label
            docs.append({'document': name, 'url': url})
        return {
            'pam4.0': pam,
            'guidelines': gl,
            'documents': docs,
        }

    project: Dict[str, Any] = {}
    proj_summary = (combined.get('project_summary') or {})
    project['capability_rating'] = proj_summary.get('capability_rating')
    project['Rationale'] = proj_summary.get('rationale')
    project['gaps'] = list(proj_summary.get('gaps') or [])
    project['recommendations'] = list(proj_summary.get('recommendations') or [])
    # Project-level evidence from the project_report snapshot if available
    project_snapshot = ((combined.get('project_report') or {}).get('prompt_snapshot') or {})
    project['evidence'] = evidence_from_snapshot(project_snapshot)

    processes_out: List[Dict[str, Any]] = []
    for proc_entry in (combined.get('processes') or []):
        if not isinstance(proc_entry, dict):
            continue
        agg = proc_entry.get('aggregate') or {}
        proc_snap = (proc_entry.get('process_report') or {}).get('prompt_snapshot') or {}

        base_practices: List[Dict[str, Any]] = []
        for bp in (proc_entry.get('base_practice_reports') or []):
            if not isinstance(bp, dict):
                continue
            assess = bp.get('assessment') or {}
            snap = bp.get('prompt_snapshot') or {}
            # Capability checks → list of strings
            checks: List[str] = []
            for it in (assess.get('capability_checks') or []):
                if not isinstance(it, dict):
                    continue
                ref = str(it.get('reference') or it.get('check_id') or '')
                note = str(it.get('note') or '')
                val = (f"{ref}: {note}").strip(': ').strip()
                if val:
                    checks.append(val)
            # Relevant guideline rules → text strings (filtered to this BP when possible)
            rules: List[str] = []
            bp_id = str(assess.get('base_practice_id') or bp.get('bp_id') or '')
            for it in (snap.get('guideline_rules') or []):
                if not isinstance(it, dict):
                    continue
                bps = (it.get('base_practices') or [])
                if bps and bp_id and (bp_id not in bps):
                    continue
                txt = str(it.get('text') or '')
                if txt:
                    rules.append(txt)
            # Contextual warnings → messages
            warns: List[str] = []
            for it in (snap.get('contextual_warnings') or []):
                if not isinstance(it, dict):
                    continue
                msg = str(it.get('message') or '')
                if msg:
                    warns.append(msg)
            # Governance checks → friendly strings (prefer note, then code+text, then code/text)
            govs: List[str] = []
            for it in (snap.get('governance_checks') or []):
                if not isinstance(it, dict):
                    continue
                note = str(it.get('note') or '').strip()
                code = str(it.get('check_id') or it.get('code') or '').strip()
                text = str(it.get('text') or '').strip()
                value = note or (f"{code}: {text}".strip(': ').strip()) or code or text
                if value:
                    govs.append(value)

            base_practices.append({
                'bp_id': bp_id or bp.get('bp_id'),
                'capability_rating': assess.get('capability_rating'),
                'Rationale': assess.get('rationale'),
                'gaps': assess.get('gaps') or [],
                'recommendations': assess.get('recommendations') or [],
                'evidence': evidence_from_snapshot(snap),
                'capability_level_checks': checks,
                'relevant_guideline_rules': rules,
                'contextual_warnings': warns,
                'governance_checks': govs,
            })

        processes_out.append({
            'process_id': agg.get('process_id'),
            'capability_rating': agg.get('capability_rating'),
            'Rationale': agg.get('rationale'),
            'gaps': agg.get('gaps') or [],
            'recommendations': agg.get('recommendations') or [],
            'evidence': evidence_from_snapshot(proc_snap),
            'base_practices': base_practices,
        })

    project['processes'] = processes_out
    return {
        'project': project
    }


@router.get("/bulk_jobs/{group_id}/export_human", tags=["jobs"])
def bulk_export_human(
    group_id: str,
    parent_report_id: Optional[str] = Query(None),
    offset: int = Query(0, ge=0),
    limit: Optional[int] = Query(None, ge=1),
) -> JSONResponse:
    """Export a human‑readable Combined Report JSON.

    - No truncation: return full rationale/summary text from assessments.
    - Confluence links at project/process/BP levels when page ids exist.
    - Optional paging of BPs via offset/limit per process.
    """
    group_payload = bulk_result(group_id, parent_report_id)  # type: ignore[misc]
    combined = group_payload.combined_report or {}
    host = _load_confluence_host()

    proj_summary = (group_payload.project_summary.dict(exclude_none=True) if group_payload.project_summary else {})  # type: ignore[union-attr]
    project_urls: List[str] = []
    project_snapshot = ((combined.get("project_report") or {}).get("prompt_snapshot") or {})
    project_urls.extend(_collect_confluence_urls_from_snapshot(project_snapshot, host))

    processes_out: List[Dict[str, Any]] = []
    total_bp = 0
    for proc_entry in (combined.get("processes") or []):
        agg = proc_entry.get("aggregate") or {}
        proc_id = str(agg.get("process_id") or "").strip()
        if not proc_id:
            continue
        original_bps = list(proc_entry.get("base_practice_reports") or [])
        original_bps.sort(key=lambda r: (r.get("bp_id") or ""))
        total_bp += len(original_bps)
        start = int(offset or 0)
        end = (start + int(limit)) if (limit is not None) else None
        page_slice = original_bps[start:end]
        bp_out: List[Dict[str, Any]] = []
        proc_urls: List[str] = []
        for bp in page_slice:
            assess = bp.get("assessment") or {}
            snap = bp.get("prompt_snapshot") or {}
            bp_urls = _collect_confluence_urls_from_snapshot(snap, host)
            proc_urls.extend(u for u in bp_urls if u)
            project_urls.extend(u for u in bp_urls if u)
            bp_out.append({
                "bp_id": assess.get("base_practice_id") or bp.get("bp_id"),
                "title": assess.get("base_practice_title") or (snap.get("base_practice") or {}).get("title"),
                "capability_rating": assess.get("capability_rating"),
                "summary": assess.get("rationale"),
                "gaps": assess.get("gaps") or [],
                "recommendations": assess.get("recommendations") or [],
                "capability_checks": assess.get("capability_checks") or [],
                "citations": [
                    {
                        "page_id": (it.get("metadata") or {}).get("page_id") or (it.get("metadata") or {}).get("pageId"),
                        "page_title": (it.get("metadata") or {}).get("page_title") or (it.get("metadata") or {}).get("title"),
                        "heading_path": (it.get("metadata") or {}).get("heading_path"),
                        "confluence_url": _build_confluence_url((it.get("metadata") or {}), host=host),
                        "text_fragment": (it.get("text") or "")[:200],
                    }
                    for it in (snap.get("evidence") or [])
                ],
                "confluence_urls": bp_urls,
                "metrics": {
                    "evidence_count": len(snap.get("evidence") or []),
                    "normative_count": len(snap.get("reference_passages") or []),
                    "guideline_count": len(snap.get("guideline_rules") or []),
                    "prompt_tokens": bp.get("metadata", {}).get("prompt_tokens"),
                },
            })
        # Deduplicate proc urls
        seen_p: Set[str] = set()
        proc_urls = [u for u in proc_urls if (u and (u not in seen_p) and not seen_p.add(u)) or (u and u not in seen_p)]
        processes_out.append({
            "process_id": proc_id,
            "capability_rating": agg.get("capability_rating"),
            "summary": agg.get("rationale"),
            "guideline_rules": agg.get("guideline_rules"),
            "governance_checks": agg.get("governance_checks"),
            "contextual_warnings": agg.get("contextual_warnings"),
            "bps": bp_out,
            "bp_paging": {
                "offset": start,
                "limit": (limit if limit is not None else len(original_bps)),
                "total": len(original_bps),
                "has_more": bool((limit is not None) and (len(original_bps) > (end or 0))),
            },
            "confluence_urls": proc_urls,
        })

    seen_proj: Set[str] = set()
    project_urls = [u for u in project_urls if (u and (u not in seen_proj) and not seen_proj.add(u)) or (u and u not in seen_proj)]

    payload: Dict[str, Any] = {
        "format": "combined_report_human_v1",
        "group_id": group_id,
        "parent_report_id": parent_report_id,
        "created_at": time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        "project": {
            "capability_rating": proj_summary.get("capability_rating"),
            "confidence": proj_summary.get("confidence"),
            "summary": proj_summary.get("rationale"),
            "included": {
                "process_count": len(proj_summary.get("included_processes") or []),
                "bp_count": total_bp,
            },
            "confluence_urls": project_urls,
        },
        "processes": processes_out,
        "attachments": {
            "combined_json_href": f"/bulk_jobs/{group_id}/result{('?parent_report_id='+parent_report_id) if parent_report_id else ''}",
            "performance_href": f"/bulk_jobs/{group_id}/performance",
        },
    }
    return JSONResponse(payload)


# -----------------------------
# Strict Combined Export (schema-aligned)
# -----------------------------

@router.get("/reports/{parent_report_id}/combined_export.json", tags=["jobs"])
def get_combined_export(parent_report_id: str, group_id: str = Query(...)) -> JSONResponse:
    """Return the strict combined export JSON for a scoped report.

    If the on-disk artifact is missing, compute via bulk_result (which will
    also attempt to persist the export) and return the in-memory payload.
    """
    # Compute/refresh combined structures
    group_payload = bulk_result(group_id, parent_report_id)  # type: ignore[misc]
    combined = group_payload.combined_report or {}

    # Try to read persisted artifact under the project job directory
    try:
        jobs = list_jobs(limit=None)
        scoped = [j for j in jobs if j.get('group_id') == group_id and j.get('parent_report_id') == parent_report_id and str((j.get('job_kind') or 'bp')).lower() == 'project']
        project_job_id = scoped[0].get('job_id') if scoped else None
    except Exception:
        project_job_id = None

    if project_job_id:
        export_path = ARTIFACT_ROOT / str(project_job_id) / 'combined_export.json'
        if export_path.exists():
            try:
                return JSONResponse(json.loads(export_path.read_text(encoding='utf-8')))
            except Exception:
                pass

    # Fallback: build in-memory
    try:
        payload = _build_strict_combined_export(combined)
    except Exception:
        payload = {'project': {}}
    return JSONResponse(payload)


@router.post("/bulk_jobs/{group_id}/assess_processes", response_model=BulkJobsResponse, tags=["jobs"], status_code=201)
async def assess_processes(group_id: str, request: ProcessAssessRequest = Body(...), parent_report_id: Optional[str] = Query(None)) -> BulkJobsResponse:
    job_ids = await enqueue_process_passes(
        group_id,
        parent_report_id=parent_report_id,
        reasoning_enabled=request.reasoning_enabled,
        skip_if_exists=False,
        require_completed_bp=False,
    )
    return BulkJobsResponse(group_id=group_id, job_ids=job_ids, parent_report_id=parent_report_id)


@router.post("/bulk_jobs/{group_id}/assess_project", tags=["jobs"], status_code=201)
async def assess_project(group_id: str, request: ProjectAssessRequest = Body(...), parent_report_id: Optional[str] = Query(None)) -> JSONResponse:
    job_id = await enqueue_project_pass(
        group_id,
        parent_report_id=parent_report_id,
        reasoning_enabled=request.reasoning_enabled,
        skip_if_exists=False,
        require_completed_processes=False,
    )
    if job_id is None:
        # Fallback to direct enqueue to preserve legacy behavior when no process jobs exist.
        job_id = uuid.uuid4().hex
        payload: Dict[str, Any] = {
            "job_id": job_id,
            "job_kind": "project",
            "group_id": group_id,
            "backend": "xgrammar",
            "model_profile": getattr(settings, "model_profile", None),
            "model_id": settings.model_id,
            "quantization": getattr(settings, "quantization", None),
            "tensor_parallel_size": getattr(settings, "tensor_parallel_size", None),
            "gpu_memory_utilization": settings.gpu_memory_utilization,
            "kv_cache_memory_bytes": getattr(settings, "kv_cache_memory_bytes", None),
            "schema_kind": "project",
        }
        if request.reasoning_enabled is not None:
            payload["reasoning_enabled"] = bool(request.reasoning_enabled)
        initialize_job(job_id, payload)
        await scheduler.enqueue(payload)
    return JSONResponse({"group_id": group_id, "job_id": job_id, "parent_report_id": parent_report_id})


@router.delete("/reports/{parent_report_id}", response_model=DeleteJobsResponse, tags=["jobs"])
def delete_report(parent_report_id: str, group_id: str = Query(...), purge_artifacts: bool = Query(True)) -> DeleteJobsResponse:
    jobs = list_jobs(limit=None)
    scoped = [j for j in jobs if j.get("group_id") == group_id and j.get("parent_report_id") == parent_report_id]
    if not scoped:
        return DeleteJobsResponse(deleted_ids=[])
    deleted: List[str] = []
    for j in scoped:
        jid = j.get("job_id")
        if not jid:
            continue
        try:
            if delete_job(jid, purge_artifacts=bool(purge_artifacts)):
                deleted.append(jid)
        except Exception:
            continue
    return DeleteJobsResponse(deleted_ids=deleted)


class ReportRerunResponse(BaseModel):
    group_id: str
    parent_report_id: str
    job_ids: List[str]


@router.post("/reports/{parent_report_id}/rerun", response_model=ReportRerunResponse, tags=["jobs"], status_code=201)
async def rerun_report(parent_report_id: str, group_id: str = Query(...)) -> ReportRerunResponse:
    jobs = list_jobs(limit=None)
    scoped = [j for j in jobs if j.get("group_id") == group_id and j.get("parent_report_id") == parent_report_id]
    bp_jobs = [j for j in scoped if str((j.get("job_kind") or "bp")).lower() == "bp"]
    job_ids: List[str] = []
    # Rerun each BP with inherited configuration
    for j in bp_jobs:
        jid = j.get("job_id")
        if not jid:
            continue
        meta = _load_job_metadata(jid)
        bp_id = meta.get("bp_id")
        if not bp_id:
            continue
        new_job_id = uuid.uuid4().hex
        payload: Dict[str, Any] = {
            "job_id": new_job_id,
            "bp_id": bp_id,
            "top_k": int(meta.get("top_k") or 5),
            "backend": meta.get("backend") or "xgrammar",
            "notes": meta.get("notes"),
            "rerun_of": jid,
            "group_id": group_id,
            "parent_report_id": parent_report_id,
            "process": str(bp_id).rsplit(".", 1)[0] if "." in str(bp_id) else None,
            "model_profile": meta.get("model_profile") or getattr(settings, "model_profile", None),
            "model_id": meta.get("model_id") or settings.model_id,
            "quantization": meta.get("quantization") or getattr(settings, "quantization", None),
            "tensor_parallel_size": meta.get("tensor_parallel_size") or getattr(settings, "tensor_parallel_size", None),
            "gpu_memory_utilization": meta.get("gpu_memory_utilization") or settings.gpu_memory_utilization,
            "kv_cache_memory_bytes": meta.get("kv_cache_memory_bytes") or getattr(settings, "kv_cache_memory_bytes", None),
        }
        mk = meta.get("manual_keywords")
        if mk is not None:
            payload["manual_keywords"] = mk
        fw = meta.get("fusion_weights")
        if fw is not None:
            payload["fusion_weights"] = fw
        if bool(meta.get("reasoning_enabled")):
            payload["reasoning_enabled"] = True
        initialize_job(new_job_id, payload)
        await scheduler.enqueue(payload)
        job_ids.append(new_job_id)
    return ReportRerunResponse(group_id=group_id, parent_report_id=parent_report_id, job_ids=job_ids)


@router.get("/evidence_preview", response_model=List[EvidenceItem], tags=["aspice"])
def evidence_preview(
    bp_id: str = Query(..., description="Base practice id, e.g. MAN.3.BP4"),
    top_k: int = Query(5, ge=1, le=20),
    manual_keywords: Optional[str] = Query(None, description="Comma-separated extra keywords"),
    aspice_normative: Optional[float] = Query(None, ge=0.0, le=1.0),
    aspice_guidelines: Optional[float] = Query(None, ge=0.0, le=1.0),
) -> List[EvidenceItem]:
    engine = _get_engine()
    kws: List[str] | None = None
    if manual_keywords:
        kws = [s.strip() for s in manual_keywords.split(",") if s and s.strip()]
        if not kws:
            kws = None
    fusion_weights: Dict[str, float] | None = None
    if aspice_normative is not None or aspice_guidelines is not None:
        fusion_weights = {}
        if aspice_normative is not None:
            fusion_weights["aspice_normative"] = float(aspice_normative)
        if aspice_guidelines is not None:
            fusion_weights["aspice_guidelines"] = float(aspice_guidelines)
    results = engine.retrieve_evidence(
        bp_id,
        top_k=top_k,
        manual_keywords=kws,
        fusion_weights=fusion_weights,
    )
    payload: List[EvidenceItem] = []
    for r in results:
        payload.append(EvidenceItem(score=float(r.score), text=r.text, metadata=dict(r.metadata)))
    return payload


@router.post("/documents/evaluate", response_model=EvaluateDocumentsResponse, tags=["aspice"])
def evaluate_documents(request: EvaluateDocumentsRequest) -> EvaluateDocumentsResponse:
    if not request.bp_ids:
        return EvaluateDocumentsResponse(bps=[])
    engine = _get_engine()
    manual_keywords = _sanitize_keywords(request.manual_keywords)
    stop_words = _sanitize_keywords(request.stop_words)
    fusion_weights = _sanitize_fusion_weights(request.fusion_weights)
    items: List[Dict[str, Any]] = []
    # Union of allowed Confluence page ids across spaces when selection files exist
    allowed_page_ids: Set[str] = set()
    try:
        for space_dir in (CORPUS_ROOT.glob("*/.sync")):
            try:
                data = json.loads((space_dir / "selection.json").read_text(encoding="utf-8"))
            except Exception:
                data = None
            if isinstance(data, dict):
                for pid in data.get("include_ids") or []:
                    try:
                        allowed_page_ids.add(str(pid))
                    except Exception:
                        continue
    except Exception:
        allowed_page_ids = set()
    for bp_id in request.bp_ids:
        try:
            prompt_payload = engine.prepare_prompt(
                bp_id,
                top_k=request.top_k,
                manual_keywords=manual_keywords,
                fusion_weights=fusion_weights,
                stop_words=stop_words,
                allowed_page_ids=list(allowed_page_ids) if allowed_page_ids else None,
                include_related_process_evidence=request.include_related_process_evidence,
                related_process_k=request.related_process_k,
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=f"Unknown base practice: {bp_id}") from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Failed to evaluate documents for {bp_id}: {exc}") from exc
        subset: Dict[str, Any] = {
            "bp_id": bp_id,
            "process_id": prompt_payload.get("process_id"),
            "process_name": prompt_payload.get("process_name"),
            "base_practice": prompt_payload.get("base_practice"),
            "evidence": prompt_payload.get("evidence") or [],
            "reference_passages": prompt_payload.get("reference_passages") or [],
            "guideline_rules": prompt_payload.get("guideline_rules") or [],
            "supporting_evidence": prompt_payload.get("supporting_evidence") or [],
            "related_processes": prompt_payload.get("related_processes") or [],
            "retrieval_query": prompt_payload.get("retrieval_query"),
            "fusion_weights": prompt_payload.get("fusion_weights"),
        }
        items.append(subset)
    return EvaluateDocumentsResponse(bps=items)


def _resolve_corpus_path(path_value: str) -> Path:
    """Resolve a user-supplied corpus path safely.

    Accepts either a repo-relative path (e.g., 'corpus/…/file.md') or an absolute
    path when it resides under the configured CORPUS_ROOT. Rejects any path that
    escapes the corpus directory.
    """
    try:
        raw = Path(path_value)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid path parameter") from exc

    try:
        corpus_root = CORPUS_ROOT.resolve()
        if raw.is_absolute():
            candidate = raw.resolve()
        else:
            candidate = (APP_ROOT / raw).resolve()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid path resolution") from exc

    # Enforce sandbox: must live within corpus
    if not str(candidate).startswith(str(corpus_root)):
        raise HTTPException(status_code=400, detail="Path must reside within the corpus directory")
    if not candidate.exists():
        raise HTTPException(status_code=404, detail="Document not found")
    return candidate


def _map_markdown_to_html_path(path: Path) -> Optional[Path]:
    try:
        if path.suffix.lower() != ".md":
            return None
        stem = path.stem
        parent = path.parent
        if "SWT1AQ_md" in parent.parts:
            parts = list(parent.parts)
            idx = parts.index("SWT1AQ_md")
            parts[idx] = "SWT1AQ"
            html_path = Path(*parts) / f"{stem}.html"
            html_candidate = (APP_ROOT / html_path).resolve()
            if html_candidate.exists():
                return html_candidate
        html_candidate = path.with_suffix(".html")
        if html_candidate.exists():
            return html_candidate
    except Exception:
        return None
    return None


def _render_markdown_snippet(content: str, title: str) -> str:
    escaped = html.escape(content)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: "Inter", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      --surface: #ffffff;
      --surface-muted: #f1f5f9;
      --border: #cbd5f5;
      --text: #0f172a;
      --text-subtle: #475569;
      --accent: #1d4ed8;
    }}
    body {{
      margin: 2rem;
      background: var(--surface);
      color: var(--text);
    }}
    pre {{
      background: var(--surface-muted);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      padding: 1.5rem;
      white-space: pre-wrap;
      line-height: 1.6;
      font-size: 0.95rem;
    }}
    a {{ color: var(--accent); }}
    h1 {{
      font-size: 1.25rem;
      font-weight: 600;
      margin-bottom: 1rem;
      color: var(--text);
    }}
  </style>
</head>
<body>
  <h1>{html.escape(title)}</h1>
  <pre>{escaped}</pre>
</body>
</html>"""


def _render_html_snippet(title: str, snippet: str, source_path: Path) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <title>{html.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      font-family: "Inter", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
      --surface: #ffffff;
      --surface-muted: #f8fafc;
      --border: #cbd5f5;
      --text: #0f172a;
      --text-subtle: #475569;
      --accent: #1d4ed8;
      --mark-bg: #ECFEDE;
    }}
    body {{
      margin: 2rem;
      background: var(--surface-muted);
      color: var(--text);
    }}
    .source {{
      font-size: 0.75rem;
      color: var(--text-subtle);
      margin-bottom: 1rem;
    }}
    .content {{
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 0.75rem;
      padding: 1.5rem;
      line-height: 1.6;
      overflow-x: auto;
      box-shadow: 0 8px 24px rgba(15, 23, 42, 0.08);
    }}
    a {{ color: var(--accent); }}
    mark {{ background: var(--mark-bg); color: var(--text); }}
    h1, h2, h3, h4, h5, h6 {{ color: var(--text); }}
  </style>
</head>
<body>
  <div class="source">{html.escape(str(source_path))}</div>
  <div class="content">
    {snippet}
  </div>
</body>
</html>"""


def _extract_html_by_heading(content: str, heading: Optional[str]) -> Optional[str]:
    if not heading:
        return None

    def normalize(value: str) -> str:
        if not value:
            return ""
        without_tags = re.sub(r"<[^>]+>", " ", value)
        unescaped = html.unescape(without_tags)
        return re.sub(r"\s+", " ", unescaped).strip().lower()

    heading_norm = normalize(heading)
    if not heading_norm:
        return None

    pattern = re.compile(r"<h([1-6])[^>]*>(.*?)</h\1>", re.IGNORECASE | re.DOTALL)
    matches = list(pattern.finditer(content))
    for idx, match in enumerate(matches):
        inner_html = match.group(2)
        inner_norm = normalize(inner_html)
        if not inner_norm:
            continue
        if heading_norm in inner_norm or inner_norm in heading_norm:
            level = int(match.group(1))
            start_index = match.start()
            end_index = len(content)
            for next_match in matches[idx + 1:]:
                next_level = int(next_match.group(1))
                if next_level <= level:
                    end_index = next_match.start()
                    break
            snippet = content[start_index:end_index].strip()
            if snippet:
                return snippet
    return None


def _extract_html_by_snippet(content: str, snippet: Optional[str]) -> Optional[str]:
    if not snippet:
        return None

    def normalize(value: str) -> str:
        if not value:
            return ""
        return re.sub(r"\s+", " ", html.unescape(value)).strip().lower()

    snippet_norm = normalize(snippet)
    if not snippet_norm:
        return None

    segments = []
    for match in re.finditer(r"<[^>]+>|[^<]+", content):
        token = match.group(0)
        if token.startswith("<"):
            continue
        normalized = normalize(token)
        if not normalized:
            continue
        segments.append((match.start(), match.end(), normalized))

    for i in range(len(segments)):
        combined = segments[i][2]
        start_index = segments[i][0]
        if snippet_norm in combined:
            return content[start_index:segments[i][1]].strip()
        for j in range(i + 1, len(segments)):
            combined = f"{combined} {segments[j][2]}".strip()
            if snippet_norm in combined:
                return content[start_index:segments[j][1]].strip()

    return None


@router.get("/documents/chunk", response_class=HTMLResponse, tags=["aspice"])
def get_document_chunk(
    path: str = Query(..., description="Relative corpus path (e.g., corpus/SWT1AQ_md/foo.md)"),
    start: Optional[int] = Query(None, ge=1, description="Optional start line (1-indexed)"),
    end: Optional[int] = Query(None, ge=1, description="Optional end line (1-indexed, inclusive)"),
    heading: Optional[str] = Query(None, description="Optional section heading hint"),
    snippet_text: Optional[str] = Query(
        None,
        alias="snippet",
        description="Optional snippet used for highlighting and markdown rendering",
    ),
) -> HTMLResponse:
    file_path = _resolve_corpus_path(path)
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to read document: {exc}") from exc
    suffix = file_path.suffix.lower()
    html_snippet: Optional[str] = None
    source_path = file_path
    if suffix == ".md":
        html_candidate = _map_markdown_to_html_path(file_path)
        if html_candidate and html_candidate.exists():
            try:
                content = html_candidate.read_text(encoding="utf-8")
                source_path = html_candidate
                suffix = ".html"
            except Exception:
                content = file_path.read_text(encoding="utf-8")
                suffix = ".md"
    snippet_param = snippet_text.strip() if isinstance(snippet_text, str) and snippet_text.strip() else None

    if suffix == ".html":
        html_snippet = _extract_html_by_heading(content, heading) or _extract_html_by_snippet(content, snippet_param)
        if not html_snippet and snippet_param:
            return HTMLResponse(_render_markdown_snippet(snippet_param, source_path.name))
        snippet_payload = html_snippet or content
        return HTMLResponse(_render_html_snippet(source_path.name, snippet_payload, source_path))
    lines = content.splitlines()
    snippet_body = snippet_param
    if snippet_body is None:
        if start is not None or end is not None:
            start_idx = max((start or 1) - 1, 0)
            end_idx = end if end is not None else start_idx + 1
            end_idx = min(max(end_idx, start_idx + 1), len(lines))
            snippet_body = "\n".join(lines[start_idx:end_idx])
        else:
            snippet_body = content
    if (not snippet_body or not snippet_body.strip()) and lines:
        snippet_body = "\n".join(lines[: min(len(lines), 200)])
    html_payload = _render_markdown_snippet(snippet_body, file_path.name)
    return HTMLResponse(html_payload)


@router.post("/jobs", response_model=CreateJobResponse, tags=["jobs"], status_code=201)
async def create_job(request: CreateJobRequest) -> CreateJobResponse:
    job_id = uuid.uuid4().hex
    manual_keywords = _sanitize_keywords(request.manual_keywords)
    fusion_weights = _sanitize_fusion_weights(request.fusion_weights)
    payload: Dict[str, Any] = {
        "job_id": job_id,
        "bp_id": request.bp_id,
        "top_k": request.top_k,
        "backend": request.backend,
        "notes": request.notes,
        "model_profile": getattr(settings, "model_profile", None),
        "model_id": settings.model_id,
        "quantization": getattr(settings, "quantization", None),
        "tensor_parallel_size": getattr(settings, "tensor_parallel_size", None),
        "gpu_memory_utilization": settings.gpu_memory_utilization,
        "kv_cache_memory_bytes": getattr(settings, "kv_cache_memory_bytes", None),
    }
    if manual_keywords:
        payload["manual_keywords"] = manual_keywords
    if fusion_weights:
        payload["fusion_weights"] = fusion_weights
    if request.include_related_process_evidence is not None:
        payload["include_related_process_evidence"] = bool(request.include_related_process_evidence)
    if request.related_process_k is not None:
        payload["related_process_k"] = int(request.related_process_k)
    if request.reasoning_enabled is not None:
        payload["reasoning_enabled"] = bool(request.reasoning_enabled)
    return await _enqueue_job(payload)


@router.get("/jobs", response_model=JobListResponse, tags=["jobs"])
def job_list(limit: int = 50) -> JobListResponse:
    safe_limit = max(1, min(limit, 1000))
    jobs = list_jobs(limit=safe_limit)
    return JobListResponse(jobs=[JobStatusResponse(**job) for job in jobs])


@router.get("/jobs/{job_id}", response_model=JobStatusResponse, tags=["jobs"])
def job_status(job_id: str) -> JobStatusResponse:
    status = get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobStatusResponse(**status)


@router.get("/jobs/{job_id}/result", response_model=JobResultResponse, tags=["jobs"])
def job_result(job_id: str) -> JobResultResponse:
    status = get_job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found")
    if status["status"] != "completed":
        raise HTTPException(status_code=409, detail="Job not completed")
    result = get_job_result(job_id)
    if result is None:
        raise HTTPException(status_code=500, detail="Result missing")
    return JobResultResponse(job_id=job_id, status="completed", result=result)


@router.delete("/jobs/{job_id}", status_code=204, tags=["jobs"])
def job_delete(job_id: str, purge_artifacts: bool = Query(True, description="Set true to delete stored artifacts.")) -> Response:
    # Default to purging artifacts for simplicity
    if not delete_job(job_id, purge_artifacts=bool(purge_artifacts)):
        raise HTTPException(status_code=404, detail="Job not found")
    return Response(status_code=204)


@router.delete("/jobs", response_model=DeleteJobsResponse, tags=["jobs"])
def jobs_bulk_delete(request: DeleteJobsRequest = Body(...)) -> DeleteJobsResponse:
    # Default to purge artifacts for bulk deletes
    purge = True if request.purge_artifacts is None else bool(request.purge_artifacts)
    if request.delete_all:
        candidate_ids = list_job_ids()
    else:
        job_ids = request.job_ids or []
        candidate_ids = []
        seen: Set[str] = set()
        for jid in job_ids:
            if not isinstance(jid, str):
                continue
            if jid in seen:
                continue
            seen.add(jid)
            candidate_ids.append(jid)
    if not candidate_ids:
        if request.delete_all:
            return DeleteJobsResponse(deleted_ids=[])
        raise HTTPException(status_code=400, detail="No job identifiers provided for deletion")

    deleted: List[str] = []
    skipped: List[str] = []
    for job_id in candidate_ids:
        if delete_job(job_id, purge_artifacts=purge):
            deleted.append(job_id)
        else:
            skipped.append(job_id)

    # If deleting all and purging artifacts, also remove any orphaned job directories and truncate log
    if request.delete_all and purge:
        try:
            from pathlib import Path
            from .job_store import ARTIFACT_ROOT, list_job_ids as _list_jobs
            survivors = set(_list_jobs())
            removed_dirs = 0
            if ARTIFACT_ROOT.exists():
                for path in ARTIFACT_ROOT.iterdir():
                    if not path.is_dir():
                        continue
                    if path.name in survivors:
                        continue
                    try:
                        import shutil
                        shutil.rmtree(path, ignore_errors=True)
                        removed_dirs += 1
                    except Exception:
                        pass
                # Truncate log file to avoid rehydrating deleted items on restart
                log_path = ARTIFACT_ROOT / 'log.jsonl'
                try:
                    log_path.write_text('', encoding='utf-8')
                except Exception:
                    pass
            try:
                logging.getLogger("backend").info(
                    "artifact prune after delete_all: removed_dirs=%s survivors=%s deleted_jobs=%s skipped=%s",
                    removed_dirs,
                    len(survivors),
                    len(deleted),
                    len(skipped) if skipped else 0,
                )
            except Exception:
                pass
        except Exception:
            pass

    return DeleteJobsResponse(deleted_ids=deleted, skipped_ids=skipped or None)


@router.get("/jobs/{job_id}/artifact/{name}", tags=["jobs"])
def job_artifact(job_id: str, name: Literal["prompt", "assessment", "reasoning_summary"]) -> Response:
    metadata = _load_job_metadata(job_id)
    artifacts = metadata.get("artifacts") or {}
    path = artifacts.get(name)
    if not path:
        raise HTTPException(status_code=404, detail="Artifact not found in metadata")
    file_path = Path(path)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing")
    try:
        text = file_path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Failed to read artifact: {exc}")
    media = "application/json" if name in ("prompt", "assessment") else "text/plain"
    try:
        _ARTIFACT_LOG.debug("job_artifact: served %s for job_id=%s path=%s bytes=%d", name, job_id, str(file_path), len(text))
    except Exception:
        pass
    return Response(content=text, media_type=media)


@router.post("/jobs/{job_id}/rerun", response_model=CreateJobResponse, tags=["jobs"])
async def rerun_job(job_id: str, request: RerunJobRequest) -> CreateJobResponse:
    metadata = _load_job_metadata(job_id)
    bp_id = metadata.get("bp_id")
    if not bp_id:
        raise HTTPException(status_code=500, detail="metadata.json missing bp_id")

    original_backend = metadata.get("backend") or "xgrammar"
    backend_choice = request.backend or original_backend
    manual_keywords = (
        _sanitize_keywords(request.manual_keywords)
        if request.manual_keywords is not None
        else _sanitize_keywords(metadata.get("manual_keywords"))
    )
    fusion_weights = (
        _sanitize_fusion_weights(request.fusion_weights)
        if request.fusion_weights is not None
        else _sanitize_fusion_weights(metadata.get("fusion_weights"))
    )
    notes = request.notes if request.notes is not None else metadata.get("notes")
    top_k = request.top_k or metadata.get("top_k") or 5

    new_job_id = uuid.uuid4().hex
    payload: Dict[str, Any] = {
        "job_id": new_job_id,
        "bp_id": bp_id,
        "top_k": int(top_k),
        "backend": backend_choice,
        "notes": notes,
        "rerun_of": job_id,
        "model_profile": metadata.get("model_profile") or getattr(settings, "model_profile", None),
        "model_id": metadata.get("model_id") or settings.model_id,
        "quantization": metadata.get("quantization") or getattr(settings, "quantization", None),
        "tensor_parallel_size": metadata.get("tensor_parallel_size") or getattr(settings, "tensor_parallel_size", None),
        "gpu_memory_utilization": metadata.get("gpu_memory_utilization") or settings.gpu_memory_utilization,
        "kv_cache_memory_bytes": metadata.get("kv_cache_memory_bytes") or getattr(settings, "kv_cache_memory_bytes", None),
    }
    # Preserve/override grouping and report scope for cascades
    payload["group_id"] = request.group_id or metadata.get("group_id")
    if request.parent_report_id is not None:
        payload["parent_report_id"] = request.parent_report_id
    elif metadata.get("parent_report_id") is not None:
        payload["parent_report_id"] = metadata.get("parent_report_id")
    # Derive process id for convenience
    try:
        payload["process"] = ".".join(str(bp_id).split(".")[:2]) if "." in str(bp_id) else None
    except Exception:
        payload["process"] = None
    if manual_keywords is not None:
        payload["manual_keywords"] = manual_keywords
    if fusion_weights is not None:
        payload["fusion_weights"] = fusion_weights
    if request.include_related_process_evidence is not None:
        payload["include_related_process_evidence"] = bool(request.include_related_process_evidence)
    if request.related_process_k is not None:
        payload["related_process_k"] = int(request.related_process_k)
    original_reasoning = metadata.get("reasoning_enabled")
    # Only include reasoning flag when explicitly requested by the UI,
    # or when the original job had reasoning enabled. Treat False/None as
    # equivalent to omission to avoid spurious configuration drift.
    if request.reasoning_enabled is not None:
        payload["reasoning_enabled"] = bool(request.reasoning_enabled)
    elif bool(original_reasoning):
        payload["reasoning_enabled"] = True

    return await _enqueue_job(payload)


@router.websocket("/jobs/{job_id}/events")
async def job_events(websocket: WebSocket, job_id: str) -> None:
    await websocket.accept()
    queue = register_listener(job_id)

    status = get_job_status(job_id)
    if status is not None:
        await websocket.send_json({"type": "snapshot", "payload": status})

    try:
        while True:
            payload = await queue.get()
            await websocket.send_json({"type": "event", "payload": payload})
    except WebSocketDisconnect:
        pass
    finally:
        unregister_listener(job_id, queue)


app = FastAPI(title="MAN.3 Assessment Backend", version="0.1.0")
app.include_router(router)

app.include_router(codebeamer_router)

# Canonical Engineering Model (CEM) pipeline endpoints.
from .cem.http import cem_router, warm_stage_cache  # noqa: E402  (deferred to avoid circular import at module top)
app.include_router(cem_router)

# CEM-Pipeline RAG (FAISS over CEM_PIPELINE.md sections).
from .cem_rag_router import cem_rag_router  # noqa: E402
app.include_router(cem_rag_router)

# Enable CORS for browser-based UIs (Svelte dev server)
_origins_env = os.environ.get(
    "CORS_ALLOW_ORIGINS",
    "http://127.0.0.1:5173,http://localhost:5173,http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:3000,http://localhost:3000",
)
_origins = [o.strip() for o in _origins_env.split(',') if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def _startup() -> None:
    await scheduler.start()
    # Warm the CEM staleness cache in the background so the first
    # /cem/stages request doesn't have to checksum twp_pal/ + twp_pip/.
    warm_stage_cache()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await scheduler.stop()


@router.post("/debug/clear_state", tags=["system"])
def debug_clear_state(artifacts: bool = False) -> JSONResponse:
    summary = jobstore_clear_state(clear_artifacts=bool(artifacts))
    return JSONResponse({"status": "ok", **summary})

# Ensure clear_state is also exposed directly on the app for clients that bypass APIRouter
@app.post("/debug/clear_state")
def app_debug_clear_state(artifacts: bool = False) -> JSONResponse:  # pragma: no cover - passthrough
    summary = jobstore_clear_state(clear_artifacts=bool(artifacts))
    return JSONResponse({"status": "ok", **summary})


# --------------------------------------------------------
# Chat over FAISS (streaming retrieval preview via SSE)
# --------------------------------------------------------

def _sse(data: Dict[str, Any]) -> str:
    try:
        payload = json.dumps(data, ensure_ascii=False)
    except Exception:
        payload = "{}"
    return f"data: {payload}\n\n"


@router.get("/chat/stream", tags=["chat"])
def chat_stream(
    query: str,
    top_k: int = 5,
    index: str = Query("confluence_staging", description="Vector index name (e.g., confluence_staging or confluence)"),
) -> StreamingResponse:
    engine = RetrievalEngine()

    def _gen():
        yield _sse({"type": "init", "query": query, "top_k": top_k, "index": index})
        try:
            results = engine.search(index, query, top_k=top_k)
        except Exception as exc:  # noqa: BLE001
            yield _sse({"type": "error", "message": str(exc)})
            return
        count = 0
        for r in results:
            count += 1
            item = {
                "type": "chunk",
                "rank": count,
                "score": float(r.score),
                "text": r.text,
                "metadata": r.metadata,
            }
            yield _sse(item)
        yield _sse({"type": "done", "count": count})

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)

# Ensure newly added chat routes are mounted (router was included earlier as well)
app.include_router(router)


@app.get("/chat/qa/stream", tags=["chat"])
def chat_qa_stream(
    query: str,
    top_k: int = Query(7, ge=1, le=15),
    index: str = Query("confluence_staging"),
    confluence_host: str | None = Query(None, description="e.g., https://luxproject.luxoft.com/confluence"),
    max_tokens: int = Query(1000, ge=128, le=4000),
    temperature: float = Query(0.2, ge=0.0, le=1.0),
) -> StreamingResponse:
    """Stream an LLM-composed answer over top‑K FAISS retrieval with Confluence links.

    Notes:
    - This endpoint is for Q&A smoke testing of index builds; it is not used for assessments.
    - Output is streamed as SSE by chunking the final completion (no token streaming).
    """
    _CONF_LOG.info(f"@GET: /chat/qa/stream: {str(datetime.now().strftime('%Y-%m-%d %H:%M:%S'))}")

    retr = RetrievalEngine()
    results = retr.search(index, query, top_k=top_k)

    def build_url(pid: str | None) -> str:
        if not pid:
            return ""
        base = (confluence_host or "").strip()
        if base and not base.startswith("http"):
            base = f"https://{base}"
        path = f"/pages/viewpage.action?pageId={pid}"
        return f"{base}{path}" if base else path

    # Prepare context and links
    contexts: list[dict[str, str]] = []
    for r in results:
        md = r.metadata or {}
        pid = str(md.get("page_id") or "")
        title = md.get("page_title") or md.get("title") or "Confluence Page"
        url = build_url(pid)
        contexts.append({
            "title": str(title),
            "url": url,
            "text": r.text or "",
        })

    # Build prompt
    context_lines: list[str] = []
    for i, c in enumerate(contexts, start=1):
        context_lines.append(f"[{i}] Title: {c['title']}\nURL: {c['url']}\nExcerpt:\n{c['text']}")
    links_lines = [f"{i}. {c['title']} — {c['url']}" for i, c in enumerate(contexts, start=1)]

    system = (
        "You are a helpful assistant. Answer the user's question strictly using the provided Confluence excerpts. "
        "Synthesize a clear answer. After the answer, include a 'Links:' list with the numbered links."
    )
    user = (
        f"Question: {query}\n\n"
        f"Context (top-{top_k}):\n" + "\n\n".join(context_lines) + "\n\n"
        "Respond with a concise, complete answer. Then add:\nLinks:\n" + "\n".join(links_lines)
    )

    if settings.engine_backend == "llama.cpp":
        api_base = f"http://{settings.llama_host}:{settings.llama_port}"

        from qwen_llamaccpp import QwenLlamaCppAssessor

        assessor = QwenLlamaCppAssessor(
            api_base=api_base,
            model_id=str(settings.model_id),
            assessment_engine=_get_engine(),
        )
    else:
        from qwen_vllm import QwenVLLMAssessor

        assessor = QwenVLLMAssessor(
            model_id=settings.model_id,
            tokenizer_dir=Path(settings.tokenizer_dir) if settings.tokenizer_dir else None,
            tensor_parallel_size=settings.tensor_parallel_size,
            gpu_memory_utilization=settings.gpu_memory_utilization,
            max_model_len=settings.max_model_len,
            quantization=settings.quantization,
            **(settings.llm_extra_kwargs or {}),
        )
    llm = assessor._ensure_llm()  # reuse loaded engine
    params = assessor._create_freeform_params(max_tokens=int(max_tokens), temperature=float(temperature))
    prompt = assessor._build_freeform_prompt(system, user)

    # Generate once; stream by chunking output
    outputs = llm.generate([prompt], params)
    full = ""
    if outputs and outputs[0].outputs:
        full = (outputs[0].outputs[0].text or "").strip()

    def _gen():
        yield _sse({"type": "init", "query": query, "top_k": top_k, "index": index, "max_tokens": max_tokens})
        if not full:
            yield _sse({"type": "error", "message": "no output"})
            return
        # Stream in chunks of ~300 chars
        step = 300
        for i in range(0, len(full), step):
            yield _sse({"type": "token", "text": full[i:i+step]})
        yield _sse({"type": "done"})

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return StreamingResponse(_gen(), media_type="text/event-stream", headers=headers)


# --------------------------------------------------------
# Confluence Stage 2 — Backend endpoints (PAT, tree, pipeline)
# --------------------------------------------------------

class ConfluencePatRequest(BaseModel):
    confluence_host: str
    token: str
    root_page_id: str | None = None


class ConfluencePatSaveRequest(BaseModel):
    confluence_host: str
    token: str | None = None


class ConfluenceEvaluateRequest(BaseModel):
    space: str
    root_page_id: str
    workers: int | None = 10


class ConfluencePullRequest(BaseModel):
    space: str
    workers: int | None = 10


class ConfluenceBuildRequest(BaseModel):
    space: str


class ConfluenceActivateRequest(BaseModel):
    space: str


class ConfluenceSelectionRequest(BaseModel):
    space: str
    include_ids: list[str]
    exclude_ids: list[str] | None = None
    # Optional filters persisted alongside selection
    stop_labels: list[str] | None = None
    stop_words: list[str] | None = None


_TASKS: dict[str, dict[str, Any]] = {}


def _secrets_path() -> Path:
    p = APP_ROOT / "secrets" / "confluence.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _write_space_auth(space: str, host: str, token: str) -> Path:
    payload = {
        "servers": {"confluence": {"env": {"CONFLUENCE_HOST": host, "CONFLUENCE_API_TOKEN": token}}}
    }
    path = (CORPUS_ROOT / space / ".sync")
    path.mkdir(parents=True, exist_ok=True)
    auth_path = path / "auth.json"
    auth_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return auth_path


def _load_saved_pat() -> dict[str, str] | None:
    try:
        data = json.loads(_secrets_path().read_text(encoding="utf-8"))
        host = str(data.get("confluence_host") or "").strip()
        token = str(data.get("token") or "").strip()
        if host and token:
            return {"confluence_host": host, "token": token}
    except Exception:
        return None
    return None


@app.post("/confluence/pat/test", tags=["confluence"])
def confluence_pat_test(req: ConfluencePatRequest) -> Dict[str, Any]:
    host = req.confluence_host.strip()
    token = req.token.strip()
    try:
        # Lightweight DC test via REST: list spaces (limit 1)
        url = host.rstrip("/") + "/rest/api/space?limit=1"
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        ok = 200 <= resp.status_code < 300
        if not ok:
            return {"ok": False, "error": f"HTTP {resp.status_code}"}
        return {"ok": True}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


@app.get("/confluence/pat", tags=["confluence"])
def confluence_pat_get() -> Dict[str, Any]:
    saved = _load_saved_pat() or {}
    host = str(saved.get("confluence_host") or "").strip()
    has_token = bool(saved.get("token") or "")
    return {"ok": True, "confluence_host": host, "has_token": has_token}


@app.post("/confluence/pat/save", tags=["confluence"])
def confluence_pat_save(req: ConfluencePatSaveRequest) -> Dict[str, Any]:
    cur = _load_saved_pat() or {}
    host = req.confluence_host.strip()
    token = (req.token or "").strip()
    if not host:
        raise HTTPException(status_code=400, detail="confluence_host required")
    if not token:
        # keep existing token if not provided
        token = str(cur.get("token") or "").strip()
    data = {"confluence_host": host, "token": token}
    _secrets_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
    return {"ok": True}


@app.get("/confluence/tree", tags=["confluence"])
def confluence_tree(root_page_id: str | None = None, space: str | None = None, limit: int = 200) -> Dict[str, Any]:
    _CONF_LOG.info(f"GET /confluence/tree space={space}" + " root_page_id=%s", root_page_id)

    saved = _load_saved_pat()
    if not saved:
        raise HTTPException(status_code=400, detail="PAT not configured")
    host = saved["confluence_host"]
    token = saved["token"]
    try:
        _CONF_LOG.info("tree request space=%s root_page_id=%s limit=%s", space, root_page_id, limit)
        client = CfClient(url=host, token=token, cloud=False)
        rid = (root_page_id or "").strip()
        if not rid:
            # Resolve space home page id
            if not space:
                raise HTTPException(status_code=400, detail="Provide 'space' or 'root_page_id'")
            # Try direct space homepage
            try:
                sp = client._client.get_space(space, expand='homepage')  # type: ignore[attr-defined]
                rid = str(((sp or {}).get('homepage') or {}).get('id') or '').strip()
            except Exception:
                rid = ''
            # Fallback to 'Home' title lookup
            if not rid:
                try:
                    rid = str(client._client.get_page_id(space, 'Home'))  # type: ignore[attr-defined]
                except Exception:
                    rid = ''
            if not rid:
                raise HTTPException(status_code=404, detail=f"Unable to resolve homepage for space {space}")
        children = client.list_children(rid, limit=limit) or []
        nodes: list[dict[str, Any]] = []
        for c in children:
            pid = str(c.get("id") or "")
            title = str(c.get("title") or "")
            nodes.append({"id": pid, "title": title})
        _CONF_LOG.info("tree resolved rid=%s children=%d", rid, len(nodes))
        return {"ok": True, "root_page_id": rid, "children": nodes}
    except Exception as exc:  # noqa: BLE001
        _CONF_LOG.exception("tree error space=%s root=%s err=%s", space, root_page_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/confluence/tree/full", tags=["confluence"])
def confluence_tree_full(space: str) -> Dict[str, Any]:
    _CONF_LOG.info("GET /confluence/tree/full" + " request space=%s", space)

    sync_dir = CORPUS_ROOT / space / ".sync"
    tree_path = sync_dir / "tree.json"
    try:
        text = tree_path.read_text(encoding="utf-8")
        data = json.loads(text)
        nodes = data.get("nodes") or {}
        _CONF_LOG.info("tree_full space=%s root=%s nodes=%d size=%dB", space, data.get("root_page_id"), len(nodes), len(text.encode('utf-8')))
    except Exception:
        _CONF_LOG.warning("tree_full missing for space=%s", space)
        raise HTTPException(status_code=404, detail="Full tree not available; run Evaluate first")
    return {"ok": True, **data}


@app.get("/confluence/history", tags=["confluence"])
def confluence_history(space: str, cmd: str = "pull", lines: int = 200) -> Dict[str, Any]:
    _CONF_LOG.info("/confluence/history" + " request space=%s cmd=%s lines=%d", space, cmd, lines)

    """Return the tail of the detached runner log for the given command.
    cmd: one of evaluate|pull|build|activate.
    """
    safe_cmd = cmd if cmd in {"evaluate", "pull", "build", "activate"} else "pull"
    safe_space = re.sub(r"[^A-Za-z0-9._-]", "_", space)
    log_path = APP_ROOT / "logs" / f"confluence_{safe_cmd}_{safe_space}.log"
    try:
        content = log_path.read_text(encoding="utf-8").splitlines()
        tail = content[-max(1, int(lines)) :]
    except Exception:
        tail = []
    return {"ok": True, "cmd": safe_cmd, "space": space, "lines": tail}


@app.post("/confluence/selection", tags=["confluence"])
def confluence_selection(req: ConfluenceSelectionRequest) -> Dict[str, Any]:
    _CONF_LOG.info("/confluence/selection" + " request space=%s include_ids=%d exclude_ids=%d",
                   req.space, len(req.include_ids), len(req.exclude_ids or []))

    path = CORPUS_ROOT / req.space / ".sync"
    path.mkdir(parents=True, exist_ok=True)
    sel_path = path / "selection.json"
    sel = {"include_ids": req.include_ids, "exclude_ids": req.exclude_ids or []}
    if isinstance(req.stop_labels, list):
        sel["stop_labels"] = [str(x).strip() for x in req.stop_labels if str(x).strip()]
    if isinstance(req.stop_words, list):
        sel["stop_words"] = [str(x).strip() for x in req.stop_words if str(x).strip()]
    sel_path.write_text(json.dumps(sel, indent=2), encoding="utf-8")
    return {"ok": True}


@app.get("/confluence/selection", tags=["confluence"])
def confluence_selection_get(space: str) -> Dict[str, Any]:
    _CONF_LOG.info("/confluence/selection" + " get request space=%s", space)

    sel_path = CORPUS_ROOT / space / ".sync" / "selection.json"
    try:
        data = json.loads(sel_path.read_text(encoding="utf-8"))
        include_ids = list((data.get("include_ids") or []))
        exclude_ids = list((data.get("exclude_ids") or []))
        resp: Dict[str, Any] = {"ok": True, "include_ids": include_ids, "exclude_ids": exclude_ids}
        # Optional filters when present
        if isinstance(data.get("stop_labels"), list):
            resp["stop_labels"] = [str(x) for x in data.get("stop_labels")]
        if isinstance(data.get("stop_words"), list):
            resp["stop_words"] = [str(x) for x in data.get("stop_words")]
        return resp
    except Exception:
        return {"ok": True, "include_ids": [], "exclude_ids": []}


def _update_status(space: str, stage: str, extra: Dict[str, Any] | None = None) -> None:
    st = {"space": space, "stage": stage, "ts": time.time()}
    if extra:
        st.update(extra)
    out = APP_ROOT / "logs" / f"confluence_status_{space}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(st), encoding="utf-8")


def _run_task(space: str, fn, *args, **kwargs) -> None:  # noqa: ANN001
    try:
        _TASKS.setdefault(space, {})
        _update_status(space, kwargs.pop("__stage", "running"))
        fn(*args, **kwargs)
        _update_status(space, "idle")
    except SystemExit as exc:
        _update_status(space, "error", {"error": str(exc)})
    except Exception as exc:  # noqa: BLE001
        _update_status(space, "error", {"error": str(exc)})
    finally:
        try:
            _TASKS[space].pop("thread", None)
        except Exception:
            pass


@app.post("/confluence/evaluate", tags=["confluence"])
def confluence_evaluate(req: ConfluenceEvaluateRequest) -> Dict[str, Any]:
    _CONF_LOG.info("POST /confluence/evaluate >>")
    _CONF_LOG.info(req)

    saved = _load_saved_pat()
    if not saved:
        raise HTTPException(status_code=400, detail="PAT not configured")
    auth_path = _write_space_auth(req.space, saved["confluence_host"], saved["token"])
    # Derive the space homepage id when not provided
    rid = (req.root_page_id or "").strip()
    if not rid:
        try:
            client = CfClient(url=saved["confluence_host"], token=saved["token"], cloud=False)
            sp = client._client.get_space(req.space, expand='homepage')  # type: ignore[attr-defined]
            rid = str(((sp or {}).get('homepage') or {}).get('id') or '').strip()
        except Exception:
            rid = ''
        if not rid:
            try:
                rid = str(client._client.get_page_id(req.space, 'Home'))  # type: ignore[attr-defined]
            except Exception:
                rid = ''
        if not rid:
            raise HTTPException(status_code=404, detail=f"Unable to resolve homepage for space {req.space}")
    run_id = str(_uuid.uuid4())
    t = threading.Thread(
        target=_run_task,
        args=(req.space, cf_evaluate_remote, req.space, rid),
        kwargs={"auth_file": str(auth_path), "workers": int(req.workers or 4), "__stage": "evaluate"},
        daemon=True,
    )
    _TASKS.setdefault(req.space, {})["thread"] = t
    t.start()
    return {"ok": True, "run_id": run_id}


@app.post("/confluence/pull", tags=["confluence"])
def confluence_pull(req: ConfluencePullRequest) -> Dict[str, Any]:
    _CONF_LOG.info("POST/confluence/pull >>", req)

    saved = _load_saved_pat()
    if not saved:
        raise HTTPException(status_code=400, detail="PAT not configured")
    auth_path = _write_space_auth(req.space, saved["confluence_host"], saved["token"])
    run_id = str(_uuid.uuid4())
    t = threading.Thread(
        target=_run_task,
        args=(req.space, cf_pull, req.space),
        kwargs={"auth_file": str(auth_path), "workers": int(req.workers or 4), "__stage": "pull"},
        daemon=True,
    )
    _TASKS.setdefault(req.space, {})["thread"] = t
    t.start()
    return {"ok": True, "run_id": run_id}


@app.post("/confluence/build", tags=["confluence"])
def confluence_build(req: ConfluenceBuildRequest) -> Dict[str, Any]:
    _CONF_LOG.info("POST/confluence/build/" + req.space)

    run_id = str(_uuid.uuid4())
    t = threading.Thread(
        target=_run_task,
        args=(req.space, cf_build, req.space),
        kwargs={"__stage": "build"},
        daemon=True,
    )
    _TASKS.setdefault(req.space, {})["thread"] = t
    t.start()
    return {"ok": True, "run_id": run_id}


@app.post("/confluence/activate", tags=["confluence"])
def confluence_activate(req: ConfluenceActivateRequest) -> Dict[str, Any]:
    _CONF_LOG.info(req)
    run_id = str(_uuid.uuid4())
    t = threading.Thread(
        target=_run_task,
        args=(req.space, cf_activate, req.space),
        kwargs={"__stage": "activate"},
        daemon=True,
    )
    _TASKS.setdefault(req.space, {})["thread"] = t
    t.start()
    return {"ok": True, "run_id": run_id}


@app.get("/confluence/status", tags=["confluence"])
def confluence_status(space: str) -> Dict[str, Any]:
    timestamp = str(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    _CONF_LOG.info(f"GET: /confluence/status request: space={space} {timestamp}")

    # Compose status from state/plan + last status file
    sync_dir = CORPUS_ROOT / space / ".sync"
    plan = {}
    delta = {}
    try:
        plan = json.loads((sync_dir / "plan.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    try:
        delta = json.loads((sync_dir / "delta.json").read_text(encoding="utf-8"))
    except Exception:
        pass
    st_path = APP_ROOT / "logs" / f"confluence_status_{space}.json"
    raw_status: Dict[str, Any] = {}
    stage = "idle"
    try:
        raw_status = json.loads(st_path.read_text(encoding="utf-8")) or {}
        stage = (raw_status.get("stage") or "idle")
    except Exception:
        raw_status = {}
        # File is locked/unreadable, likely means a process is actively writing to it
        stage = "busy"

    # If no background task is currently tracked, treat stage as idle to avoid stale lockouts
    # (e.g., server restarts during an evaluate/pull/build/activate run leaving a non-idle status file).
    try:
        t = (_TASKS.get(space) or {}).get("thread")
        # Consider recent external status updates as in-progress even if thread not tracked
        recent = False
        try:
            ts_val = float(raw_status.get("ts")) if isinstance(raw_status, dict) else None
            if ts_val is not None and (time.time() - ts_val) < 10.0:
                recent = True
        except Exception:
            recent = False
        if not t or (hasattr(t, "is_alive") and not t.is_alive()):
            if not recent:
                stage = "idle"
    except Exception:
        stage = "idle"
    # Build response, preserving progress metadata when present
    resp: Dict[str, Any] = {
        "space": space,
        "stage": stage,
        "plan": plan,
        "delta": delta,
    }
    # Pass through progress fields from raw status
    progress = raw_status.get("progress") if isinstance(raw_status, dict) else None
    if isinstance(progress, dict):
        resp["progress"] = {"total": int(progress.get("total") or 0), "done": int(progress.get("done") or 0)}
        if progress.get("failed") is not None:
            try:
                resp["progress"]["failed"] = int(progress.get("failed") or 0)
            except Exception:
                pass
    if isinstance(raw_status, dict):
        if raw_status.get("started_at") is not None:
            try:
                started_at = float(raw_status.get("started_at"))
                resp["started_at"] = started_at
                resp["elapsed_sec"] = max(0.0, time.time() - started_at)
            except Exception:
                pass
        if raw_status.get("elapsed_sec") is not None:
            try:
                resp["elapsed_sec"] = float(raw_status.get("elapsed_sec"))
            except Exception:
                pass
        if raw_status.get("eta_sec") is not None:
            try:
                resp["eta_sec"] = float(raw_status.get("eta_sec"))
            except Exception:
                pass
        if raw_status.get("phase"):
            resp["phase"] = raw_status.get("phase")
    # Derive ETA when possible and not provided
    if resp.get("stage") == "evaluate" and "eta_sec" not in resp:
        try:
            prog = resp.get("progress") or {}
            done = int(prog.get("done") or 0)
            total = int(prog.get("total") or 0)
            elapsed = float(resp.get("elapsed_sec") or 0.0)
            if done > 0 and total > done and elapsed > 0:
                eta_val = (total - done) * (elapsed / done)
                resp["eta_sec"] = max(0.0, float(eta_val))
        except Exception:
            pass
    return resp


@app.get("/llm-server/status")
def llm_server_status() -> Dict[str, Any]:
    """Probe the LLM server (llama.cpp / transformers, default port 8002)
    and return ``{"llm_status": "online"|"offline"}``.

    The LLM server may be down — that's an *expected* state. Any failure
    (connection refused, timeout, non-2xx) maps to ``offline`` cleanly so
    the browser-side indicator can poll this endpoint and trust the JSON,
    not have to interpret HTTP 5xx.
    """
    timestamp = str(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    url = f"http://{settings.llama_host}:{settings.llama_port}/status"
    try:
        req_llm = requests.get(url, timeout=3)
    except requests.exceptions.RequestException as exc:
        _CONF_LOG.info(f"GET: /llm-server/status: unreachable ({type(exc).__name__}), {timestamp}")
        return {"llm_status": "offline"}
    if req_llm.ok:
        _CONF_LOG.info(f"GET: /llm-server/status: LLM_SERVER.status={req_llm.status_code} (OK), {timestamp}")
        return {"llm_status": "online"}
    _CONF_LOG.info(f"GET: /llm-server/status: LLM_SERVER.status={req_llm.status_code}:{req_llm.text[:200]}, {timestamp}")
    return {"llm_status": "offline"}
