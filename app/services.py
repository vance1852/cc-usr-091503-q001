"""业务逻辑层：所有规则集中在服务函数中，路由层只做参数解析与鉴权。"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from fastapi import HTTPException

from . import db
from .schemas import EventIn
from .utils import CATEGORY_ZH, now_utc, parse_iso, to_iso

# 录入时间晚于发生时间超过该阈值即视为迟到补录
BACKFILL_THRESHOLD = timedelta(minutes=10)
# 发生时间允许轻微超前（时钟误差），超出则拒绝
FUTURE_TOLERANCE = timedelta(minutes=5)

EVENT_BOOL_FIELDS = (
    "is_backfill",
    "needs_followup",
    "is_important",
    "is_abnormal",
    "center_confirmed",
    "shareable_with_family",
)


# ---------------------------------------------------------------- 序列化

def event_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["payload"] = json.loads(d.pop("payload_json"))
    for f in EVENT_BOOL_FIELDS:
        d[f] = bool(d[f])
    return d


def correction_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    raw = d.pop("new_payload_json")
    d["new_payload"] = json.loads(raw) if raw is not None else None
    return d


def item_dict(row: sqlite3.Row) -> dict:
    return dict(row)


# ---------------------------------------------------------------- 基础查询

def get_staff(conn, staff_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM staff WHERE id = ?", (staff_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"护理人员 #{staff_id} 不存在")
    return row


def get_event(conn, event_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM care_event WHERE id = ?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"照护事件 #{event_id} 不存在")
    return row


def get_handover(conn, handover_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM handover WHERE id = ?", (handover_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"交接单 #{handover_id} 不存在")
    return row


def _check_subject(conn, subject_type: str, subject_id: int) -> None:
    table = "mother" if subject_type == "mother" else "baby"
    row = conn.execute(f"SELECT id FROM {table} WHERE id = ?", (subject_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"{'母亲' if subject_type == 'mother' else '婴儿'}档案 #{subject_id} 不存在")


def _normalize_dt(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _describe_event(e: sqlite3.Row) -> str:
    note = json.loads(e["payload_json"]).get("note") or ""
    base = f"{CATEGORY_ZH[e['category']]}事件#{e['id']} @ {e['occurred_at']}"
    return f"{base}：{note}" if note else base


# ---------------------------------------------------------------- 档案

def create_staff(conn, data) -> dict:
    cur = conn.execute(
        "INSERT INTO staff (name, role, created_at) VALUES (?, ?, ?)",
        (data.name, data.role, to_iso(now_utc())),
    )
    return dict(get_staff(conn, cur.lastrowid))


def create_mother(conn, data) -> dict:
    cur = conn.execute(
        "INSERT INTO mother (name, room, notes, created_at) VALUES (?, ?, ?, ?)",
        (data.name, data.room, data.notes, to_iso(now_utc())),
    )
    return dict(conn.execute("SELECT * FROM mother WHERE id = ?", (cur.lastrowid,)).fetchone())


def create_baby(conn, data) -> dict:
    if conn.execute("SELECT id FROM mother WHERE id = ?", (data.mother_id,)).fetchone() is None:
        raise HTTPException(404, f"母亲档案 #{data.mother_id} 不存在")
    cur = conn.execute(
        "INSERT INTO baby (mother_id, name, sex, birth_at, notes, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (
            data.mother_id,
            data.name,
            data.sex,
            to_iso(data.birth_at) if data.birth_at else None,
            data.notes,
            to_iso(now_utc()),
        ),
    )
    return dict(conn.execute("SELECT * FROM baby WHERE id = ?", (cur.lastrowid,)).fetchone())


def mother_detail(conn, mother_id: int) -> dict:
    row = conn.execute("SELECT * FROM mother WHERE id = ?", (mother_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"母亲档案 #{mother_id} 不存在")
    d = dict(row)
    d["babies"] = [
        dict(r) for r in conn.execute("SELECT * FROM baby WHERE mother_id = ?", (mother_id,))
    ]
    return d


def baby_detail(conn, baby_id: int) -> dict:
    row = conn.execute("SELECT * FROM baby WHERE id = ?", (baby_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"婴儿档案 #{baby_id} 不存在")
    return dict(row)


# ---------------------------------------------------------------- 班次

def start_shift(conn, staff, data) -> dict:
    target = get_staff(conn, data.staff_id)
    if target["id"] != staff["id"] and staff["role"] != "leader":
        raise HTTPException(403, "只能为本人开班")
    started = to_iso(_normalize_dt(data.started_at)) if data.started_at else to_iso(now_utc())
    cur = conn.execute(
        "INSERT INTO shift (staff_id, started_at, created_at) VALUES (?, ?, ?)",
        (target["id"], started, to_iso(now_utc())),
    )
    return dict(conn.execute("SELECT * FROM shift WHERE id = ?", (cur.lastrowid,)).fetchone())


def end_shift(conn, staff, shift_id: int, data) -> dict:
    row = conn.execute("SELECT * FROM shift WHERE id = ?", (shift_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"班次 #{shift_id} 不存在")
    if row["staff_id"] != staff["id"] and staff["role"] != "leader":
        raise HTTPException(403, "只能结束本人班次")
    if row["status"] == "closed":
        raise HTTPException(409, "班次已结束")
    ended = to_iso(_normalize_dt(data.ended_at)) if data.ended_at else to_iso(now_utc())
    if ended < row["started_at"]:
        raise HTTPException(422, "结束时间不能早于开始时间")
    conn.execute(
        "UPDATE shift SET ended_at = ?, status = 'closed' WHERE id = ?", (ended, shift_id)
    )
    return dict(conn.execute("SELECT * FROM shift WHERE id = ?", (shift_id,)).fetchone())


# ---------------------------------------------------------------- 照护事件

def _insert_event(conn, staff_id: int, shift_id: int, data: EventIn,
                  occurred: datetime, now: datetime) -> Tuple[sqlite3.Row, bool]:
    """写入事件；幂等键冲突时返回已有记录（重复提交不产生第二条）。"""
    recorded_iso = to_iso(now)
    occurred_iso = to_iso(occurred)
    is_backfill = 1 if (now - occurred) > BACKFILL_THRESHOLD else 0
    try:
        cur = conn.execute(
            """INSERT INTO care_event
               (idempotency_key, subject_type, subject_id, category, occurred_at, recorded_at,
                is_backfill, recorded_by, shift_id, payload_json,
                needs_followup, is_important, is_abnormal, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data.idempotency_key, data.subject_type, data.subject_id, data.category,
                occurred_iso, recorded_iso, is_backfill, staff_id, shift_id,
                json.dumps(data.payload, ensure_ascii=False),
                int(data.needs_followup), int(data.is_important), int(data.is_abnormal),
                recorded_iso,
            ),
        )
        return conn.execute(
            "SELECT * FROM care_event WHERE id = ?", (cur.lastrowid,)
        ).fetchone(), True
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM care_event WHERE idempotency_key = ?", (data.idempotency_key,)
        ).fetchone()
        if existing is None:  # 其他约束错误，向上抛
            raise
        same_content = (
            existing["subject_type"] == data.subject_type
            and existing["subject_id"] == data.subject_id
            and existing["category"] == data.category
            and existing["occurred_at"] == occurred_iso
        )
        if not same_content:
            raise HTTPException(409, "幂等键已被不同内容的事件使用")
        return existing, False


