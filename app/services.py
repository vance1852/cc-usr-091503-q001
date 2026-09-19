"""业务逻辑层：所有规则集中在这里，路由层只做参数解析。"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from fastapi import HTTPException

from .db import immediate_tx
from .models import CorrectionIn, EventIn, HandoverIn
from .timeutil import iso, parse, utcnow

# 录入时间晚于发生时间超过该阈值即视为迟到补录
LATE_ENTRY_THRESHOLD = timedelta(minutes=15)
# 允许时钟微小偏差，但禁止录入"未来发生"的事件
FUTURE_TOLERANCE = timedelta(minutes=5)


# ---------------------------------------------------------------- 输出组装

def _loads(payload: str) -> dict[str, Any]:
    return json.loads(payload) if payload else {}


def is_late(row: sqlite3.Row) -> bool:
    return parse(row["recorded_at"]) - parse(row["occurred_at"]) > LATE_ENTRY_THRESHOLD


def event_out(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    acks = conn.execute(
        "SELECT staff_id, acked_at FROM acknowledgements WHERE event_id=? ORDER BY acked_at",
        (row["id"],),
    ).fetchall()
    return {
        "id": row["id"],
        "client_request_id": row["client_request_id"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "category": row["category"],
        "occurred_at": row["occurred_at"],
        "recorded_at": row["recorded_at"],
        "late_entry": is_late(row),
        "recorded_by": row["recorded_by"],
        "shift_id": row["shift_id"],
        "payload": _loads(row["payload"]),
        "is_important": bool(row["is_important"]),
        "requires_followup": bool(row["requires_followup"]),
        "abnormal": bool(row["abnormal"]),
        "shareable": bool(row["shareable"]),
        "center_confirmed": bool(row["center_confirmed"]),
        "center_confirmed_by": row["center_confirmed_by"],
        "center_confirmed_at": row["center_confirmed_at"],
        "resolved": row["resolved_at"] is not None,
        "resolved_at": row["resolved_at"],
        "resolved_by": row["resolved_by"],
        "acknowledged_by": [{"staff_id": a["staff_id"], "acked_at": a["acked_at"]} for a in acks],
    }


def family_event_out(row: sqlite3.Row) -> dict[str, Any]:
    """家属端视图：只暴露经过中心确认且允许共享的内容，不含内部字段。"""
    return {
        "id": row["id"],
        "subject_type": row["subject_type"],
        "subject_id": row["subject_id"],
        "category": row["category"],
        "occurred_at": row["occurred_at"],
        "payload": _loads(row["payload"]),
    }


def item_out(conn: sqlite3.Connection, row: sqlite3.Row, with_event: bool = True) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "handover_id": row["handover_id"],
        "kind": row["kind"],
        "event_id": row["event_id"],
        "status": row["status"],
        "confirmed_by": row["confirmed_by"],
        "confirmed_at": row["confirmed_at"],
    }
    if with_event:
        ev = conn.execute("SELECT * FROM care_events WHERE id=?", (row["event_id"],)).fetchone()
        out["event"] = event_out(conn, ev) if ev else None
    return out


def handover_out(conn: sqlite3.Connection, row: sqlite3.Row, with_items: bool = True) -> dict[str, Any]:
    out = {
        "id": row["id"],
        "shift_id": row["shift_id"],
        "outgoing_staff_id": row["outgoing_staff_id"],
        "incoming_staff_id": row["incoming_staff_id"],
        "period_start": row["period_start"],
        "period_end": row["period_end"],
        "status": row["status"],
        "created_at": row["created_at"],
        "signed_at": row["signed_at"],
    }
    if with_items:
        items = conn.execute(
            "SELECT * FROM handover_items WHERE handover_id=? ORDER BY id", (row["id"],)
        ).fetchall()
        out["items"] = [item_out(conn, it) for it in items]
    return out


# ---------------------------------------------------------------- 基础查询

def get_staff_or_401(conn: sqlite3.Connection, staff_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
    if row is None:
        raise HTTPException(401, "无效的护理人员身份（X-Staff-Id）")
    return row


def get_event_or_404(conn: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM care_events WHERE id=?", (event_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"照护事件 {event_id} 不存在")
    return row


def get_handover_or_404(conn: sqlite3.Connection, handover_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM handovers WHERE id=?", (handover_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"交接单 {handover_id} 不存在")
    return row


# ---------------------------------------------------------------- 照护事件

def create_event(conn: sqlite3.Connection, staff: sqlite3.Row, data: EventIn) -> tuple[dict[str, Any], bool]:
    """录入照护事件。返回 (事件, 是否幂等命中)。

    规则：
    - 只能提交本人负责时段内发生的事实（occurred_at 落在本人某班次内）；
    - client_request_id 唯一，重复提交返回原记录，不产生第二条；
    - 同一 client_request_id 提交不同内容视为冲突。
    """
    occurred = iso(data.occurred_at)
    now = utcnow()
    if parse(occurred) > now + FUTURE_TOLERANCE:
        raise HTTPException(400, "发生时间不能晚于当前时间")

    table = "mothers" if data.subject_type == "mother" else "babies"
    if conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (data.subject_id,)).fetchone() is None:
        raise HTTPException(404, f"{data.subject_type} {data.subject_id} 不存在")

    shift = conn.execute(
        "SELECT * FROM shifts WHERE staff_id=? AND start_at<=? AND end_at>? ORDER BY id LIMIT 1",
        (staff["id"], occurred, occurred),
    ).fetchone()
    if shift is None:
        raise HTTPException(403, "只能提交自己负责时段内发生的事实")

    recorded_at = iso(now)
    payload = json.dumps(data.payload, ensure_ascii=False, sort_keys=True)
    try:
        cur = conn.execute(
            """INSERT INTO care_events
               (client_request_id, subject_type, subject_id, category, occurred_at,
                recorded_at, recorded_by, shift_id, payload,
                is_important, requires_followup, abnormal, shareable)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                data.client_request_id, data.subject_type, data.subject_id, data.category,
                occurred, recorded_at, staff["id"], shift["id"], payload,
                int(data.is_important), int(data.requires_followup),
                int(data.abnormal), int(data.shareable),
            ),
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM care_events WHERE client_request_id=?", (data.client_request_id,)
        ).fetchone()
        if existing is None:  # 唯一约束冲突来自别处时不应发生
            raise
        same = (
            existing["subject_type"] == data.subject_type
            and existing["subject_id"] == data.subject_id
            and existing["category"] == data.category
            and existing["occurred_at"] == occurred
            and _loads(existing["payload"]) == data.payload
        )
        if not same:
            raise HTTPException(409, "client_request_id 已被不同内容的记录占用")
        return event_out(conn, existing), True

    row = conn.execute("SELECT * FROM care_events WHERE id=?", (cur.lastrowid,)).fetchone()
    return event_out(conn, row), False


