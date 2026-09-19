"""SQLite 数据访问层。

设计要点：
- 所有时间统一存 UTC ISO-8601 字符串，字典序即时间序，可直接比较。
- 连接采用 autocommit（isolation_level=None），需要多步原子操作时
  显式使用 immediate_tx()（BEGIN IMMEDIATE），写操作在 SQLite 上串行化，
  配合 busy_timeout 避免并发测试中出现 SQLITE_BUSY。
- 幂等性依赖唯一约束（care_events.client_request_id、
  acknowledgements(event_id, staff_id)、handovers.shift_id 等），
  并发下由数据库保证不产生重复记录。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('nurse', 'lead_nurse')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mothers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    room       TEXT,
    notes      TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS babies (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mother_id  INTEGER NOT NULL REFERENCES mothers(id),
    name       TEXT NOT NULL,
    birth_date TEXT,
    notes      TEXT,
    created_at TEXT NOT NULL
);

-- 班次：某护理人员负责的时间段（可跨午夜，用绝对时间表示）
CREATE TABLE IF NOT EXISTS shifts (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    start_at TEXT NOT NULL,
    end_at   TEXT NOT NULL,
    label    TEXT,
    CHECK (end_at > start_at)
);

-- 照护事件：母亲与婴儿共用同一条时间线
CREATE TABLE IF NOT EXISTS care_events (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    client_request_id  TEXT NOT NULL UNIQUE,        -- 幂等键：重复提交不产生第二条记录
    subject_type       TEXT NOT NULL CHECK (subject_type IN ('mother', 'baby')),
    subject_id         INTEGER NOT NULL,
    category           TEXT NOT NULL CHECK (category IN
        ('feeding', 'sleep', 'excretion', 'medication', 'mood', 'note')),
    occurred_at        TEXT NOT NULL,               -- 实际发生时间
    recorded_at        TEXT NOT NULL,               -- 录入时间（迟到补录时两者不同）
    recorded_by        INTEGER NOT NULL REFERENCES staff(id),
    shift_id           INTEGER NOT NULL REFERENCES shifts(id),
    payload            TEXT NOT NULL DEFAULT '{}',  -- JSON 明细
    is_important       INTEGER NOT NULL DEFAULT 0,  -- 重要提醒：被明确接收前不消失
    requires_followup  INTEGER NOT NULL DEFAULT 0,  -- 需要持续关注
    abnormal           INTEGER NOT NULL DEFAULT 0,  -- 异常变化
    shareable          INTEGER NOT NULL DEFAULT 0,  -- 允许对家属共享
    center_confirmed   INTEGER NOT NULL DEFAULT 0,  -- 已经中心（护理组长）确认
    center_confirmed_by INTEGER REFERENCES staff(id),
    center_confirmed_at TEXT,
    resolved_at        TEXT,                        -- 持续关注事项的关闭时间
    resolved_by        INTEGER REFERENCES staff(id)
);
CREATE INDEX IF NOT EXISTS idx_events_subject ON care_events(subject_type, subject_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_events_shift   ON care_events(shift_id);

-- 重要提醒的接收记录（明确接收后才算数）
CREATE TABLE IF NOT EXISTS acknowledgements (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL REFERENCES care_events(id),
    staff_id INTEGER NOT NULL REFERENCES staff(id),
    acked_at TEXT NOT NULL,
    UNIQUE (event_id, staff_id)
);

-- 交接单：一个班次只能有一张
CREATE TABLE IF NOT EXISTS handovers (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id          INTEGER NOT NULL UNIQUE REFERENCES shifts(id),
    outgoing_staff_id INTEGER NOT NULL REFERENCES staff(id),
    incoming_staff_id INTEGER NOT NULL REFERENCES staff(id),
    period_start      TEXT NOT NULL,
    period_end        TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'signed')),
    created_at        TEXT NOT NULL,
    signed_at         TEXT
);

-- 交接事项：接班人逐项确认
CREATE TABLE IF NOT EXISTS handover_items (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id  INTEGER NOT NULL REFERENCES handovers(id),
    kind         TEXT NOT NULL CHECK (kind IN ('important_reminder', 'open_followup')),
    event_id     INTEGER NOT NULL REFERENCES care_events(id),
    status       TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed')),
    confirmed_by INTEGER REFERENCES staff(id),
    confirmed_at TEXT,
    UNIQUE (handover_id, event_id)
);

-- 更正事件：已签署的交接只能通过更正说明差异，原记录不变
CREATE TABLE IF NOT EXISTS corrections (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id INTEGER NOT NULL REFERENCES handovers(id),
    event_id    INTEGER REFERENCES care_events(id),
    description TEXT NOT NULL,
    created_by  INTEGER NOT NULL REFERENCES staff(id),
    created_at  TEXT NOT NULL
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def immediate_tx(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 事务：进入即取写锁，保证检查-写入序列的原子性。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
