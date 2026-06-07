import sys
import os
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import backend.codebeamer_router as router
from backend.codebeamer_router import _cb_pull, _CB_ITEM_MAPS

PROJECT_ID = 99
SAVED = {"codebeamer_host": "http://cb.test", "username": "u", "pswd": "p"}


# ── fixtures ──────────────────────────────────────────────────────────────────

def _tree(project_id=PROJECT_ID, trackers=None):
    """Build tree.json structure. trackers: list of {id, name, items}."""
    tracker_list = trackers or [
        {"id": 1, "name": "T1", "items": [{"id": 101, "name": "I1"}, {"id": 102, "name": "I2"}]},
    ]
    return {
        "project_id":   project_id,
        "project_name": "TestProject",
        "tree": [
            {"trackerId": t["id"], "text": t["name"], "items": t.get("items", [])}
            for t in tracker_list
        ],
    }


def _raw_tracker(tid=1):
    return {
        "id": tid, "name": f"Tracker-{tid}",
        "type": {"id": 1, "name": "Requirement", "type": "TrackerTypeReference"},
        "color": "#abc123", "keyName": f"T{tid}",
    }


def _field_ref(fid, name):
    return {"id": fid, "name": name, "type": "FieldReference"}


def _full_field(fid, name, hidden=False):
    return {
        "id": fid, "name": name, "type": "TextFieldValue",
        "hidden": hidden, "multipleValues": False,
        "options": [], "trackerItemField": name.lower(),
    }


def _raw_item(iid):
    return {
        "id": iid, "name": f"Item-{iid}",
        "status": {"id": 1, "name": "New", "type": "ChoiceOptionReference"},
        "subjects": [], "typeName": "Functional",
    }


def _make_cb_mock():
    """Returns a mock CB client that routes get() by URL."""
    def get(url, **kw):
        if url == "trackers/1":               return _raw_tracker(1)
        if url == "trackers/1/fields":        return [_field_ref(10, "Summary"), _field_ref(11, "Status")]
        if url == "trackers/1/fields/10":     return _full_field(10, "Summary")
        if url == "trackers/1/fields/11":     return _full_field(11, "Status")
        if url == "items/101":                return _raw_item(101)
        if url == "items/102":                return _raw_item(102)
        return {}
    cb = MagicMock()
    cb._client.get.side_effect = get
    return cb


# ── helper ────────────────────────────────────────────────────────────────────

def run_pull(tmp_path, tree_data=None, cb_mock=None):
    """Write tree.json, run _cb_pull, return parsed codebeamer.json."""
    sync_dir = Path(tmp_path) / str(PROJECT_ID) / ".sync"
    sync_dir.mkdir(parents=True)
    (sync_dir / "tree.json").write_text(
        json.dumps(tree_data or _tree()), encoding="utf-8"
    )
    if cb_mock is None:
        cb_mock = _make_cb_mock()
    with patch.object(router, "CORPUS_ROOT", Path(tmp_path)), \
         patch.object(router, "_make_client", return_value=cb_mock), \
         patch.object(router, "_update_cb_status"):
        _cb_pull(PROJECT_ID, SAVED, workers=2)
    corpus = Path(tmp_path) / str(PROJECT_ID) / "codebeamer.json"
    return json.loads(corpus.read_text(encoding="utf-8"))


# ── tests ─────────────────────────────────────────────────────────────────────