def ack_event(conn: sqlite3.Connection, staff: sqlite3.Row, event_id: int) -> dict[str, Any]:
    """明确接收重要提醒；重复接收幂等。"""
    ev = get_event_or_404(conn, event_id)
    if not ev["is_important"]:
        raise HTTPException(400, "只有重要提醒需要确认接收")
    conn.execute(
        "INSERT OR IGNORE INTO acknowledgements (event_id, staff_id, acked_at) VALUES (?,?,?)",
        (event_id, staff["id"], iso(utcnow())),
    )
    return event_out(conn, get_event_or_404(conn, event_id))


def resolve_event(conn: sqlite3.Connection, staff: sqlite3.Row, event_id: int) -> dict[str, Any]:
    """关闭需要持续关注的事项。"""
    ev = get_event_or_404(conn, event_id)
    if not ev["requires_followup"]:
        raise HTTPException(400, "该事件不是持续关注事项")
    cur = conn.execute(
        "UPDATE care_events SET resolved_at=?, resolved_by=? WHERE id=? AND resolved_at IS NULL",
        (iso(utcnow()), staff["id"], event_id),
    )
    if cur.rowcount == 0:
        raise HTTPException(409, "该关注事项已被关闭")
    return event_out(conn, get_event_or_404(conn, event_id))


def center_review_event(
    conn: sqlite3.Connection, staff: sqlite3.Row, event_id: int, shareable: bool | None
) -> dict[str, Any]:
    """中心确认（仅护理组长）；家属端只展示确认且允许共享的内容。"""
    if staff["role"] != "lead_nurse":
        raise HTTPException(403, "只有护理组长可以执行中心确认")
    get_event_or_404(conn, event_id)
    if shareable is None:
        conn.execute(
            "UPDATE care_events SET center_confirmed=1, center_confirmed_by=?, center_confirmed_at=? WHERE id=?",
            (staff["id"], iso(utcnow()), event_id),
        )
    else:
        conn.execute(
            "UPDATE care_events SET center_confirmed=1, center_confirmed_by=?, center_confirmed_at=?, shareable=? WHERE id=?",
            (staff["id"], iso(utcnow()), int(shareable), event_id),
        )
    return event_out(conn, get_event_or_404(conn, event_id))


