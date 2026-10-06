"""成组预选：同体系、未占用、满足三项约束，优先使最小容量最大，
同值时按电芯 ID 排序序列的字典序取最小者。待选电芯最多 12 节，穷举组合足够快。"""
from itertools import combinations


def choose_group(cells: list[dict], series_count: int, min_capacity: float,
                 max_capacity_diff: float, max_resistance_diff: float):
    """从同一化学体系的候选电芯中选出最优组合。

    返回 (combo, limited_capacity)；无可行组合时返回 (None, None)。
    """
    eligible = [c for c in cells if c["capacity"] >= min_capacity]
    best_combo = None
    best_key = None  # (min_capacity, sorted_ids)；min_capacity 越大越好，ids 字典序越小越好
    for combo in combinations(eligible, series_count):
        caps = [c["capacity"] for c in combo]
        if max(caps) - min(caps) > max_capacity_diff:
            continue
        res = [c["resistance"] for c in combo]
        if max(res) - min(res) > max_resistance_diff:
            continue
        ids = tuple(sorted(c["cell_id"] for c in combo))
        if (best_key is None
                or min(caps) > best_key[0]
                or (min(caps) == best_key[0] and ids < best_key[1])):
            best_key = (min(caps), ids)
            best_combo = combo
    if best_combo is None:
        return None, None
    return list(best_combo), best_key[0]
