import os
import tempfile
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    os.environ["DB_PATH"] = path
    from app import db
    db.reset()
    from app.main import app
    with TestClient(app) as c:
        yield c
    db.reset()
    Path(path).unlink(missing_ok=True)


def add_cell(client, cell_id, chemistry="LFP", capacity=100.0, resistance=2.0, version="T1"):
    r = client.post("/cells", json={
        "cell_id": cell_id, "chemistry": chemistry, "capacity": capacity,
        "resistance": resistance, "test_version": version,
    })
    assert r.status_code == 201, r.text
    return r


def seed_six(client):
    # 6 节 LFP：容量两两差 <= 5，内阻差 <= 0.5
    data = [
        ("C001", 100.0, 2.0), ("C002", 99.0, 2.1), ("C003", 98.0, 2.2),
        ("C004", 97.0, 2.3), ("C005", 96.0, 2.4), ("C006", 95.5, 2.5),
    ]
    for cid, cap, res in data:
        add_cell(client, cid, capacity=cap, resistance=res)


def isolate(client, cell_id, reason="检出安全隐患"):
    return client.post(f"/cells/{cell_id}/isolate", json={"reason": reason})


PRESELECT4 = {
    "series_count": 4, "min_capacity": 90.0,
    "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
}


def test_register_limit_12(client):
    for i in range(12):
        add_cell(client, f"C{i:03d}")
    r = client.post("/cells", json={
        "cell_id": "C999", "chemistry": "LFP", "capacity": 1.0,
        "resistance": 1.0, "test_version": "T1",
    })
    assert r.status_code == 409
    assert "12" in r.json()["detail"]


def test_duplicate_cell_rejected(client):
    add_cell(client, "C001")
    r = client.post("/cells", json={
        "cell_id": "C001", "chemistry": "LFP", "capacity": 1.0,
        "resistance": 1.0, "test_version": "T1",
    })
    assert r.status_code == 409


def test_preselect_maximizes_min_capacity(client):
    seed_six(client)
    r = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    body = r.json()
    assert body["feasible"] is True
    # 最小容量最大的 4 节组合是 C001..C004（min=97）
    assert body["cell_ids"] == ["C001", "C002", "C003", "C004"]
    assert body["limited_capacity"] == 97.0
    assert all(c["test_version"] == "T1" for c in body["cells"])


def test_preselect_tie_break_lexicographic(client):
    # 4 节中任取 3 节最小容量都是 99：同值时按 ID 排序序列字典序取最小
    add_cell(client, "B001", capacity=100.0, resistance=2.0)
    add_cell(client, "B002", capacity=99.0, resistance=2.0)
    add_cell(client, "A001", capacity=100.0, resistance=2.0)
    add_cell(client, "A002", capacity=99.0, resistance=2.0)
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 1.0, "max_resistance_diff": 0.5,
    })
    body = r.json()
    assert body["feasible"] is True
    assert body["limited_capacity"] == 99.0
    assert body["cell_ids"] == ["A001", "A002", "B001"]


def test_preselect_chemistry_isolated(client):
    seed_six(client)
    add_cell(client, "N001", chemistry="NCM", capacity=100.0, resistance=2.0)
    add_cell(client, "N002", chemistry="NCM", capacity=99.0, resistance=2.0)
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5, "chemistry": "NCM",
    })
    body = r.json()
    assert body["feasible"] is False
    assert "不足 3 节" in body["reason"]


