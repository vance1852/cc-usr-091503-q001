import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture()
def app(tmp_path):
    return create_app(str(tmp_path / "care.db"))


@pytest.fixture()
def client(app):
    return TestClient(app)


def hdr(staff_id: int) -> dict:
    return {"X-Staff-Id": str(staff_id)}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def make_staff(client, name="护士", role="nurse") -> int:
    r = client.post("/staff", json={"name": name, "role": role})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_mother(client, name="妈妈") -> int:
    r = client.post("/mothers", json={"name": name, "room": "301"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_baby(client, mother_id, name="宝宝") -> int:
    r = client.post("/babies", json={"mother_id": mother_id, "name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def make_shift(client, staff_id, started_at: datetime, ended_at: datetime = None) -> int:
    r = client.post("/shifts", json={"staff_id": staff_id, "started_at": iso(started_at)},
                    headers=hdr(staff_id))
    assert r.status_code == 201, r.text
    shift_id = r.json()["id"]
    if ended_at is not None:
        r = client.post(f"/shifts/{shift_id}/end", json={"ended_at": iso(ended_at)},
                        headers=hdr(staff_id))
        assert r.status_code == 200, r.text
    return shift_id


def post_event(client, staff_id, subject_type, subject_id, category, occurred_at,
               key=None, **flags):
    payload = flags.pop("payload", {"note": f"{category} 记录"})
    body = {
        "idempotency_key": key or uuid.uuid4().hex,
        "subject_type": subject_type,
        "subject_id": subject_id,
        "category": category,
        "occurred_at": iso(occurred_at),
        "payload": payload,
    }
    body.update(flags)
    return client.post("/events", json=body, headers=hdr(staff_id))


def make_handover(client, outgoing_id, incoming_id, shift_id) -> int:
    r = client.post("/handovers",
                    json={"shift_id": shift_id, "incoming_staff_id": incoming_id},
                    headers=hdr(outgoing_id))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def submit_handover(client, staff_id, handover_id) -> dict:
    r = client.post(f"/handovers/{handover_id}/submit", headers=hdr(staff_id))
    assert r.status_code == 200, r.text
    return r.json()


def confirm_all(client, staff_id, handover_id) -> None:
    detail = client.get(f"/handovers/{handover_id}", headers=hdr(staff_id)).json()
    for item in detail["items"]:
        if item["status"] == "pending":
            r = client.post(f"/handovers/{handover_id}/items/{item['id']}/confirm",
                            headers=hdr(staff_id))
            assert r.status_code == 200, r.text


def sign(client, staff_id, handover_id) -> dict:
    r = client.post(f"/handovers/{handover_id}/sign", headers=hdr(staff_id))
    assert r.status_code == 200, r.text
    return r.json()