def record_event(conn, staff, data: EventIn) -> Tuple[dict, bool]:
    """交班人只能提交自己负责时段内的事实；已签署时段拒绝直接写入。"""
    occurred = _normalize_dt(data.occurred_at)
    now = now_utc()
    if occurred > now + FUTURE_TOLERANCE:
        raise HTTPException(422, "发生时间不能晚于当前时间")
    _check_subject(conn, data.subject_type, data.subject_id)
    occurred_iso = to_iso(occurred)
    with db.transaction(conn):
        shift = conn.execute(
            """SELECT * FROM shift
               WHERE staff_id = ? AND started_at <= ?
                 AND (ended_at IS NULL OR ended_at >= ?)
               ORDER BY started_at DESC LIMIT 1""",
            (staff["id"], occurred_iso, occurred_iso),
        ).fetchone()
        if shift is None:
            raise HTTPException(403, "发生时间不在你负责的班次时段内，只能提交本人班次内的事实")
        locked = conn.execute(
            "SELECT id FROM handover WHERE shift_id = ? AND status = 'signed'",
            (shift["id"],),
        ).fetchone()
        if locked is not None:
            raise HTTPException(
                409, f"该时段交接单 #{locked['id']} 已签署，事实差异请通过更正事件提交"
            )
        row, created = _insert_event(conn, staff["id"], shift["id"], data, occurred, now)
    return event_dict(row), created


