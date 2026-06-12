import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from collections import defaultdict
import pytest
from backend.codebeamer_router import _tracker_process_area, _item_status_name


# ── helpers ───────────────────────────────────────────────────────────────────

def _apply_type_override(t_type: str, tracker_id: int = 99, tracker_name: str = "Unknown") -> str:
    """Mirrors the inline override in codebeamer_assessment_test_coverage."""
    area = _tracker_process_area(tracker_id, tracker_name)
    if t_type.lower() in ("testrun", "testset", "testconf"):
        area = "SYS.5"
    return area


def _compute_testcase_ids(item_tracker, testcase_tracker_ids, folder_ids, item_by_id):
    """Mirrors draft+folder exclusion logic from codebeamer_assessment_test_coverage."""
    draft_ids = {
        iid for iid, tid in item_tracker.items()
        if tid in testcase_tracker_ids
        and _item_status_name(item_by_id.get(iid, {})).lower() == "draft"
    }
    return (
        {iid for iid, tid in item_tracker.items() if tid in testcase_tracker_ids}
        - folder_ids
        - draft_ids
    )


# ── _tracker_process_area ─────────────────────────────────────────────────────

class TestTrackerProcessArea:

    def test_acceptance_maps_to_sys5(self):
        assert _tracker_process_area(99, "Acceptance Testing") == "SYS.5"

    def test_system_verification_maps_to_sys5(self):
        assert _tracker_process_area(99, "System Verification Test Cases") == "SYS.5"

    def test_system_integration_maps_to_sys4(self):
        assert _tracker_process_area(99, "System Integration Test Cases") == "SYS.4"

    def test_software_integration_maps_to_swe5(self):
        assert _tracker_process_area(99, "Software Integration Verification") == "SWE.5"

    def test_component_verification_maps_to_swe4(self):
        assert _tracker_process_area(99, "Component Verification Test Cases") == "SWE.4"

    def test_cysec_maps_to_sec(self):
        assert _tracker_process_area(99, "CySec Validation") == "SEC"

    def test_unknown_name_unmapped(self):
        assert _tracker_process_area(99, "Some Random Tracker") == "UNMAPPED"

    def test_known_id_overrides_name(self):
        assert _tracker_process_area(87637, "Completely Different Name") == "SYS.5"

    def test_known_id_87749_sys4(self):
        assert _tracker_process_area(87749, "") == "SYS.4"

    def test_known_id_88425_sys5(self):
        assert _tracker_process_area(88425, "") == "SYS.5"

    def test_known_id_88370_sys5(self):
        assert _tracker_process_area(88370, "") == "SYS.5"

    def test_known_id_88612_sup9(self):
        assert _tracker_process_area(88612, "") == "SUP.9"

    def test_req_tracker_syrs_sys2(self):
        assert _tracker_process_area(20705, "") == "SYS.2"

    def test_req_tracker_swrs_swe1(self):
        assert _tracker_process_area(30078, "") == "SWE.1"

    def test_req_tracker_syad_sys3(self):
        assert _tracker_process_area(31566, "") == "SYS.3"

    def test_req_tracker_swad_swe2(self):
        assert _tracker_process_area(31657, "") == "SWE.2"

    def test_req_tracker_feature_sys1(self):
        assert _tracker_process_area(94375, "") == "SYS.1"

    def test_req_tracker_cysec_req_sec(self):
        assert _tracker_process_area(101697, "") == "SEC"


# ── testrun / testset → SYS.5 override ───────────────────────────────────────

class TestTestrunTestsetSys5Override:

    def test_testrun_always_sys5(self):
        assert _apply_type_override("TestRun") == "SYS.5"

    def test_testset_always_sys5(self):
        assert _apply_type_override("TestSet") == "SYS.5"

    def test_testrun_lowercase_always_sys5(self):
        assert _apply_type_override("testrun") == "SYS.5"

    def test_testrun_overrides_unmapped_name(self):
        assert _apply_type_override("testrun", tracker_name="Unknown Random") == "SYS.5"

    def test_testcase_type_not_affected(self):
        assert _apply_type_override("TestCase", tracker_name="Unknown") == "UNMAPPED"

    def test_testconf_always_sys5(self):
        assert _apply_type_override("TestConf") == "SYS.5"

    def test_testconf_lowercase_always_sys5(self):
        assert _apply_type_override("testconf", tracker_name="Unknown Random") == "SYS.5"


# ── _item_status_name ─────────────────────────────────────────────────────────