def test_preselect_infeasible_reasons(client):
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert r.json()["feasible"] is False
    assert "没有可用的未占用电芯" in r.json()["reason"]

    # 容量全部不达标
    add_cell(client, "C001", capacity=50.0)
    add_cell(client, "C002", capacity=50.0)
    add_cell(client, "C003", capacity=50.0)
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert "最低单芯容量" in r.json()["reason"]

    # 数量够但差值约束不满足
    add_cell(client, "C004", capacity=200.0, resistance=9.0)
    add_cell(client, "C005", capacity=100.0, resistance=2.0)
    add_cell(client, "C006", capacity=100.0, resistance=2.0)
    r = client.post("/preselect", json={
        "series_count": 3, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert "容量差上限" in r.json()["reason"] or "内阻差上限" in r.json()["reason"]


def test_confirm_success_and_snapshot(client):
    seed_six(client)
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    r = client.post("/groups/confirm", json=payload)
    assert r.status_code == 201, r.text
    group = r.json()
    assert group["group_id"] == 1
    assert group["limited_capacity"] == 97.0
    assert group["series_count"] == 4

    # 电芯被占用
    cells = client.get("/cells").json()["cells"]
    used = {c["cell_id"]: c for c in cells if c["occupied"]}
    assert set(used) == set(pre["cell_ids"])
    assert all(c["group_id"] == 1 for c in used.values())

    # 快照可按组编号查询
    snap = client.get("/groups/1").json()
    assert snap["cells"] == group["cells"]


def test_confirm_stale_version_rejected_atomically(client):
    seed_six(client)
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    # 预选后复测其中一节 -> 候选过期
    client.put("/cells/C002/test", json={
        "capacity": 99.0, "resistance": 2.1, "test_version": "T2"})
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    r = client.post("/groups/confirm", json=payload)
    assert r.status_code == 409
    assert "C002" in r.json()["detail"]
    # 整组拒绝：无任何部分占用，无组产生
    assert all(not c["occupied"] for c in client.get("/cells").json()["cells"])
    assert client.get("/groups").json()["count"] == 0


def test_confirm_occupied_by_other_group_rejected(client):
    seed_six(client)
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    assert client.post("/groups/confirm", json=payload).status_code == 201
    # 别组已抢先占用 -> 再次确认同一候选被拒
    r = client.post("/groups/confirm", json=payload)
    assert r.status_code == 409
    assert "占用" in r.json()["detail"]
    assert client.get("/groups").json()["count"] == 1


def test_revision_after_confirm_does_not_change_snapshot(client):
    seed_six(client)
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    group = client.post("/groups/confirm", json=payload).json()
    # 确认后复测已入组电芯：允许修订档案，但快照不变
    client.put("/cells/C001/test", json={
        "capacity": 88.0, "resistance": 3.0, "test_version": "T9"})
    snap = client.get(f"/groups/{group['group_id']}").json()
    c001 = next(c for c in snap["cells"] if c["cell_id"] == "C001")
    assert c001["capacity"] == 100.0
    assert c001["test_version"] == "T1"
    assert snap["limited_capacity"] == 97.0


def test_confirm_mixed_chemistry_rejected(client):
    add_cell(client, "C001", chemistry="LFP")
    add_cell(client, "C002", chemistry="LFP")
    add_cell(client, "C003", chemistry="NCM")
    r = client.post("/groups/confirm", json={"cells": [
        {"cell_id": "C001", "test_version": "T1"},
        {"cell_id": "C002", "test_version": "T1"},
        {"cell_id": "C003", "test_version": "T1"},
    ]})
    assert r.status_code == 409
    assert "化学体系" in r.json()["detail"]
    assert all(not c["occupied"] for c in client.get("/cells").json()["cells"])


def test_series_count_validation(client):
    r = client.post("/preselect", json={
        "series_count": 2, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert r.status_code == 422
    r = client.post("/preselect", json={
        "series_count": 7, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    })
    assert r.status_code == 422


def _begin_uncommitted_group(conn):
    """在当前线程的连接上模拟一个尚未提交的确认事务。"""
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO groups (chemistry, series_count, limited_capacity, created_at)"
        " VALUES ('LFP', 4, 97.0, '2026-10-05T00:00:00+00:00')")
    gid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    for cid, cap, res in [("C001", 100.0, 2.0), ("C002", 99.0, 2.1),
                          ("C003", 98.0, 2.2), ("C004", 97.0, 2.3)]:
        conn.execute(
            "INSERT INTO group_cells (group_id, cell_id, capacity, resistance, test_version)"
            " VALUES (?, ?, ?, ?, 'T1')", (gid, cid, cap, res))
        conn.execute(
            "UPDATE cells SET occupied = 1, group_id = ? WHERE cell_id = ?", (gid, cid))
    return gid


def test_uncommitted_group_invisible_to_other_requests(client):
    """确认事务未提交时，其他请求看不到组记录、快照与临时占用；提交后整组一次性可见。"""
    seed_six(client)
    from app import db
    gid = _begin_uncommitted_group(db.get_conn())  # 本线程连接持有未提交事务

    # 未提交：组列表 / 按编号查询 / 电芯占用 / 预选都看不到事务的中间状态
    assert client.get("/groups").json()["count"] == 0
    assert client.get(f"/groups/{gid}").status_code == 404
    assert all(not c["occupied"] for c in client.get("/cells").json()["cells"])
    pre = client.post("/preselect", json={
        "series_count": 4, "min_capacity": 90.0,
        "max_capacity_diff": 5.0, "max_resistance_diff": 0.5,
    }).json()
    assert pre["feasible"] is True
    assert pre["cell_ids"] == ["C001", "C002", "C003", "C004"]

    # 提交后：整组（组记录 + 快照 + 占用）一次性可见
    db.get_conn().commit()
    assert client.get("/groups").json()["count"] == 1
    snap = client.get(f"/groups/{gid}").json()
    assert [c["cell_id"] for c in snap["cells"]] == ["C001", "C002", "C003", "C004"]
    used = {c["cell_id"] for c in client.get("/cells").json()["cells"] if c["occupied"]}
    assert used == {"C001", "C002", "C003", "C004"}


def test_rolled_back_group_leaves_no_trace(client):
    """确认回滚后，组记录、快照与占用对外毫无痕迹。"""
    seed_six(client)
    from app import db
    conn = db.get_conn()
    gid = _begin_uncommitted_group(conn)
    conn.rollback()
    assert client.get("/groups").json()["count"] == 0
    assert client.get(f"/groups/{gid}").status_code == 404
    assert all(not c["occupied"] for c in client.get("/cells").json()["cells"])


def test_concurrent_confirm_and_reads_do_not_interfere(client):
    """并发确认同一候选：只有一个成功；并发查询只看到完整快照，互不干扰。"""
    seed_six(client)
    from app.main import app
    payload = {"cells": [{"cell_id": f"C00{i}", "test_version": "T1"} for i in range(1, 5)]}
    statuses = []
    broken = []

    def worker():
        with TestClient(app) as c:
            r = c.post("/groups/confirm", json=payload)
            statuses.append(r.status_code)
            for _ in range(10):
                # 组列表里出现的任何组都必须带完整快照
                for g in c.get("/groups").json()["groups"]:
                    if len(g["cells"]) != g["series_count"]:
                        broken.append(g)
                # 已提交的占用必须整组可见，不允许只看到部分占用
                members = [cell["cell_id"] for cell in c.get("/cells").json()["cells"]
                           if cell["occupied"]]
                if members and len(members) != 4:
                    broken.append(members)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert statuses.count(201) == 1
    assert statuses.count(409) == 3
    assert not broken
    assert client.get("/groups").json()["count"] == 1


# ---------- 安全隔离 ----------

def test_isolate_cell_records_isolation(client):
    add_cell(client, "C001")
    r = isolate(client, "C001", "外壳鼓包")
    assert r.status_code == 201
    body = r.json()
    assert body["cell_id"] == "C001"
    assert body["reason"] == "外壳鼓包"
    assert body["isolated_at"]
    # 电芯档案带隔离标记（新增字段，原字段保持不变）
    cell = client.get("/cells/C001").json()
    assert cell["isolated"] is True
    assert cell["occupied"] is False
    # 隔离列表可追溯
    lst = client.get("/isolations").json()
    assert lst["count"] == 1
    assert lst["isolations"][0]["cell_id"] == "C001"
    assert lst["isolations"][0]["reason"] == "外壳鼓包"


def test_isolate_missing_cell_404(client):
    r = isolate(client, "NOPE")
    assert r.status_code == 404
    assert "NOPE" in r.json()["detail"]


def test_repeat_isolation_returns_original_record(client):
    add_cell(client, "C001")
    first = isolate(client, "C001", "首次原因")
    assert first.status_code == 201
    # 重复隔离（即使原因不同）返回首次隔离的原记录，不新增、不改写
    again = isolate(client, "C001", "不同的新原因")
    assert again.status_code == 200
    assert again.json() == first.json()
    assert client.get("/isolations").json()["count"] == 1


def test_isolation_irreversible_no_delete_endpoint(client):
    add_cell(client, "C001")
    isolate(client, "C001")
    assert client.delete("/cells/C001/isolate").status_code == 405
    assert client.get("/isolations").json()["count"] == 1


def test_retest_cannot_clear_isolation(client):
    add_cell(client, "C001")
    isolate(client, "C001", "热失控风险")
    # 复测只更新检测档案，隔离记录保持不变
    r = client.put("/cells/C001/test", json={
        "capacity": 98.0, "resistance": 2.0, "test_version": "T2"})
    assert r.status_code == 200
    assert r.json()["isolated"] is True
    lst = client.get("/isolations").json()
    assert lst["count"] == 1
    assert lst["isolations"][0]["reason"] == "热失控风险"


def test_preselect_excludes_isolated_cells(client):
    seed_six(client)
    isolate(client, "C001", "待复检")
    body = client.post("/preselect", json=PRESELECT4).json()
    assert body["feasible"] is True
    # 最优组合跳过 C001，顺延为 C002~C005
    assert body["cell_ids"] == ["C002", "C003", "C004", "C005"]
    assert body["limited_capacity"] == 96.0
    # 隔离到剩余不足成组时，预选判不可行
    for cid in ("C002", "C003", "C004"):
        isolate(client, cid, "待复检")
    body = client.post("/preselect", json=PRESELECT4).json()
    assert body["feasible"] is False
    assert "不足 4 节" in body["reason"]


def test_confirm_rechecks_isolation_and_rejects_atomically(client):
    seed_six(client)
    pre = client.post("/preselect", json=PRESELECT4).json()
    # 预选后、确认前隔离候选中的一节 -> 确认事务重查时命中，整组拒绝
    isolate(client, "C002", "复检不合格")
    payload = {"cells": [{"cell_id": c["cell_id"], "test_version": c["test_version"]}
                         for c in pre["cells"]]}
    r = client.post("/groups/confirm", json=payload)
    assert r.status_code == 409
    assert "C002" in r.json()["detail"]
    # 整组拒绝：不产生组，也不留任何部分占用
    assert client.get("/groups").json()["count"] == 0
    assert all(not c["occupied"] for c in client.get("/cells").json()["cells"])


def _confirm(client, ids):
    return client.post("/groups/confirm", json={
        "cells": [{"cell_id": i, "test_version": "T1"} for i in ids]})


def test_isolating_grouped_cell_blocks_group_snapshot_retained(client):
    seed_six(client)
    assert _confirm(client, ["C001", "C002", "C003"]).status_code == 201
    assert _confirm(client, ["C004", "C005", "C006"]).status_code == 201

    # 隔离已入组（组 1）的电芯
    assert isolate(client, "C002", "抽检发现漏液").status_code == 201

    groups = {g["group_id"]: g for g in client.get("/groups").json()["groups"]}
    g1, g2 = groups[1], groups[2]
    # 组 1 已阻断：显示涉及的电芯 ID 与原因
    assert g1["blocked"] is True
    assert [b["cell_id"] for b in g1["blocked_cells"]] == ["C002"]
    assert g1["blocked_cells"][0]["reason"] == "抽检发现漏液"
    assert g1["blocked_cells"][0]["isolated_at"]
    # 原检测快照保留不变
    assert [c["cell_id"] for c in g1["cells"]] == ["C001", "C002", "C003"]
    c002 = next(c for c in g1["cells"] if c["cell_id"] == "C002")
    assert c002["capacity"] == 99.0 and c002["test_version"] == "T1"
    assert g1["limited_capacity"] == 98.0
    # 组占用保留
    cells = {c["cell_id"]: c for c in client.get("/cells").json()["cells"]}
    assert cells["C002"]["occupied"] is True
    assert cells["C002"]["group_id"] == 1
    # 未受影响的组 2 保持可放行
    assert g2["blocked"] is False
    assert g2["blocked_cells"] == []

    # 按组编号查询同样显示阻断信息与完整快照
    one = client.get("/groups/1").json()
    assert one["blocked"] is True
    assert [b["cell_id"] for b in one["blocked_cells"]] == ["C002"]
    assert one["cells"] == g1["cells"]
    assert client.get("/groups/2").json()["blocked"] is False


def test_uncommitted_isolation_invisible_to_other_requests(client):
    """隔离事务未提交时，其他请求看不到；提交后预选立即排除该电芯。"""
    seed_six(client)
    from app import db
    conn = db.get_conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO isolations (cell_id, reason, created_at)"
        " VALUES ('C001', '排查中', '2026-10-06T00:00:00+00:00')")
    # 未提交：预选、隔离列表、电芯档案都看不到这条隔离
    pre = client.post("/preselect", json=PRESELECT4).json()
    assert pre["cell_ids"] == ["C001", "C002", "C003", "C004"]
    assert client.get("/isolations").json()["count"] == 0
    assert client.get("/cells/C001").json()["isolated"] is False
    # 提交后：一次性可见，预选排除 C001
    conn.commit()
    pre = client.post("/preselect", json=PRESELECT4).json()
    assert pre["cell_ids"] == ["C002", "C003", "C004", "C005"]
    assert client.get("/isolations").json()["count"] == 1
    assert client.get("/cells/C001").json()["isolated"] is True


def test_isolation_and_confirm_race_commit_order_decides(client):
    """隔离与确认并发：以提交先后决定是拒绝候选还是阻断新组，查询不见半成品。"""
    seed_six(client)
    from app.main import app
    payload = {"cells": [{"cell_id": f"C00{i}", "test_version": "T1"} for i in range(1, 5)]}
    results = {}
    broken = []

    def do_confirm():
        with TestClient(app) as c:
            results["confirm"] = c.post("/groups/confirm", json=payload).status_code

    def do_isolate():
        with TestClient(app) as c:
            results["isolate"] = c.post(
                "/cells/C002/isolate", json={"reason": "并发隔离"}).status_code
            for _ in range(10):
                # 任何可见的组都必须带完整快照，阻断标记与隔离记录一致
                for g in c.get("/groups").json()["groups"]:
                    if len(g["cells"]) != g["series_count"]:
                        broken.append(g)
                    if g["blocked"] and not g["blocked_cells"]:
                        broken.append(g)

    threads = [threading.Thread(target=do_confirm), threading.Thread(target=do_isolate)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not broken
    assert results["isolate"] == 201  # 隔离本身总是成功
    groups = client.get("/groups").json()["groups"]
    if results["confirm"] == 201:
        # 确认先提交：新组产生，随后的隔离使其阻断；快照完整、占用保留
        assert len(groups) == 1
        g = groups[0]
        assert g["blocked"] is True
        assert [b["cell_id"] for b in g["blocked_cells"]] == ["C002"]
        assert len(g["cells"]) == 4
        used = {c["cell_id"] for c in client.get("/cells").json()["cells"] if c["occupied"]}
        assert used == {"C001", "C002", "C003", "C004"}
    else:
        # 隔离先提交：确认整组拒绝，不产生组或部分占用
        assert results["confirm"] == 409
        assert groups == []
        assert all(not c["occupied"] for c in client.get("/cells").json()["cells"])


# ---------- 可控读写交错：单次查询内不允许跨快照拼装状态 ----------
#
# 交错顺序：读请求先读到 C001 的占用信息（occupied=0），此刻挂起；
# 写方先提交"确认"（C001 占用 + 组编号），再提交"隔离" C001；
# 随后读请求才去读隔离信息。
#
# 正确实现下两条 SELECT 共享同一个已提交快照，只可能得到：
#   确认前：occupied=false, group_id=null, isolated=false
#   确认后/隔离后：occupied=true, group_id=<gid>, isolated=true
# 绝不能拼出 occupied=false + isolated=true 这种不存在的状态。


def _install_reader_gate(conn, gate, occurrence=1):
    """在连接上装一个一次性闸门：第 occurrence 次读取 cells 行时，在该语句
    已建立读快照、尚未返回前挂起，直到 gate['release'] 被置位；进入挂起前
    置位 gate['inside']。

    用 "FROM cells" 作标记（旧实现的 `SELECT * FROM cells ...` 与新实现的
    `... FROM cells c LEFT JOIN isolations ...` 都命中），但排除录入事务里的
    `SELECT COUNT(*) ... FROM cells`；occurrence 用于录入/复测（写事务内的
    查询不计入或计入后，只挂提交后构建响应的那一次）。
    """
    seen = {"n": 0, "should_block": False, "blocked": False}

    def trace(statement):
        compact = statement.upper()
        if "FROM CELLS" in compact and "COUNT(" not in compact:
            seen["n"] += 1
            if seen["n"] == occurrence:
                seen["should_block"] = True

    def progress():
        if seen["should_block"] and not seen["blocked"]:
            seen["blocked"] = True
            seen["should_block"] = False
            gate["inside"].set()
            gate["release"].wait(5)
        return 0

    conn.set_trace_callback(trace)
    conn.set_progress_handler(progress, 1)


def _commit_confirm_then_isolate(path, target="C001", group_id=1):
    """写方线程：在独立连接上先提交整组确认的占用效果（occupied + 组编号），
    再提交对 target 的隔离。复现"先确认后隔离"两次提交在同一读请求窗口内落库。"""
    import sqlite3
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "UPDATE cells SET occupied = 1, group_id = ? WHERE cell_id = ?",
        (group_id, target))
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO isolations (cell_id, reason, created_at)"
        " VALUES (?, ?, '2026-10-06T00:00:00+00:00')", (target, "并发交错隔离"))
    conn.commit()
    conn.close()


def _assert_coherent_cell(cell, gid=1):
    """一节电芯的占用、组编号、隔离标志必须来自同一已提交快照。"""
    if cell["occupied"]:
        # 确认后（可能也是隔离后）
        assert cell["group_id"] == gid
    else:
        # 确认前：必无组编号，也不可能已隔离（隔离发生在确认提交之后）
        assert cell["group_id"] is None
        assert cell["isolated"] is False


def test_list_cells_reads_one_committed_snapshot(client):
    """GET /cells：占用、组编号与隔离标志取自同一已提交快照，不跨快照拼装。"""
    seed_six(client)
    from app import db
    from app.main import list_cells
    path = os.environ["DB_PATH"]

    gate = {"inside": threading.Event(), "release": threading.Event()}
    outcome = {}

    def reader():
        conn = db.get_conn()  # 在本线程内取连接，闸门装在读请求专用连接上
        _install_reader_gate(conn, gate, occurrence=1)
        try:
            outcome["body"] = list_cells()
        finally:
            conn.set_trace_callback(None)
            conn.set_progress_handler(None, 0)

    rt = threading.Thread(target=reader)
    rt.start()
    assert gate["inside"].wait(5)
    _commit_confirm_then_isolate(path)
    gate["release"].set()
    rt.join(5)
    assert not rt.is_alive()

    cells = {c["cell_id"]: c for c in outcome["body"]["cells"]}
    _assert_coherent_cell(cells["C001"])
    # 未被交错影响的电芯保持确认前状态
    for cid in ("C002", "C003", "C004", "C005", "C006"):
        assert cells[cid]["occupied"] is False
        assert cells[cid]["group_id"] is None
        assert cells[cid]["isolated"] is False


def test_get_cell_reads_one_committed_snapshot(client):
    """GET /cells/{cell_id}：同样不允许跨快照拼出 occupied=false, isolated=true。"""
    seed_six(client)
    from app import db
    from app.main import get_cell
    path = os.environ["DB_PATH"]

    gate = {"inside": threading.Event(), "release": threading.Event()}
    outcome = {}

    def reader():
        conn = db.get_conn()
        _install_reader_gate(conn, gate, occurrence=1)
        try:
            outcome["cell"] = get_cell("C001")
        finally:
            conn.set_trace_callback(None)
            conn.set_progress_handler(None, 0)

    rt = threading.Thread(target=reader)
    rt.start()
    assert gate["inside"].wait(5)
    _commit_confirm_then_isolate(path)
    gate["release"].set()
    rt.join(5)
    assert not rt.is_alive()

    _assert_coherent_cell(outcome["cell"])


def test_create_cell_response_reads_one_committed_snapshot(client):
    """录入响应复用电芯口径：返回档案的占用与隔离状态也必须同快照。

    交错：写入新电芯 C007 提交后构建响应时，先读到 C007 占用信息再挂起；
    写方并发确认 C007 并隔离 C007。返回只可能是确认前
    （occupied=false, group_id=null, isolated=false）或确认/隔离后
    （occupied=true, group_id=1, isolated=true）的完整状态。
    """
    seed_six(client)
    from app import db
    from app.main import create_cell
    from app.schemas import CellCreate
    path = os.environ["DB_PATH"]

    gate = {"inside": threading.Event(), "release": threading.Event()}
    outcome = {}

    def reader():
        conn = db.get_conn()
        # 录入事务内的 SELECT COUNT(*) 被排除，提交后构建响应的行读取是第 1 次
        _install_reader_gate(conn, gate, occurrence=1)
        try:
            outcome["cell"] = create_cell(CellCreate(
                cell_id="C007", chemistry="LFP", capacity=95.0,
                resistance=2.6, test_version="T1"))
        finally:
            conn.set_trace_callback(None)
            conn.set_progress_handler(None, 0)

    rt = threading.Thread(target=reader)
    rt.start()
    assert gate["inside"].wait(5)
    _commit_confirm_then_isolate(path, target="C007")
    gate["release"].set()
    rt.join(5)
    assert not rt.is_alive()

    cell = outcome["cell"]
    assert cell["cell_id"] == "C007"
    _assert_coherent_cell(cell)


def test_revise_test_response_reads_one_committed_snapshot(client):
    """复测响应复用电芯口径：返回档案的占用与隔离状态也必须同快照。

    交错：复测 C001 提交后读取档案，先读到占用信息（复测不改占用）再挂起；
    写方提交确认 C001 并隔离 C001。返回只可能是确认前或确认后的完整状态。
    """
    seed_six(client)
    from app import db
    from app.main import revise_test
    from app.schemas import TestRevision
    path = os.environ["DB_PATH"]

    gate = {"inside": threading.Event(), "release": threading.Event()}
    outcome = {}

    def reader():
        conn = db.get_conn()
        # 第 1 次 SELECT * FROM cells 在复测写事务内（不挂）；挂提交后构建响应的第 2 次
        _install_reader_gate(conn, gate, occurrence=2)
        try:
            outcome["cell"] = revise_test("C001", TestRevision(
                capacity=100.5, resistance=1.9, test_version="T2"))
        finally:
            conn.set_trace_callback(None)
            conn.set_progress_handler(None, 0)

    rt = threading.Thread(target=reader)
    rt.start()
    assert gate["inside"].wait(5)
    _commit_confirm_then_isolate(path)
    gate["release"].set()
    rt.join(5)
    assert not rt.is_alive()

    cell = outcome["cell"]
    assert cell["test_version"] == "T2"
    _assert_coherent_cell(cell)


# ---------- 交付批次 ----------

def _make_group(client, ids):
    r = _confirm(client, ids)
    assert r.status_code == 201, r.text
    return r.json()["group_id"]


def _seed_twelve(client):
    """录满 12 节 LFP，可组成 4 个 3 节组。"""
    seed_six(client)
    data = [
        ("C007", 94.0, 2.6), ("C008", 93.0, 2.7), ("C009", 92.0, 2.8),
        ("C010", 91.0, 2.9), ("C011", 90.5, 3.0), ("C012", 90.0, 3.1),
    ]
    for cid, cap, res in data:
        add_cell(client, cid, capacity=cap, resistance=res)


def _assert_coherent_batch(b):
    """一次批次查询内的可交付状态、阻断明细与各组快照必须来自同一已提交快照。"""
    blocked_gids = {g["group_id"] for g in b["groups"] if g["blocked"]}
    detail_gids = {x["group_id"] for x in b["blocked_groups"]}
    assert blocked_gids == detail_gids
    assert b["deliverable"] == (not blocked_gids)
    for g in b["groups"]:
        assert len(g["cells"]) == g["series_count"]


def test_create_batch_success(client):
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    r = client.post("/batches", json={"group_ids": [g1, g2]})
    assert r.status_code == 201, r.text
    batch = r.json()
    assert batch["batch_id"] == 1
    assert batch["chemistry"] == "LFP"
    assert batch["group_ids"] == [g1, g2]
    assert batch["deliverable"] is True
    assert batch["blocked_groups"] == []
    # 各组确认时的检测快照随批次返回，与组查询口径一致
    assert [g["group_id"] for g in batch["groups"]] == [g1, g2]
    for g in batch["groups"]:
        assert g["cells"] == client.get(f"/groups/{g['group_id']}").json()["cells"]
    # 按批次号查询与列表口径一致
    assert client.get(f"/batches/{batch['batch_id']}").json() == batch
    lst = client.get("/batches").json()
    assert lst["count"] == 1
    assert lst["batches"][0] == batch


def test_batch_of_four_groups(client):
    _seed_twelve(client)
    gids = [
        _make_group(client, ["C001", "C002", "C003"]),
        _make_group(client, ["C004", "C005", "C006"]),
        _make_group(client, ["C007", "C008", "C009"]),
        _make_group(client, ["C010", "C011", "C012"]),
    ]
    r = client.post("/batches", json={"group_ids": gids})
    assert r.status_code == 201, r.text
    assert r.json()["group_ids"] == gids
    assert len(r.json()["groups"]) == 4


def test_batch_group_count_validation(client):
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    # 少于 2 个组 / 多于 4 个组都不接受
    assert client.post("/batches", json={"group_ids": [g1]}).status_code == 422
    assert client.post("/batches", json={"group_ids": [1, 2, 3, 4, 5]}).status_code == 422
    assert client.get("/batches").json()["count"] == 0


def test_batch_duplicate_group_ids_rejected(client):
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    r = client.post("/batches", json={"group_ids": [g1, g1]})
    assert r.status_code == 422
    assert client.get("/batches").json()["count"] == 0


def test_batch_missing_group_rejected(client):
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    r = client.post("/batches", json={"group_ids": [g1, 999]})
    assert r.status_code == 409
    assert "999" in r.json()["detail"]
    # 整批拒绝：不产生批次，g1 也不留归属
    assert client.get("/batches").json()["count"] == 0
    assert client.get("/batches/1").status_code == 404


def test_batch_mixed_chemistry_rejected(client):
    add_cell(client, "C001", chemistry="LFP")
    add_cell(client, "C002", chemistry="LFP")
    add_cell(client, "C003", chemistry="LFP")
    add_cell(client, "N001", chemistry="NCM")
    add_cell(client, "N002", chemistry="NCM")
    add_cell(client, "N003", chemistry="NCM")
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["N001", "N002", "N003"])
    r = client.post("/batches", json={"group_ids": [g1, g2]})
    assert r.status_code == 409
    assert "化学体系" in r.json()["detail"]
    assert client.get("/batches").json()["count"] == 0


