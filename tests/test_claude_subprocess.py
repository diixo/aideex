import sys
import os
import json
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backend.codebeamer_router import (
    _claude_argv,
    _classify_trackers_claude,
)


# ── _claude_argv ──────────────────────────────────────────────────────────────

def test_claude_argv_windows():
    with patch("backend.codebeamer_router.sys") as mock_sys:
        mock_sys.platform = "win32"
        argv, stdin_data = _claude_argv("hello")
    assert argv == ["cmd", "/c", "claude"]
    assert stdin_data == "hello"


def test_claude_argv_linux():
    with patch("backend.codebeamer_router.sys") as mock_sys:
        mock_sys.platform = "linux"
        argv, stdin_data = _claude_argv("hello")
    assert argv == ["claude"]
    assert stdin_data == "hello"


# ── _classify_trackers_claude ─────────────────────────────────────────────────

TRACKERS = [
    {"id": 100, "name": "System Integration Tests", "type": {"name": "TestCase"}},
    {"id": 101, "name": "Acceptance Tests",         "type": {"name": "TestCase"}},
    {"id": 102, "name": "Requirements",             "type": {"name": "Requirement"}},
]


def _mock_run(stdout, returncode=0):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = ""
    return m


def test_classify_trackers_returns_mapping():
    response = json.dumps({"100": "SYS.4", "101": "SYS.5", "102": "UNMAPPED"})
    with patch("backend.codebeamer_router.subprocess.run", return_value=_mock_run(response)):
        result = _classify_trackers_claude(TRACKERS)
    assert result == {100: "SYS.4", 101: "SYS.5", 102: "UNMAPPED"}


def test_classify_trackers_extracts_json_from_prose():
    """Claude sometimes wraps JSON in explanation text — still extracted correctly."""
    response = 'Here is the mapping:\n{"100": "SYS.4", "101": "SYS.5"}\nDone.'
    with patch("backend.codebeamer_router.subprocess.run", return_value=_mock_run(response)):
        result = _classify_trackers_claude(TRACKERS)
    assert result[100] == "SYS.4"
    assert result[101] == "SYS.5"


def test_classify_trackers_nonzero_exit_returns_empty():
    with patch("backend.codebeamer_router.subprocess.run", return_value=_mock_run("", returncode=1)):
        result = _classify_trackers_claude(TRACKERS)
    assert result == {}


def test_classify_trackers_no_json_returns_empty():
    with patch("backend.codebeamer_router.subprocess.run", return_value=_mock_run("Sorry, I cannot help.")):
        result = _classify_trackers_claude(TRACKERS)
    assert result == {}


def test_classify_trackers_subprocess_exception_returns_empty():
    with patch("backend.codebeamer_router.subprocess.run", side_effect=FileNotFoundError("not found")):
        result = _classify_trackers_claude(TRACKERS)
    assert result == {}


def test_classify_trackers_empty_input_returns_empty():
    result = _classify_trackers_claude([])
    assert result == {}
