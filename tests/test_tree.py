import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from pybeamer.tree import build_tree, tree_to_rows


# ── helpers ──────────────────────────────────────────────────────────────────

def make_item(iid, upstream=None, downstream=None):
    return {
        "id": iid,
        "name": f"Item-{iid}",
        "icon": "label",
        "color": "#6c757d",
        "upstream": upstream or [],
        "downstream": downstream or [],
    }


# ── build_tree ────────────────────────────────────────────────────────────────

class TestBuildTree:

    def test_empty_ids(self):
        assert build_tree([], 1, 2, {}) == []

    def test_single_node_no_children(self):
        item_map = {1: make_item(1)}
        result = build_tree([1], 1, 2, item_map)
        assert len(result) == 1
        assert result[0]["item"]["id"] == 1
        assert result[0]["leaf_count"] == 1
        assert result[0]["children"] == []

    def test_two_level_upstream(self):
        item_map = {
            1: make_item(1, upstream=[2]),
            2: make_item(2),
        }
        result = build_tree([1], 1, 2, item_map, direction="upstream")
        assert result[0]["leaf_count"] == 1
        assert len(result[0]["children"]) == 1
        assert result[0]["children"][0]["item"]["id"] == 2

    def test_max_level_stops_recursion(self):
        item_map = {
            1: make_item(1, upstream=[2]),
            2: make_item(2, upstream=[3]),
            3: make_item(3),
        }
        result = build_tree([1], 1, 1, item_map, direction="upstream")
        # max_level=1 → no children loaded
        assert result[0]["children"] == []

    def test_leaf_count_with_branching(self):
        # Item 1 has two upstream: 2 and 3
        item_map = {
            1: make_item(1, upstream=[2, 3]),
            2: make_item(2),
            3: make_item(3),
        }
        result = build_tree([1], 1, 2, item_map, direction="upstream")
        assert result[0]["leaf_count"] == 2
        assert len(result[0]["children"]) == 2

    def test_cycle_prevention(self):
        # Item 1 ↔ Item 2
        item_map = {
            1: make_item(1, upstream=[2]),
            2: make_item(2, upstream=[1]),
        }
        result = build_tree([1], 1, 5, item_map, direction="upstream")
        child = result[0]["children"][0]
        # Item 1 should not appear again in Item 2's children
        assert child["children"] == []

    def test_visited_excludes_current(self):
        item_map = {
            2: make_item(2, downstream=[1]),
            3: make_item(3),
        }
        # visited={1} means item 1 is excluded from downstream of item 2
        result = build_tree([2], 1, 2, item_map, direction="downstream", visited={1})
        assert result[0]["children"] == []  # item 1 excluded

    def test_direction_downstream(self):
        item_map = {
            1: make_item(1, downstream=[2]),
            2: make_item(2),
        }
        result = build_tree([1], 1, 2, item_map, direction="downstream")
        assert result[0]["children"][0]["item"]["id"] == 2

    def test_unknown_item_gets_default(self):
        # item 99 not in map → uses default
        result = build_tree([99], 1, 1, {})
        assert result[0]["item"]["id"] == 99
        assert result[0]["item"]["name"] == "99"

    def test_level_set_correctly(self):
        item_map = {
            1: make_item(1, upstream=[2]),
            2: make_item(2),
        }
        result = build_tree([1], 1, 2, item_map)
        assert result[0]["level"] == 1
        assert result[0]["children"][0]["level"] == 2

    def test_string_ids_converted_to_int(self):
        item_map = {1: make_item(1)}
        result = build_tree(["1"], 1, 1, item_map)
        assert result[0]["item"]["id"] == 1


# ── tree_to_rows ──────────────────────────────────────────────────────────────

class TestTreeToRows:

    def test_empty_nodes(self):
        assert tree_to_rows([], 2) == []

    def test_single_node(self):
        item = make_item(1)
        nodes = [{"item": item, "level": 1, "children": [], "leaf_count": 1}]
        rows = tree_to_rows(nodes, 2)
        assert len(rows) == 1
        assert rows[0][0] == {"item": item, "rowspan": 1}
        assert rows[0][1] is None

    def test_two_independent_nodes(self):
        a, b = make_item(1), make_item(2)
        nodes = [
            {"item": a, "level": 1, "children": [], "leaf_count": 1},
            {"item": b, "level": 1, "children": [], "leaf_count": 1},
        ]
        rows = tree_to_rows(nodes, 1)
        assert len(rows) == 2
        assert rows[0][0]["item"]["id"] == 1
        assert rows[1][0]["item"]["id"] == 2

    def test_parent_with_two_children_rowspan(self):
        c1, c2 = make_item(2), make_item(3)
        parent = make_item(1)
        child_nodes = [
            {"item": c1, "level": 2, "children": [], "leaf_count": 1},
            {"item": c2, "level": 2, "children": [], "leaf_count": 1},
        ]
        nodes = [{"item": parent, "level": 1, "children": child_nodes, "leaf_count": 2}]
        rows = tree_to_rows(nodes, 2)
        assert len(rows) == 2
        # Parent spans 2 rows
        assert rows[0][0] == {"item": parent, "rowspan": 2}
        # Row 1 has no parent cell (covered by rowspan)
        assert rows[1][0] is None
        # Children in column 1
        assert rows[0][1]["item"]["id"] == 2
        assert rows[1][1]["item"]["id"] == 3

    def test_grid_width_equals_max_level(self):
        item = make_item(1)
        nodes = [{"item": item, "level": 1, "children": [], "leaf_count": 1}]
        rows = tree_to_rows(nodes, 5)
        assert all(len(row) == 5 for row in rows)

    def test_deep_chain(self):
        # 1 → 2 → 3 (each level 1, 2, 3)
        i3 = make_item(3)
        i2 = make_item(2)
        i1 = make_item(1)
        n3 = {"item": i3, "level": 3, "children": [], "leaf_count": 1}
        n2 = {"item": i2, "level": 2, "children": [n3], "leaf_count": 1}
        n1 = {"item": i1, "level": 1, "children": [n2], "leaf_count": 1}
        rows = tree_to_rows([n1], 3)
        assert len(rows) == 1
        assert rows[0][0]["item"]["id"] == 1
        assert rows[0][1]["item"]["id"] == 2
        assert rows[0][2]["item"]["id"] == 3