def test_batch_blocked_group_rejected_atomically(client):
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    # 组建后隔离 g1 的成员电芯 -> g1 当前被阻断，不能入批
    isolate(client, "C002", "抽检发现漏液")
    r = client.post("/batches", json={"group_ids": [g1, g2]})
    assert r.status_code == 409
    assert str(g1) in r.json()["detail"]
    # 整批拒绝：不产生批次，未阻断的 g2 也不留归属，可另组他批
    assert client.get("/batches").json()["count"] == 0
    add_cell(client, "C007", capacity=94.0, resistance=2.6)
    add_cell(client, "C008", capacity=93.0, resistance=2.7)
    add_cell(client, "C009", capacity=92.0, resistance=2.8)
    g3 = _make_group(client, ["C007", "C008", "C009"])
    r = client.post("/batches", json={"group_ids": [g2, g3]})
    assert r.status_code == 201
    assert r.json()["group_ids"] == [g2, g3]


def test_batch_group_already_assigned_rejected_atomically(client):
    _seed_twelve(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    g3 = _make_group(client, ["C007", "C008", "C009"])
    g4 = _make_group(client, ["C010", "C011", "C012"])
    assert client.post("/batches", json={"group_ids": [g1, g2]}).status_code == 201
    # g2 已被批次 1 先行占用 -> 整批拒绝
    r = client.post("/batches", json={"group_ids": [g2, g3]})
    assert r.status_code == 409
    assert "批次" in r.json()["detail"]
    assert client.get("/batches").json()["count"] == 1
    # g3 未被部分占用：仍可与 g4 建批
    r = client.post("/batches", json={"group_ids": [g3, g4]})
    assert r.status_code == 201
    assert r.json()["group_ids"] == [g3, g4]


def test_isolation_after_batch_blocks_batch_membership_snapshot_retained(client):
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    bid = client.post("/batches", json={"group_ids": [g1, g2]}).json()["batch_id"]
    before = client.get(f"/batches/{bid}").json()
    assert before["deliverable"] is True

    # 建批后隔离成员组 g2 的电芯 -> 整批转为不可交付
    isolate(client, "C005", "巡检发现鼓包")

    b = client.get(f"/batches/{bid}").json()
    assert b["deliverable"] is False
    # 受影响的组号、电芯 ID 与原因可追溯
    assert [(x["group_id"], x["cell_id"], x["reason"]) for x in b["blocked_groups"]] == [
        (g2, "C005", "巡检发现鼓包")]
    assert b["blocked_groups"][0]["isolated_at"]
    # 成员关系固定，各组确认时的检测快照不改写
    assert b["group_ids"] == before["group_ids"]
    assert [g["cells"] for g in b["groups"]] == [g["cells"] for g in before["groups"]]
    # 批次列表口径一致；组查询同样显示该组阻断
    lst = client.get("/batches").json()
    assert lst["count"] == 1
    assert lst["batches"][0]["deliverable"] is False
    assert client.get(f"/groups/{g2}").json()["blocked"] is True


def test_batch_query_returns_confirmation_snapshots(client):
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    bid = client.post("/batches", json={"group_ids": [g1, g2]}).json()["batch_id"]
    # 建批后复测已入组电芯：档案更新，批次返回的仍是确认时快照
    client.put("/cells/C001/test", json={
        "capacity": 88.0, "resistance": 3.0, "test_version": "T9"})
    b = client.get(f"/batches/{bid}").json()
    g1_payload = next(g for g in b["groups"] if g["group_id"] == g1)
    c001 = next(c for c in g1_payload["cells"] if c["cell_id"] == "C001")
    assert c001["capacity"] == 100.0
    assert c001["test_version"] == "T1"
    assert b["deliverable"] is True


def test_uncommitted_batch_invisible_to_other_requests(client):
    """建批事务未提交时，其他请求看不到批次与归属；提交后整批一次性可见。"""
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    from app import db
    conn = db.get_conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO batches (chemistry, created_at)"
        " VALUES ('LFP', '2026-10-06T00:00:00+00:00')")
    bid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    for gid in (g1, g2):
        conn.execute(
            "INSERT INTO batch_groups (batch_id, group_id) VALUES (?, ?)", (bid, gid))

    # 未提交：批次列表 / 按编号查询都看不到事务的中间状态
    assert client.get("/batches").json()["count"] == 0
    assert client.get(f"/batches/{bid}").status_code == 404

    # 提交后：整批（批次 + 成员关系 + 各组快照）一次性可见
    conn.commit()
    assert client.get("/batches").json()["count"] == 1
    b = client.get(f"/batches/{bid}").json()
    assert b["group_ids"] == [g1, g2]
    assert b["deliverable"] is True
    _assert_coherent_batch(b)