def timeline(
    conn: sqlite3.Connection,
    subject_type: str,
    subject_id: int,
    start: str | None = None,
    end: str | None = None,
) -> list[dict[str, Any]]:
    """照护时间线：按实际发生时间排序（而非录入时间）。"""
    sql = "SELECT * FROM care_events WHERE subject_type=? AND subject_id=?"
    params: list[Any] = [subject_type, subject_id]
    if start:
        sql += " AND occurred_at>=?"
        params.append(iso(parse(start)))
    if end:
        sql += " AND occurred_at<?"
        params.append(iso(parse(end)))
    sql += " ORDER BY occurred_at, recorded_at, id"
    rows = conn.execute(sql, params).fetchall()
    return [event_out(conn, r) for r in rows]


def family_timeline(conn: sqlite3.Connection, subject_type: str, subject_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT * FROM care_events
           WHERE subject_type=? AND subject_id=? AND shareable=1 AND center_confirmed=1
           ORDER BY occurred_at, recorded_at, id""",
        (subject_type, subject_id),
    ).fetchall()
    return [family_event_out(r) for r in rows]


# ---------------------------------------------------------------- 交接

def _open_handover_item_events(conn: sqlite3.Connection, period_end: str) -> list[tuple[str, int]]:
    """收集交接事项：未接收的重要提醒 + 未关闭的持续关注（含历史遗留，即自动结转）。"""
    important = conn.execute(
        """SELECT e.id FROM care_events e
           WHERE e.is_important=1 AND e.occurred_at<=?
             AND NOT EXISTS (SELECT 1 FROM acknowledgements a WHERE a.event_id=e.id)
           ORDER BY e.occurred_at""",
        (period_end,),
    ).fetchall()
    followups = conn.execute(
        """SELECT id FROM care_events
           WHERE requires_followup=1 AND resolved_at IS NULL AND occurred_at<=?
           ORDER BY occurred_at""",
        (period_end,),
    ).fetchall()
    return [("important_reminder", r["id"]) for r in important] + [
        ("open_followup", r["id"]) for r in followups
    ]


def create_handover(conn: sqlite3.Connection, staff: sqlite3.Row, data: HandoverIn) -> dict[str, Any]:
    """交班人为本班次创建交接单；事项快照在创建时生成。"""
    shift = conn.execute("SELECT * FROM shifts WHERE id=?", (data.shift_id,)).fetchone()
    if shift is None:
        raise HTTPException(404, f"班次 {data.shift_id} 不存在")
    if shift["staff_id"] != staff["id"]:
        raise HTTPException(403, "只能为本人负责的班次创建交接")
    if data.incoming_staff_id == staff["id"]:
        raise HTTPException(400, "接班人与交班人不能是同一人")
    if conn.execute("SELECT 1 FROM staff WHERE id=?", (data.incoming_staff_id,)).fetchone() is None:
        raise HTTPException(404, f"接班人 {data.incoming_staff_id} 不存在")

    with immediate_tx(conn):
        if conn.execute("SELECT 1 FROM handovers WHERE shift_id=?", (shift["id"],)).fetchone():
            raise HTTPException(409, "该班次已存在交接单；已签署的交接请通过更正事件说明差异")
        cur = conn.execute(
            """INSERT INTO handovers
               (shift_id, outgoing_staff_id, incoming_staff_id, period_start, period_end, status, created_at)
               VALUES (?,?,?,?,?, 'pending', ?)""",
            (shift["id"], staff["id"], data.incoming_staff_id,
             shift["start_at"], shift["end_at"], iso(utcnow())),
        )
        handover_id = cur.lastrowid
        for kind, event_id in _open_handover_item_events(conn, shift["end_at"]):
            conn.execute(
                "INSERT INTO handover_items (handover_id, kind, event_id) VALUES (?,?,?)",
                (handover_id, kind, event_id),
            )
    return handover_out(conn, get_handover_or_404(conn, handover_id))


def confirm_item(
    conn: sqlite3.Connection, staff: sqlite3.Row, handover_id: int, item_id: int
) -> dict[str, Any]:
    """接班人逐项确认。并发安全：条件更新 + 行数判断，重复确认返回 409。"""
    with immediate_tx(conn):
        h = get_handover_or_404(conn, handover_id)
        if h["status"] != "pending":
            raise HTTPException(409, "交接已签署，只能通过更正事件说明差异")
        if h["incoming_staff_id"] != staff["id"]:
            raise HTTPException(403, "只有接班人可以确认交接事项")
        item = conn.execute(
            "SELECT * FROM handover_items WHERE id=? AND handover_id=?", (item_id, handover_id)
        ).fetchone()
        if item is None:
            raise HTTPException(404, f"交接事项 {item_id} 不存在")
        cur = conn.execute(
            """UPDATE handover_items SET status='confirmed', confirmed_by=?, confirmed_at=?
               WHERE id=? AND status='pending'""",
            (staff["id"], iso(utcnow()), item_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, "该事项已被确认")
        # 确认"重要提醒"事项即视为明确接收该提醒
        if item["kind"] == "important_reminder":
            conn.execute(
                "INSERT OR IGNORE INTO acknowledgements (event_id, staff_id, acked_at) VALUES (?,?,?)",
                (item["event_id"], staff["id"], iso(utcnow())),
            )
        row = conn.execute("SELECT * FROM handover_items WHERE id=?", (item_id,)).fetchone()
        return item_out(conn, row)


def sign_handover(conn: sqlite3.Connection, staff: sqlite3.Row, handover_id: int) -> dict[str, Any]:
    """全部事项确认后由接班人签署；签署后不可再改，只能更正。"""
    with immediate_tx(conn):
        h = get_handover_or_404(conn, handover_id)
        if h["incoming_staff_id"] != staff["id"]:
            raise HTTPException(403, "只有接班人可以签署交接")
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM handover_items WHERE handover_id=? AND status='pending'",
            (handover_id,),
        ).fetchone()["n"]
        if pending:
            raise HTTPException(400, f"还有 {pending} 项未确认，不能签署")
        cur = conn.execute(
            "UPDATE handovers SET status='signed', signed_at=? WHERE id=? AND status='pending'",
            (iso(utcnow()), handover_id),
        )
        if cur.rowcount == 0:
            raise HTTPException(409, "交接已签署")
    return handover_out(conn, get_handover_or_404(conn, handover_id))


def add_correction(
    conn: sqlite3.Connection, staff: sqlite3.Row, handover_id: int, data: CorrectionIn
) -> dict[str, Any]:
    """已签署的交接只能通过更正事件说明差异，原交接与事件保持不变。"""
    h = get_handover_or_404(conn, handover_id)
    if h["status"] != "signed":
        raise HTTPException(409, "交接尚未签署；更正事件仅用于已签署的交接")
    if data.event_id is not None:
        get_event_or_404(conn, data.event_id)
    cur = conn.execute(
        "INSERT INTO corrections (handover_id, event_id, description, created_by, created_at) VALUES (?,?,?,?,?)",
        (handover_id, data.event_id, data.description, staff["id"], iso(utcnow())),
    )
    row = conn.execute("SELECT * FROM corrections WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(row)


def lead_view(conn: sqlite3.Connection, staff: sqlite3.Row, handover_id: int) -> dict[str, Any]:
    """护理组长视角：未确认事项、异常变化、责任人、更正经过，一屏看清。"""
    if staff["role"] != "lead_nurse":
        raise HTTPException(403, "只有护理组长可以查看交接总览")
    h = get_handover_or_404(conn, handover_id)

    items = conn.execute(
        "SELECT * FROM handover_items WHERE handover_id=? ORDER BY id", (handover_id,)
    ).fetchall()
    unconfirmed = [item_out(conn, it) for it in items if it["status"] == "pending"]

    period_events = conn.execute(
        """SELECT * FROM care_events WHERE occurred_at>=? AND occurred_at<?
           ORDER BY occurred_at, recorded_at, id""",
        (h["period_start"], h["period_end"]),
    ).fetchall()
    abnormal = [event_out(conn, e) for e in period_events if e["abnormal"]]
    late = [event_out(conn, e) for e in period_events if is_late(e)]

    corrections = conn.execute(
        "SELECT * FROM corrections WHERE handover_id=? ORDER BY id", (handover_id,)
    ).fetchall()

    def staff_brief(sid: int) -> dict[str, Any] | None:
        r = conn.execute("SELECT id, name, role FROM staff WHERE id=?", (sid,)).fetchone()
        return dict(r) if r else None

    return {
        "handover": handover_out(conn, h, with_items=False),
        "responsible": {
            "outgoing": staff_brief(h["outgoing_staff_id"]),
            "incoming": staff_brief(h["incoming_staff_id"]),
        },
        "unconfirmed_items": unconfirmed,
        "abnormal_events": abnormal,
        "late_entries": late,
        "corrections": [dict(c) for c in corrections],
        "summary": {
            "events_in_period": len(period_events),
            "items_total": len(items),
            "items_pending": len(unconfirmed),
            "abnormal_count": len(abnormal),
            "late_entry_count": len(late),
            "correction_count": len(corrections),
        },
    }
