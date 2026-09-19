"""重要提醒：被明确接收前不消失；未接收则结转到下一班次交接。"""

from conftest import T, add_event, hdr


def test_unacked_important_reminder_enters_handover_items(env):
    c = env.client
    e = add_event(c, env.day["id"], T(9), subject_type="mother", subject_id=env.mother["id"],
                  category="medication", payload={"drug": "头孢"}, is_important=True).json()
    h = c.post("/handovers", json={"shift_id": env.day_shift["id"],
                                   "incoming_staff_id": env.night["id"]}, headers=hdr(env.day["id"])).json()
    assert any(it["event_id"] == e["id"] and it["kind"] == "important_reminder"
               for it in h["items"])


def test_acked_reminder_no_longer_appears(env):
    c = env.client
    e = add_event(c, env.day["id"], T(9), subject_type="mother", subject_id=env.mother["id"],
                  category="medication", payload={"drug": "头孢"}, is_important=True).json()
    # 夜班护士明确接收
    r = c.post(f"/events/{e['id']}/ack", headers=hdr(env.night["id"]))
    assert r.status_code == 200
    assert any(a["staff_id"] == env.night["id"] for a in r.json()["acknowledged_by"])
    # 重复接收幂等，不产生第二条接收记录
    r2 = c.post(f"/events/{e['id']}/ack", headers=hdr(env.night["id"]))
    assert len(r2.json()["acknowledged_by"]) == 1
    # 已接收的提醒不再进入交接事项
    h = c.post("/handovers", json={"shift_id": env.day_shift["id"],
                                   "incoming_staff_id": env.night["id"]}, headers=hdr(env.day["id"])).json()
    assert all(it["event_id"] != e["id"] for it in h["items"])


def test_ack_only_applies_to_important_events(env):
    e = add_event(env.client, env.day["id"], T(9), category="feeding").json()
    r = env.client.post(f"/events/{e['id']}/ack", headers=hdr(env.night["id"]))
    assert r.status_code == 400


def test_followup_lifecycle(env):
    """需要持续关注的事项：未关闭前进交接清单，关闭后不再出现。"""
    c = env.client
    e = add_event(c, env.day["id"], T(10), subject_type="mother", subject_id=env.mother["id"],
                  category="mood", payload={"mood": "情绪低落"}, requires_followup=True).json()
    h = c.post("/handovers", json={"shift_id": env.day_shift["id"],
                                   "incoming_staff_id": env.night["id"]}, headers=hdr(env.day["id"])).json()
    assert any(it["event_id"] == e["id"] and it["kind"] == "open_followup" for it in h["items"])

    r = c.post(f"/events/{e['id']}/resolve", headers=hdr(env.night["id"]))
    assert r.status_code == 200
    assert r.json()["resolved"] is True
    assert r.json()["resolved_by"] == env.night["id"]
    # 重复关闭 → 409
    assert c.post(f"/events/{e['id']}/resolve", headers=hdr(env.night["id"])).status_code == 409
    # 下一班次交接不再包含该事项
    h2 = c.post("/handovers", json={"shift_id": env.night_shift["id"],
                                    "incoming_staff_id": env.day["id"]}, headers=hdr(env.night["id"])).json()
    assert all(it["event_id"] != e["id"] for it in h2["items"])