def test_rolled_back_batch_leaves_no_trace(client):
    """建批回滚后，批次与归属对外毫无痕迹，成员组可重新建批。"""
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    from app import db
    conn = db.get_conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO batches (chemistry, created_at)"
        " VALUES ('LFP', '2026-10-06T00:00:00+00:00')")
    bid = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    conn.execute(
        "INSERT INTO batch_groups (batch_id, group_id) VALUES (?, ?)", (bid, g1))
    conn.rollback()
    assert client.get("/batches").json()["count"] == 0
    assert client.get(f"/batches/{bid}").status_code == 404
    r = client.post("/batches", json={"group_ids": [g1, g2]})
    assert r.status_code == 201


def test_batch_and_isolation_race_commit_order_decides(client):
    """建批与隔离并发：以提交先后决定是拒绝建批还是让已建批次转为阻断。"""
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    from app.main import app
    results = {}
    broken = []

    def do_batch():
        with TestClient(app) as c:
            results["batch"] = c.post(
                "/batches", json={"group_ids": [g1, g2]}).status_code

    def do_isolate():
        with TestClient(app) as c:
            results["isolate"] = c.post(
                "/cells/C001/isolate", json={"reason": "并发隔离"}).status_code
            for _ in range(10):
                # 任何可见的批次都必须成员完整、快照完整、状态自洽
                for b in c.get("/batches").json()["batches"]:
                    try:
                        _assert_coherent_batch(b)
                    except AssertionError:
                        broken.append(b)

    threads = [threading.Thread(target=do_batch), threading.Thread(target=do_isolate)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not broken
    assert results["isolate"] == 201  # 隔离本身总是成功
    batches = client.get("/batches").json()["batches"]
    if results["batch"] == 201:
        # 建批先提交：批次产生，随后的隔离使其转为阻断；成员与快照不变
        assert len(batches) == 1
        b = batches[0]
        assert b["deliverable"] is False
        assert [(x["group_id"], x["cell_id"]) for x in b["blocked_groups"]] == [(g1, "C001")]
        assert b["group_ids"] == [g1, g2]
        _assert_coherent_batch(b)
    else:
        # 隔离先提交：建批整批拒绝，不产生批次也不留部分归属
        assert results["batch"] == 409
        assert batches == []


def _install_isolation_reader_gate(conn, gate, occurrence=1):
    """在连接上装一个一次性闸门：第 occurrence 次读取 isolations 表时，在该语句
    已建立读快照、尚未返回前挂起，直到 gate['release'] 被置位；进入挂起前
    置位 gate['inside']。用于复现"读到组阻断信息之后、读整批阻断汇总之前"
    另一事务提交新隔离的交错。
    """
    seen = {"n": 0, "should_block": False, "blocked": False}

    def trace(statement):
        if "FROM ISOLATIONS" in statement.upper():
            seen["n"] += 1
            if seen["n"] == occurrence:
                seen["should_block"] = True

    def progress():
        if seen["should_block"] and not seen["blocked"]:
            seen["blocked"] = True
            seen["should_block"] = False
            gate["inside"].set()
            gate["release"].wait(5)
        return 0

    conn.set_trace_callback(trace)
    conn.set_progress_handler(progress, 1)


def _commit_isolation(path, target, reason="交错隔离"):
    """写方线程：在独立连接上提交对 target 的隔离。"""
    import sqlite3
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO isolations (cell_id, reason, created_at)"
        " VALUES (?, ?, '2026-10-06T00:00:00+00:00')", (target, reason))
    conn.commit()
    conn.close()


