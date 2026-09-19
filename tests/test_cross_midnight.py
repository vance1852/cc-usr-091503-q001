"""跨午夜交接：夜班 20:00 → 次日 08:00，事件与时间线跨越零点。"""
from datetime import timedelta

from conftest import (confirm_all, hdr, iso, make_baby, make_handover,
                      make_mother, make_shift, make_staff, post_event, sign,
                      submit_handover, utcnow)


def test_cross_midnight_handover(client):
    nurse = make_staff(client, "夜班护士")
    incoming = make_staff(client, "白班护士")
    leader = make_staff(client, "护理组长", role="leader")
    mother = make_mother(client)
    baby = make_baby(client, mother)

    # 构造跨午夜的夜班：昨晚 20:00 → 今晨 08:00（相对当前时间，任意日期可运行）
    night_start = (utcnow() - timedelta(days=1)).replace(hour=20, minute=0,
                                                         second=0, microsecond=0)
    night_end = night_start + timedelta(hours=12)
    shift = make_shift(client, nurse, night_start, night_end)

    # 午夜前 23:55 喂养、午夜后 00:15 入睡、凌晨 02:00 异常（需跟进）
    r1 = post_event(client, nurse, "baby", baby, "feeding",
                    night_start + timedelta(hours=3, minutes=55),
                    payload={"note": "睡前奶", "amount_ml": 90})
    r2 = post_event(client, nurse, "baby", baby, "sleep",
                    night_start + timedelta(hours=4, minutes=15),
                    payload={"note": "入睡"})
    r3 = post_event(client, nurse, "mother", mother, "mood",
                    night_start + timedelta(hours=6),
                    needs_followup=True, is_abnormal=True,
                    payload={"note": "情绪低落哭泣，需持续关注"})
    assert r1.status_code == r2.status_code == r3.status_code == 201
    assert all(r.json()["shift_id"] == shift for r in (r1, r2, r3))

    # 时间线按实际发生时间跨零点排序
    tl = client.get("/timeline", params={"subject_type": "baby", "subject_id": baby},
                    headers=hdr(nurse)).json()
    assert [e["category"] for e in tl] == ["feeding", "sleep"]
    assert tl[0]["occurred_at"][:10] != tl[1]["occurred_at"][:10]  # 分属两天

    # 交接时段跨午夜，零点两侧的事项都进入交接单
    hid = make_handover(client, nurse, incoming, shift)
    detail = submit_handover(client, nurse, hid)
    assert detail["period_start"][:10] != detail["period_end"][:10]
    assert [i["care_event_id"] for i in detail["items"]] == [r3.json()["id"]]

    # 组长视图：异常变化可见
    summary = client.get(f"/handovers/{hid}/leader-summary", headers=hdr(leader)).json()
    assert [e["id"] for e in summary["abnormal_events"]] == [r3.json()["id"]]
    assert summary["unconfirmed_items"][0]["item_type"] == "followup"

    confirm_all(client, incoming, hid)
    assert sign(client, incoming, hid)["status"] == "signed"
    summary = client.get(f"/handovers/{hid}/leader-summary", headers=hdr(leader)).json()
    assert summary["unconfirmed_items"] == []
