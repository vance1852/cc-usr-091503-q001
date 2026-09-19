"""交接流程：逐项确认、重要提醒签收、签署后只能更正。"""
from datetime import timedelta

from conftest import (confirm_all, hdr, make_baby, make_handover, make_mother,
                      make_shift, make_staff, post_event, sign, submit_handover,
                      utcnow)


def _setup_shift_with_events(client):
    outgoing = make_staff(client, "交班护士")
    incoming = make_staff(client, "接班护士")
    leader = make_staff(client, "护理组长", role="leader")
    mother = make_mother(client)
    baby = make_baby(client, mother)
    now = utcnow()
    shift = make_shift(client, outgoing, now - timedelta(hours=8), now - timedelta(hours=1))
    # 需跟进的喂养事件
    r = post_event(client, outgoing, "baby", baby, "feeding", now - timedelta(hours=3),
                   needs_followup=True, payload={"note": "奶量下降，需观察", "amount_ml": 40})
    followup_event = r.json()["id"]
    # 重要提醒：母亲用药协助
    r = post_event(client, outgoing, "mother", mother, "medication", now - timedelta(hours=2),
                   is_important=True, payload={"note": "22:00 需再服一次"})
    reminder_event = r.json()["id"]
    # 普通事件，不产生交接事项
    post_event(client, outgoing, "baby", baby, "sleep", now - timedelta(hours=4))
    return outgoing, incoming, leader, mother, baby, shift, followup_event, reminder_event


def test_full_handover_flow(client):
    outgoing, incoming, leader, mother, baby, shift, followup_ev, reminder_ev = \
        _setup_shift_with_events(client)

    # 重要提醒在签收前挂在待签收列表
    pending = client.get("/reminders/pending", headers=hdr(incoming)).json()
    assert [e["id"] for e in pending] == [reminder_ev]

    hid = make_handover(client, outgoing, incoming, shift)
    detail = submit_handover(client, outgoing, hid)
    assert detail["status"] == "submitted"
    items = {i["item_type"]: i for i in detail["items"]}
    assert set(items) == {"followup", "reminder"}
    assert items["followup"]["care_event_id"] == followup_ev
    assert items["reminder"]["care_event_id"] == reminder_ev

    # 未确认完不能签署
    r = client.post(f"/handovers/{hid}/sign", headers=hdr(incoming))
    assert r.status_code == 409
    assert r.json()["detail"]["pending_item_ids"]

    # 交班人无权确认
    r = client.post(f"/handovers/{hid}/items/{items['followup']['id']}/confirm",
                    headers=hdr(outgoing))
    assert r.status_code == 403

    # 接班人逐项确认；重复确认被拒绝
    assert client.post(f"/handovers/{hid}/items/{items['followup']['id']}/confirm",
                       headers=hdr(incoming)).status_code == 200
    assert client.post(f"/handovers/{hid}/items/{items['followup']['id']}/confirm",
                       headers=hdr(incoming)).status_code == 409
    assert client.post(f"/handovers/{hid}/items/{items['reminder']['id']}/confirm",
                       headers=hdr(incoming)).status_code == 200

    # 确认提醒事项即签收，待签收列表清空
    assert client.get("/reminders/pending", headers=hdr(incoming)).json() == []

    detail = sign(client, incoming, hid)
    assert detail["status"] == "signed" and detail["signed_at"]

    # 签署后该时段被锁定，只能走更正
    r = post_event(client, outgoing, "baby", baby, "excretion",
                   utcnow() - timedelta(hours=2))
    assert r.status_code == 409 and "更正" in r.json()["detail"]

    # 组长视图：无未确认事项、责任人齐全
    summary = client.get(f"/handovers/{hid}/leader-summary", headers=hdr(leader)).json()
    assert summary["unconfirmed_items"] == []
    assert {s["name"] for s in summary["responsible_staff"]} == {"交班护士"}
    assert {c["confirmed_by_name"] for c in summary["confirmations"]} == {"接班护士"}