def test_list_batches_reads_one_committed_snapshot(client):
    """GET /batches：整批可交付状态、阻断明细与各组快照取自同一已提交快照。

    交错：读请求读到组 1 的阻断信息（尚未隔离）后挂起；写方提交对 C001 的隔离；
    读请求随后才汇总整批阻断。没有同一快照时会拼出 deliverable=false 但各组
    blocked=false 的不存在状态。
    """
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    assert client.post("/batches", json={"group_ids": [g1, g2]}).status_code == 201
    from app import db
    from app.main import list_batches
    path = os.environ["DB_PATH"]

    gate = {"inside": threading.Event(), "release": threading.Event()}
    outcome = {}

    def reader():
        conn = db.get_conn()
        _install_isolation_reader_gate(conn, gate, occurrence=1)
        try:
            outcome["body"] = list_batches()
        finally:
            conn.set_trace_callback(None)
            conn.set_progress_handler(None, 0)

    rt = threading.Thread(target=reader)
    rt.start()
    assert gate["inside"].wait(5)
    _commit_isolation(path, "C001")
    gate["release"].set()
    rt.join(5)
    assert not rt.is_alive()

    batches = outcome["body"]["batches"]
    assert len(batches) == 1
    _assert_coherent_batch(batches[0])


def test_get_batch_reads_one_committed_snapshot(client):
    """GET /batches/{batch_id}：同样不允许跨快照拼装批次状态。"""
    seed_six(client)
    g1 = _make_group(client, ["C001", "C002", "C003"])
    g2 = _make_group(client, ["C004", "C005", "C006"])
    bid = client.post("/batches", json={"group_ids": [g1, g2]}).json()["batch_id"]
    from app import db
    from app.main import get_batch
    path = os.environ["DB_PATH"]

    gate = {"inside": threading.Event(), "release": threading.Event()}
    outcome = {}

    def reader():
        conn = db.get_conn()
        _install_isolation_reader_gate(conn, gate, occurrence=1)
        try:
            outcome["batch"] = get_batch(bid)
        finally:
            conn.set_trace_callback(None)
            conn.set_progress_handler(None, 0)

    rt = threading.Thread(target=reader)
    rt.start()
    assert gate["inside"].wait(5)
    _commit_isolation(path, "C001")
    gate["release"].set()
    rt.join(5)
    assert not rt.is_alive()

    _assert_coherent_batch(outcome["batch"])


