"""测试公共装置。

时间基准固定在历史日期（2026-09-10 08:00 UTC），保证"发生时间不能晚于
当前时间"的校验在任何运行日期下都稳定通过；需要"非迟到"事件的测试
自行围绕当前时间建班次。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.main import create_app

BASE = datetime(2026, 9, 10, 8, 0, tzinfo=timezone.utc)


def T(hours: float = 0, minutes: int = 0) -> str:
    """相对基准时间的 ISO 字符串。T(0)=08:00，T(12)=20:00，T(36)=次日20:00。"""
    return (BASE + timedelta(hours=hours, minutes=minutes)).isoformat()


def hdr(staff_id: int) -> dict[str, str]:
    return {"X-Staff-Id": str(staff_id)}


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "care.db")


@pytest.fixture()
def client(db_path):
    with TestClient(create_app(db_path)) as c:
        yield c


def add_staff(client, name: str, role: str = "nurse") -> dict:
    r = client.post("/staff", json={"name": name, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


def add_shift(client, staff_id: int, start: str, end: str, label: str | None = None) -> dict:
    r = client.post(
        "/shifts",
        json={"staff_id": staff_id, "start_at": start, "end_at": end, "label": label},
    )
    assert r.status_code == 201, r.text
    return r.json()


def add_event(client, staff_id: int, occurred_at: str, *, subject_type: str = "baby",
              subject_id: int = 1, category: str = "feeding", payload: dict | None = None,
              request_id: str | None = None, **flags):
    body = {
        "client_request_id": request_id or uuid.uuid4().hex,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "category": category,
        "occurred_at": occurred_at,
        "payload": payload or {},
    }
    body.update(flags)
    return client.post("/events", json=body, headers=hdr(staff_id))


@pytest.fixture()
def env(client):
    """基础场景：组长 + 白班/夜班护士 + 一对母婴；夜班 20:00→次日08:00 跨午夜。"""
    lead = add_staff(client, "王组长", "lead_nurse")
    day = add_staff(client, "李护士")
    night = add_staff(client, "赵护士")
    mother = client.post("/mothers", json={"name": "陈女士", "room": "301"}).json()
    baby = client.post("/babies", json={"mother_id": mother["id"], "name": "小宝"}).json()
    day_shift = add_shift(client, day["id"], T(0), T(12), "白班")
    night_shift = add_shift(client, night["id"], T(12), T(36), "夜班")
    return SimpleNamespace(
        client=client, lead=lead, day=day, night=night,
        mother=mother, baby=baby, day_shift=day_shift, night_shift=night_shift,
    )
