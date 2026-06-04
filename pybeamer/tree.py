

def build_tree(item_ids, level, max_level, item_map, direction="upstream", visited=None):
    if visited is None:
        visited = set()
    result = []
    for iid in item_ids:
        iid = int(iid) if not isinstance(iid, int) else iid
        if iid in visited:
            continue
        new_visited = visited | {iid}
        item = item_map.get(iid, {
            "id": iid,
            "name": str(iid),
            "icon": "label",
            "color": "#6c757d",
            "upstream": [],
            "downstream": []
            })
        child_ids = item.get(direction, []) if level < max_level else []
        children = build_tree(child_ids, level + 1, max_level, item_map, direction, new_visited)
        leaf_count = sum(c["leaf_count"] for c in children) if children else 1
        result.append({"item": item, "level": level, "children": children, "leaf_count": leaf_count})
    return result


def tree_to_rows(nodes, max_level):
    """Returns list of rows. Each row = list of max_level cells.
    Cell = {item, rowspan} or None (covered by rowspan above)."""
    grid = []

    def recurse(nodes, current_row):
        for node in nodes:
            col = node["level"] - 1
            while len(grid) <= current_row + node["leaf_count"] - 1:
                grid.append([None] * max_level)
            grid[current_row][col] = {"item": node["item"], "rowspan": node["leaf_count"]}
            if node["children"]:
                recurse(node["children"], current_row)
            current_row += node["leaf_count"]

    recurse(nodes, 0)
    return grid