# ---------- 发运 ----------

def _make_batch(client, cell_groups=(("C001", "C002", "C003"), ("C004", "C005", "C006"))):
    seed_six(client)
    gids = [_make_group(client, list(ids)) for ids in cell_groups]
    r = client.post("/batches", json={"group_ids": gids})
    assert r.status_code == 201, r.text
    return r.json()["batch_id"], gids


def _assert_coherent_shipment_state(b):
    """发运事实与当前隔离状态必须来自同一已提交快照。"""
    assert b["shipped"] == (b["shipped_at"] is not None)
    assert b["trace_required"] == (b["shipped"] and len(b["blocked_groups"]) > 0)


def test_ship_batch_success(client):
    bid, gids = _make_batch(client)
    # 发运前：未发运批次继续按原规则查询，无发运事实
    before = client.get(f"/batches/{bid}").json()
    assert before["shipped"] is False
    assert before["shipped_at"] is None
    assert before["trace_required"] is False
    assert before["deliverable"] is True

    r = client.post(f"/batches/{bid}/ship")
    assert r.status_code == 201, r.text
    ship = r.json()
    # 返回批次号、成员组号和各组原检测快照
    assert ship["batch_id"] == bid
    assert ship["shipped_at"]
    assert ship["group_ids"] == gids
    assert [g["group_id"] for g in ship["groups"]] == gids
    for g in ship["groups"]:
        assert g["cells"] == client.get(f"/groups/{g['group_id']}").json()["cells"]
        assert g["blocked"] is False

    # 批次查询呈现发运事实；成员未被隔离时不需追溯，其余口径不变
    after = client.get(f"/batches/{bid}").json()
    assert after["shipped"] is True
    assert after["shipped_at"] == ship["shipped_at"]
    assert after["trace_required"] is False
    assert after["deliverable"] is True
    assert after["group_ids"] == before["group_ids"]
    assert [g["cells"] for g in after["groups"]] == [g["cells"] for g in before["groups"]]
    lst = client.get("/batches").json()
    assert lst["count"] == 1
    assert lst["batches"][0]["shipped"] is True
    assert lst["batches"][0]["shipped_at"] == ship["shipped_at"]
    _assert_coherent_shipment_state(lst["batches"][0])


