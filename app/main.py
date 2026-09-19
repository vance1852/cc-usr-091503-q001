"""FastAPI 应用与路由。

身份约定：所有写操作通过请求头 X-Staff-Id 标识操作人；
服务端以该身份做班次归属、角色与交接权限校验。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from . import services
from .db import connect, init_db
from .models import (
    BabyIn,
    CenterReviewIn,
    CorrectionIn,
    EventIn,
    HandoverIn,
    MotherIn,
    ShiftIn,
    StaffIn,
)
from .timeutil import iso, utcnow

DEFAULT_DB_PATH = "care.db"


# ---------------------------------------------------------------- 依赖

def get_conn(request: Request):
    conn = connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[sqlite3.Connection, Depends(get_conn)]


def get_staff(conn: Conn, x_staff_id: Annotated[int, Header()]) -> sqlite3.Row:
    return services.get_staff_or_401(conn, x_staff_id)


Staff = Annotated[sqlite3.Row, Depends(get_staff)]


def create_app(db_path: str = DEFAULT_DB_PATH) -> FastAPI:
    init_db(db_path)
    app = FastAPI(title="母婴照护交接服务", version="1.0.0")
    app.state.db_path = db_path

    # ------------------------------------------------------------ 建档

    @app.post("/staff", status_code=201)
    def create_staff(data: StaffIn, conn: Conn):
        cur = conn.execute(
            "INSERT INTO staff (name, role, created_at) VALUES (?,?,?)",
            (data.name, data.role, iso(utcnow())),
        )
        return dict(conn.execute("SELECT * FROM staff WHERE id=?", (cur.lastrowid,)).fetchone())

    @app.post("/mothers", status_code=201)
    def create_mother(data: MotherIn, conn: Conn):
        cur = conn.execute(
            "INSERT INTO mothers (name, room, notes, created_at) VALUES (?,?,?,?)",
            (data.name, data.room, data.notes, iso(utcnow())),
        )
        return dict(conn.execute("SELECT * FROM mothers WHERE id=?", (cur.lastrowid,)).fetchone())

    @app.post("/babies", status_code=201)
    def create_baby(data: BabyIn, conn: Conn):
        if conn.execute("SELECT 1 FROM mothers WHERE id=?", (data.mother_id,)).fetchone() is None:
            raise HTTPException(404, f"母亲 {data.mother_id} 不存在")
        cur = conn.execute(
            "INSERT INTO babies (mother_id, name, birth_date, notes, created_at) VALUES (?,?,?,?,?)",
            (data.mother_id, data.name, data.birth_date, data.notes, iso(utcnow())),
        )
        return dict(conn.execute("SELECT * FROM babies WHERE id=?", (cur.lastrowid,)).fetchone())

    @app.post("/shifts", status_code=201)
    def create_shift(data: ShiftIn, conn: Conn):
        if conn.execute("SELECT 1 FROM staff WHERE id=?", (data.staff_id,)).fetchone() is None:
            raise HTTPException(404, f"护理人员 {data.staff_id} 不存在")
        start, end = iso(data.start_at), iso(data.end_at)
        if end <= start:
            raise HTTPException(400, "班次结束时间必须晚于开始时间")
        cur = conn.execute(
            "INSERT INTO shifts (staff_id, start_at, end_at, label) VALUES (?,?,?,?)",
            (data.staff_id, start, end, data.label),
        )
        return dict(conn.execute("SELECT * FROM shifts WHERE id=?", (cur.lastrowid,)).fetchone())

    # ------------------------------------------------------------ 照护事件

    @app.post("/events", status_code=201)
    def create_event(data: EventIn, staff: Staff, conn: Conn, response: Response):
        event, deduplicated = services.create_event(conn, staff, data)
        if deduplicated:
            response.status_code = 200  # 重复提交：返回原记录，不生成第二条
        return event

    @app.get("/events/{event_id}")
    def get_event(event_id: int, conn: Conn):
        return services.event_out(conn, services.get_event_or_404(conn, event_id))

    @app.post("/events/{event_id}/ack")
    def ack_event(event_id: int, staff: Staff, conn: Conn):
        return services.ack_event(conn, staff, event_id)

    @app.post("/events/{event_id}/resolve")
    def resolve_event(event_id: int, staff: Staff, conn: Conn):
        return services.resolve_event(conn, staff, event_id)

    @app.post("/events/{event_id}/center-review")
    def center_review(event_id: int, data: CenterReviewIn, staff: Staff, conn: Conn):
        return services.center_review_event(conn, staff, event_id, data.shareable)

    @app.get("/timeline")
    def get_timeline(
        subject_type: str, subject_id: int, conn: Conn,
        start: str | None = None, end: str | None = None,
    ):
        return services.timeline(conn, subject_type, subject_id, start, end)

    @app.get("/family/timeline")
    def get_family_timeline(subject_type: str, subject_id: int, conn: Conn):
        return services.family_timeline(conn, subject_type, subject_id)

    # ------------------------------------------------------------ 交接

    @app.post("/handovers", status_code=201)
    def create_handover(data: HandoverIn, staff: Staff, conn: Conn):
        return services.create_handover(conn, staff, data)

    @app.get("/handovers/{handover_id}")
    def get_handover(handover_id: int, conn: Conn):
        return services.handover_out(conn, services.get_handover_or_404(conn, handover_id))

    @app.post("/handovers/{handover_id}/items/{item_id}/confirm")
    def confirm_item(handover_id: int, item_id: int, staff: Staff, conn: Conn):
        return services.confirm_item(conn, staff, handover_id, item_id)

    @app.post("/handovers/{handover_id}/sign")
    def sign_handover(handover_id: int, staff: Staff, conn: Conn):
        return services.sign_handover(conn, staff, handover_id)

    @app.post("/handovers/{handover_id}/corrections", status_code=201)
    def add_correction(handover_id: int, data: CorrectionIn, staff: Staff, conn: Conn):
        return services.add_correction(conn, staff, handover_id, data)

    @app.get("/handovers/{handover_id}/lead-view")
    def lead_view(handover_id: int, staff: Staff, conn: Conn):
        return services.lead_view(conn, staff, handover_id)

    return app


app = create_app()
