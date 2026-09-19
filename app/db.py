"""SQLite 连接与 schema。

设计要点：
- WAL 模式 + busy_timeout，支持多线程并发读写；
- 所有写操作通过 transaction() 以 BEGIN IMMEDIATE 开启，保证写者串行；
- 幂等键、确认状态等依靠唯一约束与条件 UPDATE 保证并发安全。
"""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL DEFAULT 'nurse' CHECK (role IN ('nurse', 'leader')),
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mother (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    room        TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS baby (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mother_id   INTEGER NOT NULL REFERENCES mother(id),
    name        TEXT NOT NULL,
    sex         TEXT,
    birth_at    TEXT,
    notes       TEXT,
    created_at  TEXT NOT NULL
);

-- 班次：事件只能落在记录人自己的班次窗口内
CREATE TABLE IF NOT EXISTS shift (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    staff_id    INTEGER NOT NULL REFERENCES staff(id),
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    status      TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    created_at  TEXT NOT NULL
);

-- 统一照护时间线：喂养/睡眠/排泄/用药协助/情绪观察/护理备注
-- occurred_at 为实际发生时间，recorded_at 为录入时间（迟到补录两者都保留）
CREATE TABLE IF NOT EXISTS care_event (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key       TEXT NOT NULL UNIQUE,
    subject_type          TEXT NOT NULL CHECK (subject_type IN ('mother', 'baby')),
    subject_id            INTEGER NOT NULL,
    category              TEXT NOT NULL CHECK (category IN
                              ('feeding', 'sleep', 'excretion', 'medication', 'mood', 'note')),
    occurred_at           TEXT NOT NULL,
    recorded_at           TEXT NOT NULL,
    is_backfill           INTEGER NOT NULL DEFAULT 0,
    recorded_by           INTEGER NOT NULL REFERENCES staff(id),
    shift_id              INTEGER NOT NULL REFERENCES shift(id),
    payload_json          TEXT NOT NULL DEFAULT '{}',
    needs_followup        INTEGER NOT NULL DEFAULT 0,
    is_important          INTEGER NOT NULL DEFAULT 0,
    is_abnormal           INTEGER NOT NULL DEFAULT 0,
    status                TEXT NOT NULL DEFAULT 'active'
                              CHECK (status IN ('active', 'corrected', 'voided')),
    center_confirmed      INTEGER NOT NULL DEFAULT 0,
    center_confirmed_by   INTEGER REFERENCES staff(id),
    center_confirmed_at   TEXT,
    shareable_with_family INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_subject
    ON care_event (subject_type, subject_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_event_shift ON care_event (shift_id);

-- 重要提醒签收：未签收前提醒不消失
CREATE TABLE IF NOT EXISTS acknowledgement (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    care_event_id INTEGER NOT NULL REFERENCES care_event(id),
    staff_id      INTEGER NOT NULL REFERENCES staff(id),
    acked_at      TEXT NOT NULL,
    UNIQUE (care_event_id, staff_id)
);

-- 交接单：一个班次只能有一张
CREATE TABLE IF NOT EXISTS handover (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id          INTEGER NOT NULL UNIQUE REFERENCES shift(id),
    outgoing_staff_id INTEGER NOT NULL REFERENCES staff(id),
    incoming_staff_id INTEGER NOT NULL REFERENCES staff(id),
    period_start      TEXT NOT NULL,
    period_end        TEXT NOT NULL,
    status            TEXT NOT NULL DEFAULT 'draft'
                          CHECK (status IN ('draft', 'submitted', 'signed')),
    submitted_at      TEXT,
    signed_at         TEXT,
    created_at        TEXT NOT NULL
);

-- 交接事项：接班人逐项确认；确认状态迁移用条件 UPDATE 保证并发只成功一次
CREATE TABLE IF NOT EXISTS handover_item (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id   INTEGER NOT NULL REFERENCES handover(id),
    item_type     TEXT NOT NULL CHECK (item_type IN ('followup', 'reminder', 'manual')),
    care_event_id INTEGER REFERENCES care_event(id),
    description   TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'confirmed')),
    confirmed_by  INTEGER REFERENCES staff(id),
    confirmed_at  TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_item_handover ON handover_item (handover_id);

-- 更正事件：已签署交接的事实差异只能通过更正说明
CREATE TABLE IF NOT EXISTS correction (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    handover_id      INTEGER REFERENCES handover(id),
    care_event_id    INTEGER REFERENCES care_event(id),
    action           TEXT NOT NULL CHECK (action IN ('amend', 'void', 'add')),
    reason           TEXT NOT NULL,
    new_payload_json TEXT,
    corrected_by     INTEGER NOT NULL REFERENCES staff(id),
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_correction_handover ON correction (handover_id);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(
        db_path, check_same_thread=False, isolation_level=None, timeout=30.0
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE 事务：进入即取得写锁，串行化并发写者。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
