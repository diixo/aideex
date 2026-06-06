"""Integration test for _cb_pull against a real Codebeamer project.

Usage:
    python tests/integration_cb_pull.py [project_id]

If project_id is omitted, reads it from secrets/codebeamer.json.
Credentials are always read from secrets/codebeamer.json.
"""
import sys
import os
import json
import time
import tempfile
import logging
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backend.codebeamer_router as router
from backend.codebeamer_router import _cb_pull, _CB_ITEM_MAPS, _load_saved_pswd

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")


def _check(condition, msg):
    status = "OK  " if condition else "FAIL"
    print(f"  [{status}] {msg}")
    return condition


def run(project_id: int, saved: dict, workers: int = 4):
    print(f"\n=== integration_cb_pull project_id={project_id} workers={workers} ===\n")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Patch CORPUS_ROOT so pull writes to temp dir; _update_cb_status is a no-op
        with patch.object(router, "CORPUS_ROOT", tmp_path), \
             patch.object(router, "_update_cb_status"):

            t0 = time.time()
            try:
                _cb_pull(project_id, saved, workers=workers)
            except SystemExit as e:
                print(f"ABORT: {e}")
                return False
            elapsed = time.time() - t0

        corpus_path = tmp_path / str(project_id) / "codebeamer.json"
        if not corpus_path.exists():
            print("FAIL: codebeamer.json was not written")
            return False

        data = json.loads(corpus_path.read_text(encoding="utf-8"))

    print(f"Pull completed in {elapsed:.1f}s\n")

    ok = True

    # ── structure checks ──────────────────────────────────────────────────────
    print("Structure:")
    ok &= _check(data.get("project_id") == project_id,    "project_id matches")
    ok &= _check(isinstance(data.get("trackers"), list),   "trackers is list")
    ok &= _check(isinstance(data.get("items"), list),      "items is list")
    ok &= _check(isinstance(data.get("tracker_fields"), dict), "tracker_fields is dict")

    trackers = data.get("trackers", [])
    items    = data.get("items", [])
    fields   = data.get("tracker_fields", {})

    print(f"\nCounts:")
    print(f"  trackers      : {len(trackers)}")
    print(f"  items (full)  : {len(items)}")
    print(f"  tracker_fields: {len(fields)} trackers")

    # ── tracker refs ──────────────────────────────────────────────────────────
    print("\nTracker item refs (lightweight):")
    for t in trackers[:3]:
        refs = t.get("items", [])
        bad  = [r for r in refs if set(r.keys()) - {"id", "name"}]
        ok  &= _check(not bad, f"  tracker {t['id']} ({t.get('name','')[:30]}): {len(refs)} refs, no extra keys")
    if len(trackers) > 3:
        print(f"  ... ({len(trackers) - 3} more trackers not shown)")

    # ── full items ────────────────────────────────────────────────────────────
    print("\nFull items sample (first 3):")
    for it in items[:3]:
        has_status = "status" in it
        ok &= _check(has_status, f"  item {it.get('id')} ({str(it.get('name',''))[:30]}): has status field")

    # ── tracker_fields ────────────────────────────────────────────────────────
    print("\nTracker fields sample (first 2 trackers):")
    for tid, raw_fields in list(fields.items())[:2]:
        ok &= _check(isinstance(raw_fields, list), f"  tracker {tid}: fields is list ({len(raw_fields)} fields)")

    # ── item map in memory ────────────────────────────────────────────────────
    # item map is built in _CB_ITEM_MAPS but we patched CORPUS_ROOT,
    # so the map key uses real project_id — check it was populated
    print("\nItem map:")
    ok &= _check(project_id in _CB_ITEM_MAPS, "item map built in _CB_ITEM_MAPS")
    if project_id in _CB_ITEM_MAPS:
        ok &= _check(len(_CB_ITEM_MAPS[project_id]) > 0, f"item map non-empty ({len(_CB_ITEM_MAPS[project_id])} entries)")

    print(f"\n{'ALL CHECKS PASSED' if ok else 'SOME CHECKS FAILED'}")
    return ok


if __name__ == "__main__":
    saved = _load_saved_pswd()
    if not saved:
        print("ERROR: no credentials in secrets/codebeamer.json")
        sys.exit(1)

    # if len(sys.argv) > 1:
    #     project_id = int(sys.argv[1])
    # elif saved.get("project_id"):
    #     project_id = int(saved["project_id"])
    # else:
    #     print("ERROR: provide project_id as argument or set it in secrets/codebeamer.json")
    #     sys.exit(1)

    project_id = 25

    ok = run(project_id, saved, workers=1)
    sys.exit(0 if ok else 1)
