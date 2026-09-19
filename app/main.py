"""FastAPI 路由层：母婴照护交接服务。

身份约定：请求头 X-Staff-Id 标识当前操作人（演示用，生产应替换为正式认证）。
"""
from __future__ import annotations

import os
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response

from . import db, schemas, services


def create_app(db_path: str = "care.db") -> FastAPI:
    db.init_db(db_path)
    app = FastAPI(title="月子中心母婴照护交接服务", version="1.0.0")
    app.state.db_path = db_path

    def get_conn(request: Request):
        conn = db.connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def current_staff(
        conn=Depends(get_conn), x_staff_id: int = Header(..., alias="X-Staff-Id")
    ):
        row = conn.execute("SELECT * FROM staff WHERE id = ?", (x_staff_id,)).fetchone()
        if row is None:
            raise HTTPException(401, "未知护理人员，请检查 X-Staff-Id")
        return row

    def require_leader(staff=Depends(current_staff)):
        if staff["role"] != "leader":
            raise HTTPException(403, "需要护理组长权限")
        return staff

    # ---------------- 档案 ----------------

    @app.post("/staff", status_code=201)
    def create_staff(data: schemas.StaffIn, conn=Depends(get_conn)):
        return services.create_staff(conn, data)

    @app.post("/mothers", status_code=201)
    def create_mother(data: schemas.MotherIn, conn=Depends(get_conn)):
        return services.create_mother(conn, data)

    @app.get("/mothers/{mother_id}")
    def get_mother(mother_id: int, conn=Depends(get_conn)):
        return services.mother_detail(conn, mother_id)

    @app.post("/babies", status_code=201)
    def create_baby(data: schemas.BabyIn, conn=Depends(get_conn)):
        return services.create_baby(conn, data)

    @app.get("/babies/{baby_id}")
    def get_baby(baby_id: int, conn=Depends(get_conn)):
        return services.baby_detail(conn, baby_id)

    # ---------------- 班次 ----------------

    @app.post("/shifts", status_code=201)
    def start_shift(data: schemas.ShiftIn, staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.start_shift(conn, staff, data)

    @app.post("/shifts/{shift_id}/end")
    def end_shift(shift_id: int, data: schemas.ShiftEndIn,
                  staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.end_shift(conn, staff, shift_id, data)

    # ---------------- 照护事件与时间线 ----------------

    @app.post("/events", status_code=201)
    def record_event(data: schemas.EventIn, response: Response,
                     staff=Depends(current_staff), conn=Depends(get_conn)):
        event, created = services.record_event(conn, staff, data)
        if not created:
            response.status_code = 200  # 幂等重放：返回已存在的记录
        return event

    @app.get("/events/{event_id}")
    def get_event(event_id: int, staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.event_detail(conn, event_id)

    @app.get("/timeline")
    def get_timeline(subject_type: str, subject_id: int,
                     start: Optional[str] = None, end: Optional[str] = None,
                     staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.timeline(conn, subject_type, subject_id, start, end)

    @app.post("/events/{event_id}/acknowledge")
    def acknowledge(event_id: int, staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.acknowledge_event(conn, staff, event_id)

    @app.get("/reminders/pending")
    def get_pending_reminders(subject_type: Optional[str] = None,
                              subject_id: Optional[int] = None,
                              staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.pending_reminders(conn, subject_type, subject_id)

    @app.post("/events/{event_id}/corrections", status_code=201)
    def correct_event(event_id: int, data: schemas.CorrectionIn,
                      staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.correct_event(conn, staff, event_id, data)

    # ---------------- 交接 ----------------

    @app.post("/handovers", status_code=201)
    def create_handover(data: schemas.HandoverIn,
                        staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.create_handover(conn, staff, data)

    @app.get("/handovers")
    def list_handovers(status: Optional[str] = None,
                       staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.list_handovers(conn, status)

    @app.get("/handovers/{handover_id}")
    def get_handover(handover_id: int, staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.handover_detail(conn, handover_id)

    @app.post("/handovers/{handover_id}/submit")
    def submit_handover(handover_id: int, staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.submit_handover(conn, staff, handover_id)

    @app.post("/handovers/{handover_id}/items", status_code=201)
    def add_item(handover_id: int, data: schemas.ManualItemIn,
                 staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.add_manual_item(conn, staff, handover_id, data)

    @app.post("/handovers/{handover_id}/items/{item_id}/confirm")
    def confirm_item(handover_id: int, item_id: int,
                     staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.confirm_item(conn, staff, handover_id, item_id)

    @app.post("/handovers/{handover_id}/sign")
    def sign_handover(handover_id: int, staff=Depends(current_staff), conn=Depends(get_conn)):
        return services.sign_handover(conn, staff, handover_id)

    @app.post("/handovers/{handover_id}/corrections", status_code=201)
    def add_missed_event(handover_id: int, data: schemas.MissedEventIn, response: Response,
                         staff=Depends(current_staff), conn=Depends(get_conn)):
        event, created = services.add_missed_event(conn, staff, handover_id, data)
        if not created:
            response.status_code = 200
        return event

    @app.get("/handovers/{handover_id}/leader-summary")
    def leader_summary(handover_id: int, staff=Depends(require_leader), conn=Depends(get_conn)):
        return services.leader_summary(conn, handover_id)

    # ---------------- 家属端 ----------------

    @app.post("/events/{event_id}/family-review")
    def family_review(event_id: int, data: schemas.FamilyReviewIn,
                      staff=Depends(require_leader), conn=Depends(get_conn)):
        return services.review_for_family(conn, staff, event_id, data)

    @app.get("/family/timeline")
    def family_timeline(subject_type: str, subject_id: int, conn=Depends(get_conn)):
        return services.family_timeline(conn, subject_type, subject_id)

    return app


app = create_app(os.environ.get("CARE_DB", "care.db"))
