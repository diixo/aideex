import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from backend.codebeamer_router import _build_item_map


# ── helpers ───────────────────────────────────────────────────────────────────

def make_tracker(tid, type_id=1, color="#dc3545", item_ids=None):
    """Tracker with lightweight item refs — mirrors GET /trackers/{id}/items."""
    return {
        "id": tid,
        "name": f"Tracker-{tid}",
        "type": {"id": type_id, "name": "Issue", "type": "TrackerTypeReference"},
        "color": color,
        "keyName": f"T{tid}",
        "items": [{"id": iid, "name": f"Item-{iid}"} for iid in (item_ids or [])],
    }


def make_item(iid, name="", status_name=None, subjects=None):
    """Full TrackerItem response — mirrors GET /items/{id}."""
    status = {"id": 1, "name": status_name, "type": "ChoiceOptionReference"} if status_name else None
    return {
        "id": iid,
        "name": name or f"Item-{iid}",
        "status": status,
        "subjects": [
            {"id": sid, "name": f"Item-{sid}", "type": "TrackerItemReference"}
            for sid in (subjects or [])
        ],
        "typeName": "Functional",
    }


def make_payload(tracker_defs, items):
    """
    tracker_defs: list of (tracker_dict, [item_ids])
    items:        list of full item objects (top-level)
    """
    trackers = []
    for t, iids in tracker_defs:
        t = dict(t)
        t["items"] = [{"id": iid, "name": f"Item-{iid}"} for iid in iids]
        trackers.append(t)
    return {"trackers": trackers, "items": items}


# ── tests ─────────────────────────────────────────────────────────────────────

class TestBuildItemMap:

    def test_empty_payload(self):
        result = _build_item_map({"trackers": [], "items": []})
        assert result == {}

    def test_single_item_no_status(self):
        t = make_tracker(1)
        item = make_item(101)
        payload = make_payload([(t, [101])], [item])
        m = _build_item_map(payload)
        assert 101 in m
        assert m[101]["name"] == "Item-101"
        assert m[101]["upstream"] == []
        assert m[101]["downstream"] == []

    def test_item_ref_without_full_object_is_skipped(self):
        """Tracker refs an item but top-level items array has no matching object."""
        t = make_tracker(1)
        payload = make_payload([(t, [101])], [])  # no full item object
        m = _build_item_map(payload)
        assert m == {}

    def test_status_color_new(self):
        t = make_tracker(1, color="#aaa")
        item = make_item(101, status_name="New")
        payload = make_payload([(t, [101])], [item])
        m = _build_item_map(payload)
        assert m[101]["color"] == "#dc3545"  # STATUS_COLORS["New"]

    def test_status_color_resolved(self):
        t = make_tracker(1)
        item = make_item(101, status_name="Resolved")
        payload = make_payload([(t, [101])], [item])
        m = _build_item_map(payload)
        assert m[101]["color"] == "#198754"

    def test_unknown_status_fallback_to_tracker_color(self):
        t = make_tracker(1, color="#123456")
        item = make_item(101, status_name="SomeUnknownStatus")
        payload = make_payload([(t, [101])], [item])
        m = _build_item_map(payload)
        assert m[101]["color"] == "#123456"

    def test_no_status_fallback_to_tracker_color(self):
        t = make_tracker(1, color="#abcdef")
        item = make_item(101, status_name=None)
        payload = make_payload([(t, [101])], [item])
        m = _build_item_map(payload)
        assert m[101]["color"] == "#abcdef"

    def test_upstream_from_subjects(self):
        t = make_tracker(1)
        items = [make_item(101, subjects=[200, 201]), make_item(200), make_item(201)]
        payload = make_payload([(t, [101, 200, 201])], items)
        m = _build_item_map(payload)
        assert set(m[101]["upstream"]) == {200, 201}

    def test_downstream_built_as_inverse_of_upstream(self):
        t = make_tracker(1)
        items = [make_item(101, subjects=[200]), make_item(200)]
        payload = make_payload([(t, [101, 200])], items)
        m = _build_item_map(payload)
        assert m[101]["upstream"] == [200]
        assert 101 in m[200]["downstream"]

    def test_multiple_trackers(self):
        t1 = make_tracker(1)
        t2 = make_tracker(2)
        items = [make_item(101), make_item(102), make_item(103)]
        payload = make_payload([(t1, [101]), (t2, [102, 103])], items)
        m = _build_item_map(payload)
        assert set(m.keys()) == {101, 102, 103}

    def test_tracker_icon_by_type_id(self):
        t = make_tracker(1, type_id=1)
        item = make_item(101)
        payload = make_payload([(t, [101])], [item])
        m = _build_item_map(payload)
        assert m[101]["icon"] == "fireplace"  # TRACKER_ICONS[1]

    def test_unknown_type_id_fallback_icon(self):
        t = make_tracker(1, type_id=9999)
        item = make_item(101)
        payload = make_payload([(t, [101])], [item])
        m = _build_item_map(payload)
        assert m[101]["icon"] == "label"

    def test_item_ref_missing_id_skipped(self):
        t = make_tracker(1)
        t["items"] = [{"name": "no id"}]  # ref without id
        payload = {"trackers": [t], "items": [make_item(101)]}
        m = _build_item_map(payload)
        assert m == {}

    def test_cross_tracker_downstream(self):
        """Item in tracker 2 references item in tracker 1 via subjects."""
        t1 = make_tracker(1)
        t2 = make_tracker(2)
        items = [make_item(100), make_item(200, subjects=[100])]
        payload = make_payload([(t1, [100]), (t2, [200])], items)
        m = _build_item_map(payload)
        assert 200 in m[100]["downstream"]
        assert m[200]["upstream"] == [100]