def test_ship_missing_batch_404(client):
    r = client.post("/batches/999/ship")
    assert r.status_code == 404
    assert "999" in r.json()["detail"]


def test_repeat_ship_rejected_record_immutable(client):
    bid, _ = _make_batch(client)
    first = client.post(f"/batches/{bid}/ship")
    assert first.status_code == 201
    # 重复发运明确拒绝，不改写首次发运记录
    again = client.post(f"/batches/{bid}/ship")
    assert again.status_code == 409
    assert "已发运" in again.json()["detail"]
    b = client.get(f"/batches/{bid}").json()
    assert b["shipped"] is True
    assert b["shipped_at"] == first.json()["shipped_at"]


def test_ship_blocked_batch_rejected_no_record(client):
    bid, gids = _make_batch(client)
    # 建批后隔离成员电芯 -> 批次当前被阻断，拒绝发运且不留发运记录
    isolate(client, "C005", "巡检发现鼓包")
    r = client.post(f"/batches/{bid}/ship")
    assert r.status_code == 409
    assert str(gids[1]) in r.json()["detail"]
    assert "C005" in r.json()["detail"]
    b = client.get(f"/batches/{bid}").json()
    assert b["shipped"] is False
    assert b["shipped_at"] is None
    assert b["trace_required"] is False
    assert b["deliverable"] is False


