"""已签署的交接只能通过更正事件说明差异。"""

from conftest import T, add_event, hdr


def _signed_handover(env):
    c = env.client
    e = add_event(c, env.day["id"], T(9), subject_type="mother", subject_id=env.mother["id"],
                  category="medication", payload={"drug": "钙片"}, is_important=True).json()
    h = c.post("/handovers", json={"shift_id": env.day_shift["id"],
                                   "incoming_staff_id": env.night["id"]},
               headers=hdr(env.day["id"])).json()
    for it in h["items"]:
        c.post(f"/handovers/{h['id']}/items/{it['id']}/confirm", headers=hdr(env.night["id"]))
    c.post(f"/handovers/{h['id']}/sign", headers=hdr(env.night["id"]))
    return h, e


def test_unsigned_handover_rejects_corrections(env):
    c = env.client
    h = c.post("/handovers", json={"shift_id": env.day_shift["id"],
                                   "incoming_staff_id": env.night["id"]},
               headers=hdr(env.day["id"])).json()
    r = c.post(f"/handovers/{h['id']}/corrections",
               json={"description": "提前更正"}, headers=hdr(env.night["id"]))
    assert r.status_code == 409


def test_signed_handover_is_immutable_except_corrections(env):
    c = env.client
    h, e = _signed_handover(env)
    hid = h["id"]
    item_id = h["items"][0]["id"]

    # 不能再确认事项、不能重复签署、不能为同班次再建交接
    assert c.post(f"/handovers/{hid}/items/{item_id}/confirm",
                  headers=hdr(env.night["id"])).status_code == 409
    assert c.post(f"/handovers/{hid}/sign", headers=hdr(env.night["id"])).status_code == 409
    assert c.post("/handovers", json={"shift_id": env.day_shift["id"],
                                      "incoming_staff_id": env.night["id"]},
                  headers=hdr(env.day["id"])).status_code == 409

    # 差异通过更正事件说明，原事件记录保持不变
    r = c.post(f"/handovers/{hid}/corrections",
               json={"event_id": e["id"],
                     "description": "钙片实际服用时间为 17:20，原记录 17:00 有误"},
               headers=hdr(env.night["id"]))
    assert r.status_code == 201
    original = c.get(f"/events/{e['id']}").json()
    assert original["occurred_at"].startswith("2026-09-10T17:00")

    # 组长总览可见更正经过（含责任人与时间）
    lv = c.get(f"/handovers/{hid}/lead-view", headers=hdr(env.lead["id"])).json()
    assert len(lv["corrections"]) == 1
    corr = lv["corrections"][0]
    assert corr["description"].startswith("钙片实际服用时间")
    assert corr["created_by"] == env.night["id"]
    assert corr["created_at"]
    assert lv["summary"]["correction_count"] == 1


def test_correction_references_must_exist(env):
    c = env.client
    h, _ = _signed_handover(env)
    r = c.post(f"/handovers/{h['id']}/corrections",
               json={"event_id": 99999, "description": "引用不存在的事件"},
               headers=hdr(env.night["id"]))
    assert r.status_code == 404
