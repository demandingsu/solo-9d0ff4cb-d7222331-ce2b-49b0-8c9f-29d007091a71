"""SQLite 连接与事务管理。

库结构：
- source_subtitles: 原片字幕（素材号, 字幕号 为主键），version 用于乐观并发控制
- selections:       选段，按 seq 首尾相接拼成成片，offset_ms 为该段在成片中的起点
- final_rows:       已保存的成片字幕行（页面读取的就是这张表）
- meta:             单行表，保存数据版本号 data_rev：整批换数据（重传 CSV）时 +1，
                     校时页把读出时的 data_rev 随表单回传，换段后旧页面提交会被拒绝
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager

# 写事务拿锁最多等这么久；重算已改为场外构建，写事务都很短，
# 这个超时只用于吸收切换瞬间的竞争，不再让长重算阻塞校时保存。
BUSY_TIMEOUT_MS = 10_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS source_subtitles (
    material_id  TEXT    NOT NULL,
    subtitle_no  INTEGER NOT NULL,
    start_ms     INTEGER NOT NULL,
    end_ms       INTEGER NOT NULL,
    text         TEXT    NOT NULL,
    version      INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (material_id, subtitle_no),
    CHECK (start_ms >= 0 AND start_ms < end_ms)
);

CREATE TABLE IF NOT EXISTS selections (
    seq         INTEGER PRIMARY KEY,   -- 成片顺序，从 1 开始
    material_id TEXT    NOT NULL,
    start_ms    INTEGER NOT NULL,
    end_ms      INTEGER NOT NULL,
    offset_ms   INTEGER NOT NULL,      -- 本段在成片时间轴上的起点
    CHECK (start_ms >= 0 AND start_ms < end_ms)
);

CREATE TABLE IF NOT EXISTS final_rows (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    selection_seq  INTEGER NOT NULL REFERENCES selections(seq) ON DELETE CASCADE,
    material_id    TEXT    NOT NULL,
    subtitle_no    INTEGER NOT NULL,
    final_start_ms INTEGER NOT NULL,
    final_end_ms   INTEGER NOT NULL,
    src_start_ms   INTEGER NOT NULL,   -- 重算时原字幕的起止（展示用快照）
    src_end_ms     INTEGER NOT NULL,
    start_clipped  INTEGER NOT NULL DEFAULT 0,  -- 起点被本段切点裁掉
    end_clipped    INTEGER NOT NULL DEFAULT 0,  -- 终点被本段切点裁掉
    text           TEXT    NOT NULL,
    UNIQUE (selection_seq, material_id, subtitle_no)
);

CREATE TABLE IF NOT EXISTS meta (
    id       INTEGER PRIMARY KEY CHECK (id = 1),
    data_rev INTEGER NOT NULL
);
"""


def db_path() -> str:
    return os.environ.get("APP_DB", os.path.join("data", "app.db"))


def connect() -> sqlite3.Connection:
    path = db_path()
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL：读不阻塞写、写不阻塞读。场外重算走只读连接，校时写事务可照常提交。
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def init() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta (id, data_rev) VALUES (1, 1)"
        )
    finally:
        conn.close()


def get_data_rev(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT data_rev FROM meta WHERE id = 1").fetchone()
    return row["data_rev"] if row is not None else 1


def bump_data_rev(conn: sqlite3.Connection) -> int:
    """整批换数据后调用，返回新版本号。"""
    conn.execute("UPDATE meta SET data_rev = data_rev + 1 WHERE id = 1")
    return get_data_rev(conn)


@contextmanager
def transaction():
    """写事务：任一一步失败整体回滚，原字幕和成片行都保留上次结果。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
