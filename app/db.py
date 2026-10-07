"""SQLite 存储层：每线程独立连接（WAL 模式）+ 写锁。

- 写路径（录入 / 复测 / 确认 / 隔离 / 建批 / 发运 / 撤销）在 write_lock 内以
  BEGIN IMMEDIATE 开启单事务，保证整组占用与快照写入、整批归属登记、发运记录、
  撤销记录与占用释放的原子性；
- 每个请求线程使用自己的连接，读请求因此只能看到**已提交**的数据：
  确认事务中途写入的组记录、检测快照与临时占用对其他请求不可见，
  提交成功的瞬间整组一次性可见，回滚的确认对外不留任何痕迹；
- WAL 模式下读写互不阻塞，并发查询不会干扰确认事务。
"""
import os
import sqlite3
import threading
from contextlib import contextmanager

DEFAULT_DB_PATH = "battery.db"

MAX_CELLS = 12  # 最多录入 12 节待选电芯

SCHEMA = """
CREATE TABLE IF NOT EXISTS cells (
    cell_id      TEXT PRIMARY KEY,
    chemistry    TEXT NOT NULL,
    capacity     REAL NOT NULL,
    resistance   REAL NOT NULL,
    test_version TEXT NOT NULL,
    occupied     INTEGER NOT NULL DEFAULT 0,
    group_id     INTEGER
);

CREATE TABLE IF NOT EXISTS groups (
    group_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chemistry        TEXT NOT NULL,
    series_count     INTEGER NOT NULL,
    limited_capacity REAL NOT NULL,
    created_at       TEXT NOT NULL
);

-- 检测快照：确认时写入，之后不可改写
CREATE TABLE IF NOT EXISTS group_cells (
    group_id     INTEGER NOT NULL REFERENCES groups(group_id),
    cell_id      TEXT NOT NULL,
    capacity     REAL NOT NULL,
    resistance   REAL NOT NULL,
    test_version TEXT NOT NULL,
    PRIMARY KEY (group_id, cell_id)
);

-- 安全隔离：每节电芯最多一条记录，一旦写入不可撤销（复测也不解除）
CREATE TABLE IF NOT EXISTS isolations (
    cell_id    TEXT PRIMARY KEY REFERENCES cells(cell_id),
    reason     TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_groups_no_update BEFORE UPDATE ON groups
BEGIN SELECT RAISE(ABORT, 'groups are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_groups_no_delete BEFORE DELETE ON groups
BEGIN SELECT RAISE(ABORT, 'groups are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_group_cells_no_update BEFORE UPDATE ON group_cells
BEGIN SELECT RAISE(ABORT, 'group snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_group_cells_no_delete BEFORE DELETE ON group_cells
BEGIN SELECT RAISE(ABORT, 'group snapshots are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_isolations_no_update BEFORE UPDATE ON isolations
BEGIN SELECT RAISE(ABORT, 'isolations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_isolations_no_delete BEFORE DELETE ON isolations
BEGIN SELECT RAISE(ABORT, 'isolations are immutable'); END;

-- 交付批次：建批时写入，批次头不可改写
CREATE TABLE IF NOT EXISTS batches (
    batch_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    chemistry  TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 批次成员：建批时登记，成员关系固定不可改写；批次撤销后这些行原样保留
-- （仅历史归属，不再代表占用，因此这里不再有 group_id 唯一索引）
CREATE TABLE IF NOT EXISTS batch_groups (
    batch_id INTEGER NOT NULL REFERENCES batches(batch_id),
    group_id INTEGER NOT NULL REFERENCES groups(group_id),
    PRIMARY KEY (batch_id, group_id)
);

-- 当前占用表：同一组号在全库最多归属一个**未撤销**批次（主键兜底）。
-- 建批时随成员关系一起插入；撤销时整批删除（这不是历史改写，只是释放占用，
-- 批次头、原成员关系 batch_groups 与检测快照都原样保留）。
CREATE TABLE IF NOT EXISTS active_batch_groups (
    batch_id INTEGER NOT NULL REFERENCES batches(batch_id),
    group_id INTEGER NOT NULL REFERENCES groups(group_id),
    PRIMARY KEY (group_id)
);

CREATE TRIGGER IF NOT EXISTS trg_active_batch_groups_no_update BEFORE UPDATE ON active_batch_groups
BEGIN SELECT RAISE(ABORT, 'active ownership cannot be updated'); END;

CREATE TRIGGER IF NOT EXISTS trg_batches_no_update BEFORE UPDATE ON batches
BEGIN SELECT RAISE(ABORT, 'batches are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_batches_no_delete BEFORE DELETE ON batches
BEGIN SELECT RAISE(ABORT, 'batches are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_batch_groups_no_update BEFORE UPDATE ON batch_groups
BEGIN SELECT RAISE(ABORT, 'batch membership is immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_batch_groups_no_delete BEFORE DELETE ON batch_groups
BEGIN SELECT RAISE(ABORT, 'batch membership is immutable'); END;

-- 发运记录：每批次最多一条（主键兜底），发运时间与批次关联一旦写入不可改写
CREATE TABLE IF NOT EXISTS shipments (
    batch_id   INTEGER PRIMARY KEY REFERENCES batches(batch_id),
    shipped_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_shipments_no_update BEFORE UPDATE ON shipments
BEGIN SELECT RAISE(ABORT, 'shipments are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_shipments_no_delete BEFORE DELETE ON shipments
BEGIN SELECT RAISE(ABORT, 'shipments are immutable'); END;

-- 批次撤销：每批次最多一条（主键兜底），只允许未发运批次撤销。
-- 撤销时间与原因一旦写入不可改写；撤销本身也不可撤销（只能重建新批次）。
CREATE TABLE IF NOT EXISTS cancellations (
    batch_id    INTEGER PRIMARY KEY REFERENCES batches(batch_id),
    reason      TEXT NOT NULL,
    cancelled_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_cancellations_no_update BEFORE UPDATE ON cancellations
BEGIN SELECT RAISE(ABORT, 'cancellations are immutable'); END;
CREATE TRIGGER IF NOT EXISTS trg_cancellations_no_delete BEFORE DELETE ON cancellations
BEGIN SELECT RAISE(ABORT, 'cancellations are immutable'); END;
"""