def timeline(conn, subject_type: str, subject_id: int,
             start: Optional[str], end: Optional[str]) -> list:
    _check_subject(conn, subject_type, subject_id)
    sql = ("SELECT * FROM care_event WHERE subject_type = ? AND subject_id = ? "
           "AND status != 'voided'")
    args: list = [subject_type, subject_id]
    if start:
        sql += " AND occurred_at >= ?"
        args.append(to_iso(parse_iso(start)))
    if end:
        sql += " AND occurred_at <= ?"
        args.append(to_iso(parse_iso(end)))
    sql += " ORDER BY occurred_at, id"
    return [event_dict(r) for r in conn.execute(sql, args)]


def event_detail(conn, event_id: int) -> dict:
    row = get_event(conn, event_id)
    d = event_dict(row)
    d["corrections"] = [
        correction_dict(r)
        for r in conn.execute(
            "SELECT * FROM correction WHERE care_event_id = ? ORDER BY id", (event_id,)
        )
    ]
    d["acknowledgements"] = [
        dict(r)
        for r in conn.execute(
            "SELECT * FROM acknowledgement WHERE care_event_id = ? ORDER BY id", (event_id,)
        )
    ]
    return d


def acknowledge_event(conn, staff, event_id: int) -> dict:
    """重要提醒被明确签收；签收前一直挂在待签收列表里。"""
    row = get_event(conn, event_id)
    if not row["is_important"]:
        raise HTTPException(422, "该事件不是重要提醒，无需签收")
    conn.execute(
        "INSERT OR IGNORE INTO acknowledgement (care_event_id, staff_id, acked_at) VALUES (?, ?, ?)",
        (event_id, staff["id"], to_iso(now_utc())),
    )
    return event_detail(conn, event_id)


def pending_reminders(conn, subject_type: Optional[str], subject_id: Optional[int]) -> list:
    sql = """SELECT * FROM care_event e
             WHERE e.is_important = 1 AND e.status != 'voided'
               AND NOT EXISTS (SELECT 1 FROM acknowledgement a WHERE a.care_event_id = e.id)"""
    args: list = []
    if subject_type and subject_id is not None:
        sql += " AND e.subject_type = ? AND e.subject_id = ?"
        args += [subject_type, subject_id]
    sql += " ORDER BY e.occurred_at, e.id"
    return [event_dict(r) for r in conn.execute(sql, args)]


# ---------------------------------------------------------------- 交接

