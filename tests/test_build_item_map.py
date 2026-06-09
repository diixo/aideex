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

    def test_upstream_from_custom_field_tracker_item_reference(self):
        """TrackerItemReference custom field values are treated as upstream."""
        t = make_tracker(1)
        item_with_cf = {
            "id": 101,
            "name": "Item-101",
            "status": None,
            "subjects": [],
            "typeName": "Requirement",
            "customFields": [
                {
                    "type": "TrackerItemReference",
                    "name": "Derived From",
                    "values": [
                        {"id": 200, "name": "Item-200", "type": "TrackerItemReference"},
                        {"id": 201, "name": "Item-201", "type": "TrackerItemReference"},
                    ],
                }
            ],
        }
        items = [item_with_cf, make_item(200), make_item(201)]
        payload = make_payload([(t, [101, 200, 201])], items)
        m = _build_item_map(payload)
        assert set(m[101]["upstream"]) == {200, 201}
        assert 101 in m[200]["downstream"]
        assert 101 in m[201]["downstream"]

    def test_upstream_subjects_and_custom_field_combined(self):
        """Both subjects and TrackerItemReference custom fields contribute to upstream."""
        t = make_tracker(1)
        item = {
            "id": 101,
            "name": "Item-101",
            "status": None,
            "subjects": [{"id": 200, "name": "Item-200", "type": "TrackerItemReference"}],
            "typeName": "Requirement",
            "customFields": [
                {
                    "type": "TrackerItemReference",
                    "name": "Derived From",
                    "values": [{"id": 201, "name": "Item-201", "type": "TrackerItemReference"}],
                }
            ],
        }
        items = [item, make_item(200), make_item(201)]
        payload = make_payload([(t, [101, 200, 201])], items)
        m = _build_item_map(payload)
        assert set(m[101]["upstream"]) == {200, 201}

    def test_non_tracker_item_reference_custom_field_ignored(self):
        """Custom fields of other types do not affect upstream."""
        t = make_tracker(1)
        item = {
            "id": 101,
            "name": "Item-101",
            "status": None,
            "subjects": [],
            "typeName": "Requirement",
            "customFields": [
                {"type": "TextFieldValue", "name": "Notes", "values": ["some text"]},
                {"type": "ChoiceFieldValue", "name": "Priority", "values": [{"id": 999}]},
            ],
        }
        items = [item, make_item(999)]
        payload = make_payload([(t, [101, 999])], items)
        m = _build_item_map(payload)
        assert m[101]["upstream"] == []


# ── integration: live corpus project_id=10 ───────────────────────────────────

import json
import os
import pytest

CORPUS_PATH = os.path.join(os.path.dirname(__file__), "..", "corpus", "10", "codebeamer.json")

@pytest.mark.skipif(not os.path.exists(CORPUS_PATH), reason="corpus/10 not available")
class TestBuildItemMapLiveCorpus:

    @pytest.fixture(scope="class")
    def item_map(self):
        with open(CORPUS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        from backend.codebeamer_router import _build_item_map
        return _build_item_map(data)

    def test_map_non_empty(self, item_map):
        assert len(item_map) > 0

    def test_all_entries_have_required_keys(self, item_map):
        for iid, entry in item_map.items():
            assert "id" in entry
            assert "name" in entry
            assert "upstream" in entry
            assert "downstream" in entry

    def test_subjects_become_upstream(self, item_map):
        """Items with subjects must have non-empty upstream and those upstreams
        must appear in the map (or at least some of them for cross-tracker refs)."""
        items_with_upstream = [e for e in item_map.values() if e["upstream"]]
        assert len(items_with_upstream) > 0

    def test_downstream_is_inverse_of_upstream(self, item_map):
        """For every upstream link A→B, B must list A in its downstream."""
        errors = []
        for iid, entry in item_map.items():
            for uid in entry["upstream"]:
                if uid in item_map and iid not in item_map[uid]["downstream"]:
                    errors.append((iid, uid))
        assert errors == [], f"downstream/upstream mismatch: {errors[:5]}"

    def test_known_item_302494_has_upstream_288168(self, item_map):
        """Item 302494 has subject 288168 in corpus — must appear as upstream."""
        if 302494 not in item_map:
            pytest.skip("item 302494 not in map")
        assert 288168 in item_map[302494]["upstream"]


if __name__ == "__main__":
    with open(CORPUS_PATH, encoding="utf-8") as f:
        data = json.load(f)
    from backend.codebeamer_router import _build_item_map
    item_map = _build_item_map(data)

    suite = TestBuildItemMapLiveCorpus()
    suite.test_map_non_empty(item_map)
    suite.test_all_entries_have_required_keys(item_map)
    suite.test_subjects_become_upstream(item_map)
    suite.test_downstream_is_inverse_of_upstream(item_map)
    suite.test_known_item_302494_has_upstream_288168(item_map)
    print(f"All tests passed. item_map size: {len(item_map)}")
