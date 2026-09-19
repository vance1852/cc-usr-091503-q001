"""跨午夜交接：事项生成、逐项确认、签署、组长总览。"""

from conftest import T, add_event, hdr


def _night_events(env):
    """夜班 20:00→次日08:00 期间发生的五件事，横跨午夜。"""
    c, night = env.client, env.night["id"]
    add_event(c, night, T(12, 30), category="feeding", payload={"amount_ml": 70})          # 20:30
    add_event(c, night, T(15, 59), category="sleep", payload={"state": "入睡"})            # 23:59
    med = add_event(c, night, T(16, 10), subject_type="mother", subject_id=env.mother["id"],
                    category="medication", payload={"drug": "钙片"}, is_important=True)    # 00:10 重要提醒
    add_event(c, night, T(18), subject_type="mother", subject_id=env.mother["id"],
              category="mood", payload={"mood": "焦虑"}, requires_followup=True)           # 02:00 需持续关注
    add_event(c, night, T(35, 30), category="note", abnormal=True,
              payload={"text": "体温 37.8℃，复测中"})                                      # 次日07:30 异常
    return med.json()


def test_cross_midnight_handover(env):
    c = env.client
    med = _night_events(env)

    # 交班：夜班 → 白班
    r = c.post("/handovers", json={"shift_id": env.night_shift["id"],
                                   "incoming_staff_id": env.day["id"]}, headers=hdr(env.night["id"]))
    assert r.status_code == 201
    handover = r.json()
    assert handover["status"] == "pending"
    assert handover["outgoing_staff_id"] == env.night["id"]
    # 未接收的重要提醒与未关闭的关注事项进入交接清单
    kinds = {(it["kind"], it["event"]["category"]) for it in handover["items"]}
    assert ("important_reminder", "medication") in kinds
    assert ("open_followup", "mood") in kinds

    # 未全部确认前不能签署
    assert c.post(f"/handovers/{handover['id']}/sign", headers=hdr(env.day["id"])).status_code == 400

    # 接班人逐项确认
    for it in handover["items"]:
        cr = c.post(f"/handovers/{handover['id']}/items/{it['id']}/confirm",
                    headers=hdr(env.day["id"]))
        assert cr.status_code == 200
        assert cr.json()["status"] == "confirmed"

    # 确认"重要提醒"事项即明确接收该提醒
    med_after = c.get(f"/events/{med['id']}").json()
    assert any(a["staff_id"] == env.day["id"] for a in med_after["acknowledged_by"])

    # 签署
    s = c.post(f"/handovers/{handover['id']}/sign", headers=hdr(env.day["id"]))
    assert s.status_code == 200
    assert s.json()["status"] == "signed"
    assert s.json()["signed_at"]

    # 护理组长从这次交班直接看到：责任人、异常变化、无遗留未确认事项
    lv = c.get(f"/handovers/{handover['id']}/lead-view", headers=hdr(env.lead["id"]))
    assert lv.status_code == 200
    view = lv.json()
    assert view["responsible"]["outgoing"]["id"] == env.night["id"]
    assert view["responsible"]["incoming"]["id"] == env.day["id"]
    assert view["unconfirmed_items"] == []
    assert len(view["abnormal_events"]) == 1
    assert view["abnormal_events"][0]["payload"]["text"].startswith("体温")
    assert view["summary"]["events_in_period"] == 5  # 跨午夜的事件全部计入本班次


def test_handover_requires_distinct_incoming_staff(env):
    c = env.client
    r = c.post("/handovers", json={"shift_id": env.night_shift["id"],
                                   "incoming_staff_id": env.night["id"]}, headers=hdr(env.night["id"]))
    assert r.status_code == 400


def test_only_outgoing_owner_creates_handover(env):
    # 白班护士不能为夜班班次创建交接
    r = env.client.post("/handovers", json={"shift_id": env.night_shift["id"],
                                            "incoming_staff_id": env.day["id"]},
                        headers=hdr(env.day["id"]))
    assert r.status_code == 403


def test_only_incoming_staff_confirms_items(env):
    c = env.client
    add_event(c, env.night["id"], T(13), is_important=True, category="note",
              payload={"text": "注意观察"})
    h = c.post("/handovers", json={"shift_id": env.night_shift["id"],
                                   "incoming_staff_id": env.day["id"]}, headers=hdr(env.night["id"])).json()
    item_id = h["items"][0]["id"]
    # 交班人不能替接班人确认
    assert c.post(f"/handovers/{h['id']}/items/{item_id}/confirm",
                  headers=hdr(env.night["id"])).status_code == 403
    # 无关人员（组长）也不能确认
    assert c.post(f"/handovers/{h['id']}/items/{item_id}/confirm",
                  headers=hdr(env.lead["id"])).status_code == 403
