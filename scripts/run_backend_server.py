"""Run the MAN.3 assessment FastAPI backend with file-based logging."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import sys

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.server import app


LOG_PATH = Path("logs/backend_server.log")
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

# Truncate previous log content so each launch starts fresh.
LOG_PATH.write_text("", encoding="utf-8")

# Also reset assessor manager JSON log to avoid mixed historical entries.
ASSESSOR_LOG = Path("logs/assessor_manager.log")
ASSESSOR_LOG.parent.mkdir(parents=True, exist_ok=True)
ASSESSOR_LOG.write_text("", encoding="utf-8")

TIMESTAMP_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
# Configure root logger to write to file with timestamps.
file_handler = console_handler = logging.StreamHandler()
file_handler.setFormatter(logging.Formatter(TIMESTAMP_FMT))

def _resolve_level(env_value: str | None) -> tuple[int, str]:
    candidate = (env_value or "INFO").strip().upper() or "INFO"
    level = logging.getLevelName(candidate)
    if isinstance(level, str):
        return logging.INFO, "INFO"
    return level, candidate


LOG_LEVEL, LOG_LEVEL_NAME = _resolve_level(os.environ.get("LOG_LEVEL"))
root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL)
root_logger.handlers = [file_handler]

log_config = {
    "version": 1,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(asctime)s %(levelprefix)s %(message)s",
            "use_colors": False,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": "%(asctime)s %(levelprefix)s %(client_addr)s - \"%(request_line)s\" %(status_code)s",
            "use_colors": False,
        },
    },
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "default",
        },
        "access": {
            "class": "logging.StreamHandler",
            "formatter": "access",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": LOG_LEVEL_NAME},
        "uvicorn.error": {"handlers": ["default"], "level": LOG_LEVEL_NAME, "propagate": False},
        "uvicorn.access": {"handlers": ["access"], "level": LOG_LEVEL_NAME, "propagate": False},
        "backend": {"handlers": ["default"], "level": LOG_LEVEL_NAME, "propagate": False},
        "backend.aggregate": {"handlers": ["default"], "level": LOG_LEVEL_NAME, "propagate": False},
    },
}

ACCESS_LOG = (os.environ.get("ACCESS_LOG", "0") or "0").strip().lower() in {"1", "true", "yes", "on"}
raw_excludes = os.environ.get("ACCESS_LOG_EXCLUDE_PATHS", "")
ACCESS_EXCLUDE_PATHS = [p.strip() for p in raw_excludes.split(",") if p.strip()]

if ACCESS_LOG and ACCESS_EXCLUDE_PATHS:
    log_config.setdefault("filters", {})
    log_config["filters"]["access_path_exclude"] = {
        "()": "backend.logging_utils.AccessPathExclusionFilter",
        "paths": ACCESS_EXCLUDE_PATHS,
    }
    log_config["handlers"]["access"].setdefault("filters", [])
    log_config["handlers"]["access"]["filters"].append("access_path_exclude")

if not ACCESS_LOG:
    log_config["loggers"]["uvicorn.access"]["handlers"] = []


def _configure_vllm_logger() -> None:
    """Ensure vLLM loggers honor our level and plain formatter."""
    plain_formatter = logging.Formatter(TIMESTAMP_FMT)
    for name in ("vllm", "vllm.engine", "vllm.distributed"):
        logger = logging.getLogger(name)
        logger.setLevel(LOG_LEVEL)
        logger.propagate = True
        for handler in list(logger.handlers):
            handler.setLevel(LOG_LEVEL)
            handler.setFormatter(plain_formatter)


if __name__ == "__main__":
    _configure_vllm_logger()
    config = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=8001,
        log_config=log_config,
        log_level=LOG_LEVEL_NAME.lower(),
        access_log=ACCESS_LOG,
        reload=False,
    )
    server = uvicorn.Server(config)
    server.run()