# 确认/写入路径上的互斥锁，配合 BEGIN IMMEDIATE 保证事务原子性
write_lock = threading.Lock()

_local = threading.local()  # 每个请求线程一条独立连接
_conns: list[sqlite3.Connection] = []  # 已建连接清单，reset 时统一关闭
_conns_lock = threading.Lock()
_init_lock = threading.Lock()
_initialized: set[str] = set()  # 已完成建库初始化（WAL + 表结构）的路径
_shared: sqlite3.Connection | None = None  # :memory: 无法跨连接共享，退化为单连接


def _db_path() -> str:
    return os.environ.get("DB_PATH", DEFAULT_DB_PATH)


def _new_conn(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    with _init_lock:
        if path not in _initialized:
            # 建库初始化只做一次：WAL 模式与表结构都持久化在库文件中，
            # 后续连接不再执行 DDL，避免与进行中的写事务争锁
            conn.execute("PRAGMA journal_mode = WAL")
            # 旧版把"全库唯一归属"做成了 batch_groups(group_id) 唯一索引；
            # 撤销要保留历史成员关系但释放占用，独占性改由 active_batch_groups
            # 承担。检测到旧索引即说明是旧库升级：先删索引、建新表，再把旧库中
            # 全部批次（旧库不存在撤销记录）回填为当前占用。仅在升级时回填一次，
            # 避免重启后把已撤销批次释放的组重新占用。
            legacy = conn.execute(
                "SELECT 1 FROM sqlite_master"
                " WHERE type = 'index' AND name = 'idx_batch_groups_group'"
            ).fetchone()
            if legacy is not None:
                conn.execute("DROP INDEX idx_batch_groups_group")
            conn.executescript(SCHEMA)
            if legacy is not None:
                conn.execute(
                    "INSERT INTO active_batch_groups (batch_id, group_id)"
                    " SELECT bg.batch_id, bg.group_id FROM batch_groups bg"
                    " WHERE NOT EXISTS ("
                    "     SELECT 1 FROM cancellations c WHERE c.batch_id = bg.batch_id)"
                )
            conn.commit()  # 迁移 DDL/DML 显式提交，避免连接上残留打开的事务
            _initialized.add(path)
    return conn


def get_conn() -> sqlite3.Connection:
    """返回当前请求线程专属的连接（首次使用时创建）。

    读请求与确认事务使用不同连接，因此只能看到已提交的完整快照，
    看不到确认事务未提交的中间状态。
    """
    global _shared
    path = _db_path()
    if path == ":memory:":
        with _conns_lock:
            if _shared is None:
                _shared = _new_conn(path)
            return _shared
    conn = getattr(_local, "conn", None)
    if conn is None or getattr(_local, "path", None) != path:
        conn = _new_conn(path)
        _local.conn = conn
        _local.path = path
        with _conns_lock:
            _conns.append(conn)
    return conn


@contextmanager
def read_snapshot(conn: sqlite3.Connection):
    """把一次查询里的多条 SELECT 包进同一个只读事务。

    保证这次请求看到同一个已提交快照；WAL 模式下既不阻塞写事务，
    也不受写事务影响。
    """
    conn.execute("BEGIN")
    try:
        yield
    finally:
        conn.rollback()  # 只读事务：回滚即结束，不产生任何修改


def reset() -> None:
    """关闭并丢弃所有连接（测试用，配合 DB_PATH 环境变量换新库）。"""
    global _local, _shared
    with _conns_lock:
        for conn in _conns:
            try:
                conn.close()
            except Exception:
                pass
        _conns.clear()
        if _shared is not None:
            try:
                _shared.close()
            except Exception:
                pass
            _shared = None
    with _init_lock:
        _initialized.clear()
    _local = threading.local()