class TestItemStatusName:

    def test_draft_dict_status(self):
        assert _item_status_name({"status": {"name": "Draft"}}).lower() == "draft"

    def test_draft_string_status(self):
        assert _item_status_name({"status": "Draft"}).lower() == "draft"

    def test_no_status_field(self):
        assert _item_status_name({}) == ""

    def test_none_status(self):
        assert _item_status_name({"status": None}) == ""

    def test_approved_status(self):
        assert _item_status_name({"status": {"name": "Approved"}}) == "Approved"

    def test_case_preserved(self):
        assert _item_status_name({"status": {"name": "In Review"}}) == "In Review"


# ── draft exclusion ───────────────────────────────────────────────────────────

class TestDraftExclusion:

    def test_draft_testcase_excluded(self):
        item_by_id = {
            1: {"status": {"name": "Draft"}},
            2: {"status": {"name": "Approved"}},
        }
        ids = _compute_testcase_ids(
            item_tracker={1: 10, 2: 10},
            testcase_tracker_ids={10},
            folder_ids=set(),
            item_by_id=item_by_id,
        )
        assert 1 not in ids
        assert 2 in ids

    def test_non_draft_items_included(self):
        item_by_id = {1: {"status": {"name": "New"}}, 2: {"status": {"name": "Approved"}}}
        ids = _compute_testcase_ids(
            item_tracker={1: 10, 2: 10},
            testcase_tracker_ids={10},
            folder_ids=set(),
            item_by_id=item_by_id,
        )
        assert ids == {1, 2}

    def test_no_status_item_included(self):
        item_by_id = {1: {}}
        ids = _compute_testcase_ids(
            item_tracker={1: 10},
            testcase_tracker_ids={10},
            folder_ids=set(),
            item_by_id=item_by_id,
        )
        assert 1 in ids

    def test_draft_and_folder_both_excluded(self):
        item_by_id = {
            1: {"status": {"name": "Draft"}},
            2: {"status": {"name": "Approved"}},
            3: {"status": {"name": "New"}},
        }
        ids = _compute_testcase_ids(
            item_tracker={1: 10, 2: 10, 3: 10},
            testcase_tracker_ids={10},
            folder_ids={3},
            item_by_id=item_by_id,
        )
        assert ids == {2}


# ── run deduplication ─────────────────────────────────────────────────────────

class TestRunDeduplication:

    def _count_runs(self, testcase_to_runs, testcase_process):
        process_runs_sets: dict[str, set] = defaultdict(set)
        process_runs_passed_sets: dict[str, set] = defaultdict(set)
        runs_passed_set: set = set()
        runs_failed_set: set = set()

        def fake_outcome(rid):
            return "passed" if rid % 2 == 0 else "failed"

        for tcid, runs in testcase_to_runs.items():
            area = testcase_process.get(tcid, "UNMAPPED")
            for rid in runs:
                process_runs_sets[area].add(rid)
                outcome = fake_outcome(rid)
                if outcome == "passed":
                    runs_passed_set.add(rid)
                    process_runs_passed_sets[area].add(rid)
                else:
                    runs_failed_set.add(rid)

        return {
            "process_runs_total": {a: len(s) for a, s in process_runs_sets.items()},
            "process_runs_passed": {a: len(s) for a, s in process_runs_passed_sets.items()},
            "runs_passed": len(runs_passed_set),
            "runs_failed": len(runs_failed_set),
        }

    def test_shared_run_counted_once(self):
        result = self._count_runs(
            testcase_to_runs={1: {100}, 2: {100}},
            testcase_process={1: "SYS.5", 2: "SYS.5"},
        )
        assert result["process_runs_total"]["SYS.5"] == 1

    def test_distinct_runs_counted_separately(self):
        result = self._count_runs(
            testcase_to_runs={1: {100}, 2: {102}},
            testcase_process={1: "SYS.5", 2: "SYS.5"},
        )
        assert result["process_runs_total"]["SYS.5"] == 2

    def test_runs_passed_no_duplicates(self):
        # run 100 (even=passed) linked to two testcases
        result = self._count_runs(
            testcase_to_runs={1: {100}, 2: {100}},
            testcase_process={1: "SYS.5", 2: "SYS.5"},
        )
        assert result["runs_passed"] == 1

    def test_runs_across_areas_not_cross_counted(self):
        result = self._count_runs(
            testcase_to_runs={1: {100}, 2: {101}},
            testcase_process={1: "SYS.5", 2: "SWE.4"},
        )
        assert result["process_runs_total"].get("SYS.5") == 1
        assert result["process_runs_total"].get("SWE.4") == 1