class TestCbPull:

    def test_writes_codebeamer_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = run_pull(tmp)
        assert data["project_id"] == PROJECT_ID
        assert data["project_name"] == "TestProject"
        assert "trackers" in data
        assert "items" in data
        assert "tracker_fields" in data

    def test_tracker_items_are_lightweight_refs(self):
        """trackers[i].items must only contain {id, name} — not full objects."""
        with tempfile.TemporaryDirectory() as tmp:
            data = run_pull(tmp)
        refs = data["trackers"][0]["items"]
        assert len(refs) == 2
        for ref in refs:
            assert set(ref.keys()) == {"id", "name"}

    def test_top_level_items_are_full_objects(self):
        """top-level items array must contain full CB item responses."""
        with tempfile.TemporaryDirectory() as tmp:
            data = run_pull(tmp)
        ids = {it["id"] for it in data["items"]}
        assert ids == {101, 102}
        item = next(it for it in data["items"] if it["id"] == 101)
        assert "status" in item
        assert "subjects" in item

    def test_tracker_fields_stored_by_tracker_id(self):
        """tracker_fields keyed by str(tracker_id) contains fetched field dicts."""
        with tempfile.TemporaryDirectory() as tmp:
            data = run_pull(tmp)
        fields = data["tracker_fields"].get("1")
        assert fields is not None
        field_ids = {f["id"] for f in fields}
        assert field_ids == {10, 11}

    def test_fields_fetched_individually_per_id(self):
        """Each field ref triggers a separate GET /trackers/{id}/fields/{fid}."""
        cb = _make_cb_mock()
        with tempfile.TemporaryDirectory() as tmp:
            run_pull(tmp, cb_mock=cb)
        urls = [str(c.args[0]) for c in cb._client.get.call_args_list]
        assert "trackers/1/fields/10" in urls
        assert "trackers/1/fields/11" in urls

    def test_item_map_built_in_memory(self):
        with tempfile.TemporaryDirectory() as tmp:
            run_pull(tmp)
        assert PROJECT_ID in _CB_ITEM_MAPS
        assert 101 in _CB_ITEM_MAPS[PROJECT_ID]
        assert 102 in _CB_ITEM_MAPS[PROJECT_ID]

    def test_missing_tree_auto_evaluates(self):
        """If tree.json is absent, _cb_pull runs evaluate automatically."""
        cb = _make_cb_mock()
        evaluate_called = []

        def fake_evaluate(pid, saved, workers, _stage="evaluate"):
            evaluate_called.append(pid)
            # write tree.json as evaluate would
            sync_dir = Path(router.CORPUS_ROOT) / str(pid) / ".sync"
            sync_dir.mkdir(parents=True, exist_ok=True)
            (sync_dir / "tree.json").write_text(json.dumps(_tree(pid)), encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / str(PROJECT_ID) / ".sync").mkdir(parents=True)
            with patch.object(router, "CORPUS_ROOT", Path(tmp)), \
                 patch.object(router, "_make_client", return_value=cb), \
                 patch.object(router, "_update_cb_status"), \
                 patch.object(router, "_cb_evaluate", side_effect=fake_evaluate):
                _cb_pull(PROJECT_ID, SAVED, workers=1)

        assert evaluate_called == [PROJECT_ID]

    def test_stale_tree_auto_evaluates(self):
        """If tree.json has wrong project_id, _cb_pull reruns evaluate."""
        cb = _make_cb_mock()
        evaluate_called = []

        def fake_evaluate(pid, saved, workers, _stage="evaluate"):
            evaluate_called.append(pid)
            sync_dir = Path(router.CORPUS_ROOT) / str(pid) / ".sync"
            sync_dir.mkdir(parents=True, exist_ok=True)
            (sync_dir / "tree.json").write_text(json.dumps(_tree(pid)), encoding="utf-8")

        with tempfile.TemporaryDirectory() as tmp:
            sync_dir = Path(tmp) / str(PROJECT_ID) / ".sync"
            sync_dir.mkdir(parents=True)
            (sync_dir / "tree.json").write_text(json.dumps(_tree(project_id=PROJECT_ID + 1)), encoding="utf-8")
            with patch.object(router, "CORPUS_ROOT", Path(tmp)), \
                 patch.object(router, "_make_client", return_value=cb), \
                 patch.object(router, "_update_cb_status"), \
                 patch.object(router, "_cb_evaluate", side_effect=fake_evaluate):
                _cb_pull(PROJECT_ID, SAVED, workers=1)

        assert evaluate_called == [PROJECT_ID]

    def test_multiple_trackers(self):
        """Items and fields from all trackers end up in the output."""
        tree = _tree(trackers=[
            {"id": 1, "name": "T1", "items": [{"id": 101, "name": "I1"}]},
            {"id": 2, "name": "T2", "items": [{"id": 201, "name": "I2"}]},
        ])

        def get(url, **kw):
            if url == "trackers/1":           return _raw_tracker(1)
            if url == "trackers/2":           return _raw_tracker(2)
            if url in ("trackers/1/fields", "trackers/2/fields"): return []
            if url == "items/101":            return _raw_item(101)
            if url == "items/201":            return _raw_item(201)
            return {}
        cb = MagicMock(); cb._client.get.side_effect = get

        with tempfile.TemporaryDirectory() as tmp:
            data = run_pull(tmp, tree_data=tree, cb_mock=cb)

        assert len(data["trackers"]) == 2
        assert {it["id"] for it in data["items"]} == {101, 201}
        assert "1" in data["tracker_fields"]
        assert "2" in data["tracker_fields"]

    def test_failed_tracker_fetch_uses_fallback(self):
        """If GET /trackers/{id} fails, pull continues with fallback tracker data."""
        def get(url, **kw):
            if url == "trackers/1":           raise ConnectionError("timeout")
            if url == "trackers/1/fields":    return []
            if url == "items/101":            return _raw_item(101)
            if url == "items/102":            return _raw_item(102)
            return {}
        cb = MagicMock(); cb._client.get.side_effect = get

        with tempfile.TemporaryDirectory() as tmp:
            data = run_pull(tmp, cb_mock=cb)

        assert len(data["trackers"]) == 1
        assert data["trackers"][0]["id"] == 1
        assert {it["id"] for it in data["items"]} == {101, 102}
