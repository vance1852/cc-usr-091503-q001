"""家属端：只展示中心确认且允许共享的内容；更正后展示生效内容。"""
from datetime import timedelta

from conftest import (confirm_all, hdr, make_baby, make_handover, make_mother,
                      make_shift, make_staff, post_event, sign, submit_handover,
                      utcnow)


def _setup(client):
    nurse = make_staff(client, "护士")
    leader = make_staff(client, "护理组长", role="leader")
    mother = make_mother(client)
    baby = make_baby(client, mother)
    now = utcnow()
    make_shift(client, nurse, now - timedelta(hours=2))
    return nurse, leader, mother, baby, now


def test_family_sees_only_confirmed_and_shareable(client):
    nurse, leader, mother, baby, now = _setup(client)
    e1 = post_event(client, nurse, "baby", baby, "feeding",
                    now - timedelta(minutes=50)).json()["id"]
    e2 = post_event(client, nurse, "baby", baby, "sleep",
                    now - timedelta(minutes=40)).json()["id"]
    e3 = post_event(client, nurse, "baby", baby, "note",
                    now - timedelta(minutes=30)).json()["id"]

    # 未经中心确认：家属端为空
    assert client.get("/family/timeline",
                      params={"subject_type": "baby", "subject_id": baby}).json() == []

    # 普通护士无权审核
    r = client.post(f"/events/{e1}/family-review", json={"shareable": True},
                    headers=hdr(nurse))
    assert r.status_code == 403

    # 组长确认并允许共享 e1；e2 确认但不共享；e3 不处理
    assert client.post(f"/events/{e1}/family-review", json={"shareable": True},
                       headers=hdr(leader)).status_code == 200
    assert client.post(f"/events/{e2}/family-review", json={"shareable": False},
                       headers=hdr(leader)).status_code == 200

    tl = client.get("/family/timeline",
                    params={"subject_type": "baby", "subject_id": baby}).json()
    assert [e["id"] for e in tl] == [e1]
    # 家属端不暴露工作人员信息
    assert "recorded_by" not in tl[0] and "shift_id" not in tl[0]

    # 取消共享后立即不可见
    client.post(f"/events/{e1}/family-review", json={"shareable": False},
                headers=hdr(leader))
    assert client.get("/family/timeline",
                      params={"subject_type": "baby", "subject_id": baby}).json() == []


def test_family_sees_corrected_content(client):
    nurse, leader, mother, baby, now = _setup(client)
    e1 = post_event(client, nurse, "baby", baby, "feeding", now - timedelta(minutes=30),
                    payload={"note": "奶量 80ml", "amount_ml": 80}).json()["id"]
    client.post(f"/events/{e1}/family-review", json={"shareable": True},
                headers=hdr(leader))
    # 更正奶量后，家属端展示生效内容
    r = client.post(f"/events/{e1}/corrections",
                    json={"action": "amend", "reason": "奶量誊写错误",
                          "new_payload": {"note": "奶量 60ml", "amount_ml": 60}},
                    headers=hdr(nurse))
    assert r.status_code == 201
    tl = client.get("/family/timeline",
                    params={"subject_type": "baby", "subject_id": baby}).json()
    assert tl[0]["payload"]["amount_ml"] == 60