def test_isolation_after_ship_trace_required_record_retained(client):
    bid, gids = _make_batch(client)
    ship = client.post(f"/batches/{bid}/ship")
    assert ship.status_code == 201
    before = client.get(f"/batches/{bid}").json()

    # 发运先提交、隔离后到：发运记录保留，已发运批次显示需追溯
    isolate(client, "C005", "巡检发现鼓包")
    b = client.get(f"/batches/{bid}").json()
    assert b["shipped"] is True
    assert b["shipped_at"] == ship.json()["shipped_at"]
    assert b["trace_required"] is True
    assert b["deliverable"] is False
    # 受影响的组号、电芯 ID 和原因可追溯
    assert [(x["group_id"], x["cell_id"], x["reason"]) for x in b["blocked_groups"]] == [
        (gids[1], "C005", "巡检发现鼓包")]
    # 成员关系与各组原检测快照不改写
    assert b["group_ids"] == before["group_ids"]
    assert [g["cells"] for g in b["groups"]] == [g["cells"] for g in before["groups"]]
    # 批次列表口径一致
    lst = client.get("/batches").json()
    assert lst["batches"][0]["shipped"] is True
    assert lst["batches"][0]["trace_required"] is True
    # 已发运批次不可再发运
    assert client.post(f"/batches/{bid}/ship").status_code == 409


def test_shipment_record_immutable_triggers(client):
    bid, _ = _make_batch(client)
    assert client.post(f"/batches/{bid}/ship").status_code == 201
    import sqlite3
    from app import db
    conn = db.get_conn()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "UPDATE shipments SET shipped_at = '2099-01-01' WHERE batch_id = ?", (bid,))
    conn.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM shipments WHERE batch_id = ?", (bid,))
    conn.rollback()
    # 触发器拦截后原记录保持不变
    assert client.get(f"/batches/{bid}").json()["shipped"] is True


def test_uncommitted_shipment_invisible_to_other_requests(client):
    """发运事务未提交时，其他请求看不到发运事实；提交后一次性可见。"""
    bid, _ = _make_batch(client)
    from app import db
    conn = db.get_conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO shipments (batch_id, shipped_at)"
        " VALUES (?, '2026-10-06T01:00:00+00:00')", (bid,))
    # 未提交：批次列表 / 按号查询都看不到发运事实
    assert client.get(f"/batches/{bid}").json()["shipped"] is False
    assert client.get("/batches").json()["batches"][0]["shipped"] is False
    # 提交后：发运事实一次性可见
    conn.commit()
    b = client.get(f"/batches/{bid}").json()
    assert b["shipped"] is True
    assert b["shipped_at"] == "2026-10-06T01:00:00+00:00"


def test_rolled_back_shipment_leaves_no_trace(client):
    """发运回滚后毫无痕迹，批次仍可正常发运。"""
    bid, _ = _make_batch(client)
    from app import db
    conn = db.get_conn()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO shipments (batch_id, shipped_at)"
        " VALUES (?, '2026-10-06T01:00:00+00:00')", (bid,))
    conn.rollback()
    assert client.get(f"/batches/{bid}").json()["shipped"] is False
    r = client.post(f"/batches/{bid}/ship")
    assert r.status_code == 201
    assert client.get(f"/batches/{bid}").json()["shipped"] is True


def test_ship_and_isolation_race_commit_order_decides(client):
    """发运与隔离并发：以提交先后决定是拒绝发运还是保留记录并转为需追溯。"""
    bid, gids = _make_batch(client)
    from app.main import app
    results = {}
    broken = []

    def do_ship():
        with TestClient(app) as c:
            results["ship"] = c.post(f"/batches/{bid}/ship").status_code

    def do_isolate():
        with TestClient(app) as c:
            results["isolate"] = c.post(
                "/cells/C001/isolate", json={"reason": "并发隔离"}).status_code
            for _ in range(10):
                # 任何可见的批次都必须发运事实与隔离状态自洽
                for b in c.get("/batches").json()["batches"]:
                    try:
                        _assert_coherent_batch(b)
                        _assert_coherent_shipment_state(b)
                    except AssertionError:
                        broken.append(b)

    threads = [threading.Thread(target=do_ship), threading.Thread(target=do_isolate)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not broken
    assert results["isolate"] == 201  # 隔离本身总是成功
    b = client.get(f"/batches/{bid}").json()
    if results["ship"] == 201:
        # 发运先提交：记录保留，随后的隔离使已发运批次显示需追溯
        assert b["shipped"] is True
        assert b["trace_required"] is True
        assert [(x["group_id"], x["cell_id"]) for x in b["blocked_groups"]] == [
            (gids[0], "C001")]
    else:
        # 隔离先提交：发运被拒绝，不留发运记录
        assert results["ship"] == 409
        assert b["shipped"] is False
        assert b["shipped_at"] is None
        assert b["trace_required"] is False
        assert b["deliverable"] is False


def _commit_shipment_then_isolation(path, batch_id, target):
    """写方线程：在独立连接上先提交发运记录，再提交对 target 的隔离。
    复现"发运先提交、隔离后提交"两次提交落在同一读请求窗口内。"""
    import sqlite3
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO shipments (batch_id, shipped_at)"
        " VALUES (?, '2026-10-06T01:00:00+00:00')", (batch_id,))
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        "INSERT INTO isolations (cell_id, reason, created_at)"
        " VALUES (?, ?, '2026-10-06T02:00:00+00:00')", (target, "发运后隔离"))
    conn.commit()
    conn.close()


def test_get_batch_shipment_reads_one_committed_snapshot(client):
    """GET /batches/{batch_id}：发运事实与当前隔离状态取自同一已提交快照。

    交错：读请求读到组阻断信息（尚未发运、尚未隔离）后挂起；写方先提交发运、
    再提交对成员电芯的隔离；读请求随后才汇总阻断并读发运记录。合法结果只有
    发运前 / 发运后隔离前 / 隔离后三种完整状态；绝不允许拼出 shipped=false
    却 deliverable=false（发运先于隔离提交，该组合从未作为已提交状态存在）。
    """
    bid, _ = _make_batch(client)
    from app import db
    from app.main import get_batch
    path = os.environ["DB_PATH"]

    gate = {"inside": threading.Event(), "release": threading.Event()}
    outcome = {}

    def reader():
        conn = db.get_conn()
        _install_isolation_reader_gate(conn, gate, occurrence=1)
        try:
            outcome["batch"] = get_batch(bid)
        finally:
            conn.set_trace_callback(None)
            conn.set_progress_handler(None, 0)

    rt = threading.Thread(target=reader)
    rt.start()
    assert gate["inside"].wait(5)
    _commit_shipment_then_isolation(path, bid, "C001")
    gate["release"].set()
    rt.join(5)
    assert not rt.is_alive()

    b = outcome["batch"]
    _assert_coherent_batch(b)
    _assert_coherent_shipment_state(b)
    if not b["shipped"]:
        # 发运前的快照里不可能看到发运后才提交的隔离
        assert b["deliverable"] is True
        assert b["blocked_groups"] == []

