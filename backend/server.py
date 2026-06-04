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
import requests

from fastapi import APIRouter, Body, FastAPI, HTTPException, Response, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from datetime import datetime

from .codebeamer_router import codebeamer_router

# Ensure 'scripts/' is importable for Stage 1 CLI wrappers
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = APP_ROOT / "data"

print("_ROOT:", _ROOT, "APP_ROOT:", APP_ROOT, "DATA_ROOT:", DATA_ROOT)

_LOGGER = logging.getLogger("backend.confluence")

router = APIRouter()

class HealthResponse(BaseModel):
    status: Literal["ok"]


@router.get("/health", response_model=HealthResponse, tags=["system"])
def healthcheck() -> HealthResponse:
    return HealthResponse(status="ok")


app = FastAPI(title="Server-Backend", version="0.0.0")
app.include_router(router)

app.include_router(codebeamer_router)

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

