"""统一时间线、班次时段约束与幂等提交。"""
from datetime import timedelta

from conftest import (hdr, iso, make_baby, make_mother, make_shift, make_staff,
                      post_event, utcnow)


def _setup(client):
    nurse = make_staff(client, "责任护士")
    mother = make_mother(client)
    baby = make_baby(client, mother)
    return nurse, mother, baby


def test_mother_and_baby_have_separate_profiles(client):
    nurse, mother, baby = _setup(client)
    m = client.get(f"/mothers/{mother}").json()
    assert m["name"] and [b["id"] for b in m["babies"]] == [baby]
    b = client.get(f"/babies/{baby}").json()
    assert b["mother_id"] == mother


def test_unified_timeline_ordered_by_occurred_at(client):
    nurse, mother, baby = _setup(client)
    now = utcnow()
    make_shift(client, nurse, now - timedelta(hours=2))
    # 故意乱序提交六类事件
    post_event(client, nurse, "baby", baby, "note", now - timedelta(minutes=5))
    post_event(client, nurse, "baby", baby, "sleep", now - timedelta(minutes=50))
    post_event(client, nurse, "baby", baby, "feeding", now - timedelta(minutes=40))
    post_event(client, nurse, "baby", baby, "excretion", now - timedelta(minutes=30))
    post_event(client, nurse, "mother", mother, "medication", now - timedelta(minutes=20))
    post_event(client, nurse, "mother", mother, "mood", now - timedelta(minutes=10))

    baby_tl = client.get("/timeline", params={"subject_type": "baby", "subject_id": baby},
                         headers=hdr(nurse)).json()
    assert [e["category"] for e in baby_tl] == ["sleep", "feeding", "excretion", "note"]
    assert [e["occurred_at"] for e in baby_tl] == sorted(e["occurred_at"] for e in baby_tl)

    mother_tl = client.get("/timeline", params={"subject_type": "mother", "subject_id": mother},
                           headers=hdr(nurse)).json()
    assert [e["category"] for e in mother_tl] == ["medication", "mood"]


def test_event_outside_own_shift_rejected(client):
    nurse, mother, baby = _setup(client)
    other = make_staff(client, "别人")
    now = utcnow()
    make_shift(client, other, now - timedelta(hours=2))  # 只有别人有班次
    r = post_event(client, nurse, "baby", baby, "feeding", now - timedelta(minutes=30))
    assert r.status_code == 403
    assert "本人班次" in r.json()["detail"]


def test_event_before_shift_start_rejected(client):
    nurse, mother, baby = _setup(client)
    now = utcnow()
    make_shift(client, nurse, now - timedelta(hours=1))
    r = post_event(client, nurse, "baby", baby, "feeding", now - timedelta(hours=3))
    assert r.status_code == 403


def test_future_event_rejected(client):
    nurse, mother, baby = _setup(client)
    now = utcnow()
    make_shift(client, nurse, now - timedelta(hours=1))
    r = post_event(client, nurse, "baby", baby, "feeding", now + timedelta(hours=1))
    assert r.status_code == 422


def test_duplicate_submission_does_not_create_second_record(client):
    nurse, mother, baby = _setup(client)
    now = utcnow()
    make_shift(client, nurse, now - timedelta(hours=1))
    key = "feed-0001"
    r1 = post_event(client, nurse, "baby", baby, "feeding", now - timedelta(minutes=5), key=key)
    assert r1.status_code == 201
    r2 = post_event(client, nurse, "baby", baby, "feeding", now - timedelta(minutes=5), key=key)
    assert r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]
    tl = client.get("/timeline", params={"subject_type": "baby", "subject_id": baby},
                    headers=hdr(nurse)).json()
    assert len(tl) == 1


def test_idempotency_key_reused_with_different_content_conflicts(client):
    nurse, mother, baby = _setup(client)
    now = utcnow()
    make_shift(client, nurse, now - timedelta(hours=1))
    key = "dup-key"
    assert post_event(client, nurse, "baby", baby, "feeding",
                      now - timedelta(minutes=5), key=key).status_code == 201
    r = post_event(client, nurse, "baby", baby, "sleep",
                   now - timedelta(minutes=5), key=key)
    assert r.status_code == 409


def test_unauthenticated_rejected(client):
    r = client.get("/timeline", params={"subject_type": "baby", "subject_id": 1},
                   headers=hdr(9999))
    assert r.status_code == 401