def test_reminder_persists_until_explicitly_acknowledged(client):
    outgoing, incoming, leader, mother, baby, shift, _, reminder_ev = \
        _setup_shift_with_events(client)
    # 不交接、不签收：提醒一直存在
    for _ in range(2):
        pending = client.get("/reminders/pending", headers=hdr(incoming)).json()
        assert [e["id"] for e in pending] == [reminder_ev]
    # 明确签收后消失
    r = client.post(f"/events/{reminder_ev}/acknowledge", headers=hdr(incoming))
    assert r.status_code == 200
    assert client.get("/reminders/pending", headers=hdr(incoming)).json() == []
    # 签收后再提交交接，不再生成提醒事项
    hid = make_handover(client, outgoing, incoming, shift)
    detail = submit_handover(client, outgoing, hid)
    assert {i["item_type"] for i in detail["items"]} == {"followup"}


def test_signed_handover_changes_only_via_corrections(client):
    outgoing, incoming, leader, mother, baby, shift, followup_ev, _ = \
        _setup_shift_with_events(client)
    hid = make_handover(client, outgoing, incoming, shift)
    submit_handover(client, outgoing, hid)
    confirm_all(client, incoming, hid)
    sign(client, incoming, hid)

    # amend 更正：说明差异
    r = client.post(f"/events/{followup_ev}/corrections",
                    json={"action": "amend", "reason": "奶量誊写错误，实为 60ml",
                          "new_payload": {"note": "奶量下降，需观察", "amount_ml": 60}},
                    headers=hdr(outgoing))
    assert r.status_code == 201
    event = client.get(f"/events/{followup_ev}", headers=hdr(leader)).json()
    assert event["status"] == "corrected"
    assert event["payload"]["amount_ml"] == 40  # 原始记录保留
    assert event["corrections"][0]["new_payload"]["amount_ml"] == 60

    # 漏录事件通过交接单的 add 更正补登
    missed = {
        "reason": "离线在纸卡上记录，交接时漏登",
        "event": {
            "idempotency_key": "missed-1",
            "subject_type": "baby",
            "subject_id": baby,
            "category": "excretion",
            "occurred_at": (utcnow() - timedelta(hours=2)).isoformat(),
            "payload": {"note": "大便一次，性状正常"},
        },
    }
    r = client.post(f"/handovers/{hid}/corrections", json=missed, headers=hdr(outgoing))
    assert r.status_code == 201
    assert r.json()["is_backfill"] is True

    # 组长视图能看到完整更正经过与责任人
    summary = client.get(f"/handovers/{hid}/leader-summary", headers=hdr(leader)).json()
    assert [c["action"] for c in summary["corrections"]] == ["amend", "add"]
    assert all(c["corrected_by_name"] == "交班护士" for c in summary["corrections"])
    assert summary["corrections"][0]["reason"] == "奶量誊写错误，实为 60ml"


def test_void_correction_removes_event_from_timeline(client):
    outgoing, incoming, leader, mother, baby, shift, followup_ev, _ = \
        _setup_shift_with_events(client)
    hid = make_handover(client, outgoing, incoming, shift)
    submit_handover(client, outgoing, hid)
    confirm_all(client, incoming, hid)
    sign(client, incoming, hid)

    r = client.post(f"/events/{followup_ev}/corrections",
                    json={"action": "void", "reason": "误录到他床"},
                    headers=hdr(leader))
    assert r.status_code == 201
    tl = client.get("/timeline", params={"subject_type": "baby", "subject_id": baby},
                    headers=hdr(leader)).json()
    assert followup_ev not in [e["id"] for e in tl]


def test_manual_item_and_one_handover_per_shift(client):
    outgoing, incoming, leader, mother, baby, shift, _, _ = _setup_shift_with_events(client)
    hid = make_handover(client, outgoing, incoming, shift)
    r = client.post(f"/handovers/{hid}/items",
                    json={"description": "凌晨 3 点复查黄疸值"}, headers=hdr(outgoing))
    assert r.status_code == 201
    detail = submit_handover(client, outgoing, hid)
    assert "manual" in {i["item_type"] for i in detail["items"]}
    # 同一班次不能重复建交接单
    r = client.post("/handovers", json={"shift_id": shift, "incoming_staff_id": incoming},
                    headers=hdr(outgoing))
    assert r.status_code == 409