def create_handover(conn, staff, data) -> dict:
    shift = conn.execute("SELECT * FROM shift WHERE id = ?", (data.shift_id,)).fetchone()
    if shift is None:
        raise HTTPException(404, f"班次 #{data.shift_id} 不存在")
    if shift["staff_id"] != staff["id"] and staff["role"] != "leader":
        raise HTTPException(403, "只能为本人班次发起交接")
    incoming = get_staff(conn, data.incoming_staff_id)
    if incoming["id"] == shift["staff_id"]:
        raise HTTPException(422, "接班人与交班人不能是同一人")
    period_end = shift["ended_at"] or to_iso(now_utc())
    try:
        cur = conn.execute(
            """INSERT INTO handover
               (shift_id, outgoing_staff_id, incoming_staff_id, period_start, period_end, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (data.shift_id, shift["staff_id"], incoming["id"],
             shift["started_at"], period_end, to_iso(now_utc())),
        )
    except sqlite3.IntegrityError:
        raise HTTPException(409, "该班次已存在交接单")
    return handover_detail(conn, cur.lastrowid)


def _insert_item(conn, handover_id: int, item_type: str, care_event_id: Optional[int],
                 description: str, now_iso: str) -> None:
    conn.execute(
        """INSERT INTO handover_item (handover_id, item_type, care_event_id, description, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (handover_id, item_type, care_event_id, description, now_iso),
    )


def submit_handover(conn, staff, handover_id: int) -> dict:
    """提交时快照未完成事项：需跟进事件 + 未被签收的重要提醒。"""
    with db.transaction(conn):
        h = conn.execute("SELECT * FROM handover WHERE id = ?", (handover_id,)).fetchone()
        if h is None:
            raise HTTPException(404, f"交接单 #{handover_id} 不存在")
        if h["status"] != "draft":
            raise HTTPException(409, f"交接单已处于 {h['status']} 状态，不能重复提交")
        if h["outgoing_staff_id"] != staff["id"] and staff["role"] != "leader":
            raise HTTPException(403, "只有交班人本人或护理组长可以提交交接单")
        now_iso = to_iso(now_utc())
        events = conn.execute(
            "SELECT * FROM care_event WHERE shift_id = ? AND status = 'active' ORDER BY occurred_at",
            (h["shift_id"],),
        ).fetchall()
        for e in events:
            if e["needs_followup"]:
                _insert_item(conn, handover_id, "followup", e["id"],
                             f"[需跟进] {_describe_event(e)}", now_iso)
            if e["is_important"]:
                acked = conn.execute(
                    "SELECT 1 FROM acknowledgement WHERE care_event_id = ? LIMIT 1", (e["id"],)
                ).fetchone()
                if acked is None:
                    _insert_item(conn, handover_id, "reminder", e["id"],
                                 f"[重要提醒] {_describe_event(e)}", now_iso)
        conn.execute(
            "UPDATE handover SET status = 'submitted', submitted_at = ? WHERE id = ?",
            (now_iso, handover_id),
        )
    return handover_detail(conn, handover_id)


def add_manual_item(conn, staff, handover_id: int, data) -> dict:
    with db.transaction(conn):
        h = conn.execute("SELECT * FROM handover WHERE id = ?", (handover_id,)).fetchone()
        if h is None:
            raise HTTPException(404, f"交接单 #{handover_id} 不存在")
        if h["status"] == "signed":
            raise HTTPException(409, "交接单已签署，不能再添加事项")
        _insert_item(conn, handover_id, "manual", None, data.description, to_iso(now_utc()))
    return handover_detail(conn, handover_id)


def confirm_item(conn, staff, handover_id: int, item_id: int) -> dict:
    """接班人逐项确认；条件 UPDATE 保证并发下只有一方成功。"""
    with db.transaction(conn):
        h = conn.execute("SELECT * FROM handover WHERE id = ?", (handover_id,)).fetchone()
        if h is None:
            raise HTTPException(404, f"交接单 #{handover_id} 不存在")
        if h["status"] != "submitted":
            raise HTTPException(409, "交接单不在待确认状态")
        if h["incoming_staff_id"] != staff["id"]:
            raise HTTPException(403, "只有接班人可以确认交接事项")
        item = conn.execute(
            "SELECT * FROM handover_item WHERE id = ? AND handover_id = ?",
            (item_id, handover_id),
        ).fetchone()
        if item is None:
            raise HTTPException(404, f"交接事项 #{item_id} 不存在")
        now_iso = to_iso(now_utc())
        cur = conn.execute(
            """UPDATE handover_item SET status = 'confirmed', confirmed_by = ?, confirmed_at = ?
               WHERE id = ? AND status = 'pending'""",
            (staff["id"], now_iso, item_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, "该事项已被确认")
        # 确认重要提醒事项即视为签收该提醒
        if item["item_type"] == "reminder" and item["care_event_id"] is not None:
            conn.execute(
                "INSERT OR IGNORE INTO acknowledgement (care_event_id, staff_id, acked_at) "
                "VALUES (?, ?, ?)",
                (item["care_event_id"], staff["id"], now_iso),
            )
    return handover_detail(conn, handover_id)


def sign_handover(conn, staff, handover_id: int) -> dict:
    with db.transaction(conn):
        h = conn.execute("SELECT * FROM handover WHERE id = ?", (handover_id,)).fetchone()
        if h is None:
            raise HTTPException(404, f"交接单 #{handover_id} 不存在")
        if h["status"] != "submitted":
            raise HTTPException(409, f"交接单当前状态为 {h['status']}，不能签署")
        if h["incoming_staff_id"] != staff["id"]:
            raise HTTPException(403, "只有接班人可以签署交接")
        pending = conn.execute(
            "SELECT id FROM handover_item WHERE handover_id = ? AND status = 'pending'",
            (handover_id,),
        ).fetchall()
        if pending:
            raise HTTPException(409, {
                "detail": "仍有未确认事项，不能签署",
                "pending_item_ids": [r["id"] for r in pending],
            })
        cur = conn.execute(
            "UPDATE handover SET status = 'signed', signed_at = ? "
            "WHERE id = ? AND status = 'submitted'",
            (to_iso(now_utc()), handover_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, "交接单已被签署")
    return handover_detail(conn, handover_id)


def handover_detail(conn, handover_id: int) -> dict:
    h = conn.execute("SELECT * FROM handover WHERE id = ?", (handover_id,)).fetchone()
    if h is None:
        raise HTTPException(404, f"交接单 #{handover_id} 不存在")
    d = dict(h)
    for key, col in (("outgoing_staff", "outgoing_staff_id"), ("incoming_staff", "incoming_staff_id")):
        s = conn.execute("SELECT id, name, role FROM staff WHERE id = ?", (d[col],)).fetchone()
        d[key] = dict(s) if s else None
    d["items"] = [
        item_dict(r)
        for r in conn.execute(
            "SELECT * FROM handover_item WHERE handover_id = ? ORDER BY id", (handover_id,)
        )
    ]
    return d


def list_handovers(conn, status: Optional[str]) -> list:
    sql = "SELECT * FROM handover"
    args: list = []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC"
    return [dict(r) for r in conn.execute(sql, args)]


def leader_summary(conn, handover_id: int) -> dict:
    """护理组长视图：未确认事项、异常变化、责任人、更正经过。"""
    h = get_handover(conn, handover_id)
    items = conn.execute(
        "SELECT * FROM handover_item WHERE handover_id = ? ORDER BY id", (handover_id,)
    ).fetchall()
    unconfirmed = [item_dict(r) for r in items if r["status"] == "pending"]
    abnormal = [
        event_dict(r)
        for r in conn.execute(
            "SELECT * FROM care_event WHERE shift_id = ? AND is_abnormal = 1 "
            "AND status != 'voided' ORDER BY occurred_at",
            (h["shift_id"],),
        )
    ]
    responsible = [
        dict(r)
        for r in conn.execute(
            """SELECT s.id, s.name, s.role, COUNT(e.id) AS event_count
               FROM care_event e JOIN staff s ON s.id = e.recorded_by
               WHERE e.shift_id = ? GROUP BY s.id, s.name, s.role ORDER BY s.id""",
            (h["shift_id"],),
        )
    ]
    confirmations = [
        dict(r)
        for r in conn.execute(
            """SELECT i.id AS item_id, i.item_type, i.description,
                      i.confirmed_at, s.id AS confirmed_by_id, s.name AS confirmed_by_name
               FROM handover_item i JOIN staff s ON s.id = i.confirmed_by
               WHERE i.handover_id = ? AND i.status = 'confirmed' ORDER BY i.id""",
            (handover_id,),
        )
    ]
    corrections = [
        correction_dict(r) | {"corrected_by_name": r["corrected_by_name"]}
        for r in conn.execute(
            """SELECT c.*, s.name AS corrected_by_name
               FROM correction c JOIN staff s ON s.id = c.corrected_by
               WHERE c.handover_id = ? ORDER BY c.id""",
            (handover_id,),
        )
    ]
    return {
        "handover": handover_detail(conn, handover_id),
        "unconfirmed_items": unconfirmed,
        "abnormal_events": abnormal,
        "responsible_staff": responsible,
        "confirmations": confirmations,
        "corrections": corrections,
    }


# ---------------------------------------------------------------- 更正

def correct_event(conn, staff, event_id: int, data) -> dict:
    """对事件发起更正；已签署时段的差异只能以此方式说明。"""
    if data.action == "amend" and data.new_payload is None:
        raise HTTPException(422, "amend 更正必须提供 new_payload")
    with db.transaction(conn):
        e = get_event(conn, event_id)
        if e["status"] == "voided":
            raise HTTPException(409, "事件已作废，不能再更正")
        signed = conn.execute(
            "SELECT id FROM handover WHERE shift_id = ? AND status = 'signed'",
            (e["shift_id"],),
        ).fetchone()
        cur = conn.execute(
            """INSERT INTO correction
               (handover_id, care_event_id, action, reason, new_payload_json, corrected_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                signed["id"] if signed else None,
                event_id,
                data.action,
                data.reason,
                json.dumps(data.new_payload, ensure_ascii=False)
                if data.new_payload is not None else None,
                staff["id"],
                to_iso(now_utc()),
            ),
        )
        conn.execute(
            "UPDATE care_event SET status = ? WHERE id = ?",
            ("voided" if data.action == "void" else "corrected", event_id),
        )
        cid = cur.lastrowid
    return correction_dict(
        conn.execute("SELECT * FROM correction WHERE id = ?", (cid,)).fetchone()
    )


def add_missed_event(conn, staff, handover_id: int, data) -> Tuple[dict, bool]:
    """已签署交接的漏录补登：事件进入时间线，同时留下 add 更正记录。"""
    with db.transaction(conn):
        h = conn.execute("SELECT * FROM handover WHERE id = ?", (handover_id,)).fetchone()
        if h is None:
            raise HTTPException(404, f"交接单 #{handover_id} 不存在")
        if h["status"] != "signed":
            raise HTTPException(409, "仅已签署的交接需要更正补录；未签署时段请直接提交事件")
        occurred = _normalize_dt(data.event.occurred_at)
        occurred_iso = to_iso(occurred)
        if not (h["period_start"] <= occurred_iso <= h["period_end"]):
            raise HTTPException(422, "补录事件的发生时间不在该交接时段内")
        _check_subject(conn, data.event.subject_type, data.event.subject_id)
        row, created = _insert_event(
            conn, staff["id"], h["shift_id"], data.event, occurred, now_utc()
        )
        if created:
            conn.execute(
                """INSERT INTO correction
                   (handover_id, care_event_id, action, reason, corrected_by, created_at)
                   VALUES (?, ?, 'add', ?, ?, ?)""",
                (handover_id, row["id"], data.reason, staff["id"], to_iso(now_utc())),
            )
    return event_dict(row), created


# ---------------------------------------------------------------- 家属端

def review_for_family(conn, staff, event_id: int, data) -> dict:
    """护理组长审核：确认内容属实并决定是否允许共享给家属。"""
    get_event(conn, event_id)
    conn.execute(
        """UPDATE care_event
           SET center_confirmed = 1, center_confirmed_by = ?, center_confirmed_at = ?,
               shareable_with_family = ?
           WHERE id = ?""",
        (staff["id"], to_iso(now_utc()), int(data.shareable), event_id),
    )
    return event_dict(get_event(conn, event_id))


def _effective_payload(conn, event_row: sqlite3.Row) -> dict:
    """家属端展示生效内容：被更正过的事件以最近一次 amend 为准。"""
    if event_row["status"] == "corrected":
        corr = conn.execute(
            """SELECT new_payload_json FROM correction
               WHERE care_event_id = ? AND action = 'amend' AND new_payload_json IS NOT NULL
               ORDER BY id DESC LIMIT 1""",
            (event_row["id"],),
        ).fetchone()
        if corr is not None:
            return json.loads(corr["new_payload_json"])
    return json.loads(event_row["payload_json"])


def family_timeline(conn, subject_type: str, subject_id: int) -> list:
    """家属端只展示经过中心确认且允许共享的内容，不含工作人员信息。"""
    _check_subject(conn, subject_type, subject_id)
    rows = conn.execute(
        """SELECT * FROM care_event
           WHERE subject_type = ? AND subject_id = ?
             AND center_confirmed = 1 AND shareable_with_family = 1 AND status != 'voided'
           ORDER BY occurred_at, id""",
        (subject_type, subject_id),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "subject_type": r["subject_type"],
            "subject_id": r["subject_id"],
            "category": r["category"],
            "category_zh": CATEGORY_ZH[r["category"]],
            "occurred_at": r["occurred_at"],
            "payload": _effective_payload(conn, r),
        }
        for r in rows
    ]
