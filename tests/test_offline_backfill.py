"""离线补录：保留录入时间与发生时间；重试不产生重复；已签署时段走更正补登。"""
from datetime import timedelta

from conftest import (confirm_all, hdr, iso, make_baby, make_handover,
                      make_mother, make_shift, make_staff, post_event, sign,
                      submit_handover, utcnow)


def test_offline_backfill_preserves_both_timestamps(client):
    nurse = make_staff(client, "夜班护士")
    mother = make_mother(client)
    baby = make_baby(client, mother)
    now = utcnow()
    # 班次已结束但尚未交接：离线缓存的事件重新同步
    make_shift(client, nurse, now - timedelta(hours=8), now - timedelta(hours=1))

    occurred = now - timedelta(hours=3)
    r = post_event(client, nurse, "baby", baby, "feeding", occurred,
                   key="offline-1", payload={"note": "纸卡记录奶量", "amount_ml": 80})
    assert r.status_code == 201
    body = r.json()
    assert body["is_backfill"] is True
    assert body["occurred_at"] == iso(occurred)          # 发生时间原样保留
    assert body["recorded_at"] > body["occurred_at"]     # 录入时间为服务端当前时间

    # 客户端断网重试同一批：不产生第二条
    r2 = post_event(client, nurse, "baby", baby, "feeding", occurred,
                    key="offline-1", payload={"note": "纸卡记录奶量", "amount_ml": 80})
    assert r2.status_code == 200 and r2.json()["id"] == body["id"]
    tl = client.get("/timeline", params={"subject_type": "baby", "subject_id": baby},
                    headers=hdr(nurse)).json()
    assert len(tl) == 1


def test_backfill_into_signed_period_requires_correction(client):
    outgoing = make_staff(client, "交班护士")
    incoming = make_staff(client, "接班护士")
    leader = make_staff(client, "护理组长", role="leader")
    mother = make_mother(client)
    baby = make_baby(client, mother)
    now = utcnow()
    shift = make_shift(client, outgoing, now - timedelta(hours=8), now - timedelta(hours=1))
    post_event(client, outgoing, "baby", baby, "sleep", now - timedelta(hours=4))

    hid = make_handover(client, outgoing, incoming, shift)
    submit_handover(client, outgoing, hid)
    confirm_all(client, incoming, hid)
    sign(client, incoming, hid)

    # 签署后迟到的离线事件不能直接写入
    r = post_event(client, outgoing, "baby", baby, "feeding", now - timedelta(hours=3),
                   key="late-1")
    assert r.status_code == 409

    # 通过交接单的更正补登进入时间线，并留下更正经过
    missed = {
        "reason": "设备离线，交接签署后才同步到纸卡记录",
        "event": {
            "idempotency_key": "late-1",
            "subject_type": "baby",
            "subject_id": baby,
            "category": "feeding",
            "occurred_at": iso(now - timedelta(hours=3)),
            "payload": {"note": "补登奶量", "amount_ml": 70},
        },
    }
    r = client.post(f"/handovers/{hid}/corrections", json=missed, headers=hdr(outgoing))
    assert r.status_code == 201
    event = r.json()
    assert event["is_backfill"] is True
    assert event["recorded_at"] > event["occurred_at"]

    # 重复补登同一幂等键：不生成第二条，也不重复记更正
    r = client.post(f"/handovers/{hid}/corrections", json=missed, headers=hdr(outgoing))
    assert r.status_code == 200 and r.json()["id"] == event["id"]

    summary = client.get(f"/handovers/{hid}/leader-summary", headers=hdr(leader)).json()
    adds = [c for c in summary["corrections"] if c["action"] == "add"]
    assert len(adds) == 1
    assert adds[0]["reason"] == "设备离线，交接签署后才同步到纸卡记录"
    assert adds[0]["corrected_by_name"] == "交班护士"


def test_missed_event_outside_handover_period_rejected(client):
    outgoing = make_staff(client, "交班护士")
    incoming = make_staff(client, "接班护士")
    mother = make_mother(client)
    baby = make_baby(client, mother)
    now = utcnow()
    shift = make_shift(client, outgoing, now - timedelta(hours=8), now - timedelta(hours=1))
    hid = make_handover(client, outgoing, incoming, shift)
    submit_handover(client, outgoing, hid)
    confirm_all(client, incoming, hid)
    sign(client, incoming, hid)

    missed = {
        "reason": "补登",
        "event": {
            "idempotency_key": "out-of-period",
            "subject_type": "baby",
            "subject_id": baby,
            "category": "feeding",
            "occurred_at": iso(now - timedelta(hours=20)),  # 不在交接时段内
            "payload": {},
        },
    }
    r = client.post(f"/handovers/{hid}/corrections", json=missed, headers=hdr(outgoing))
    assert r.status_code == 422
