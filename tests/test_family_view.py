"""家属端：只展示经过中心确认且允许共享的内容。"""

from conftest import T, add_event, hdr


def test_family_sees_only_confirmed_and_shareable(env):
    c = env.client
    baby = env.baby["id"]
    # 1. 允许共享但未经中心确认 → 不可见
    add_event(c, env.day["id"], T(2), category="feeding",
              payload={"amount_ml": 60}, shareable=True)
    # 2. 允许共享且组长确认 → 可见
    e2 = add_event(c, env.day["id"], T(3), category="sleep",
                   payload={"state": "安睡"}, shareable=True).json()
    r = c.post(f"/events/{e2['id']}/center-review", json={}, headers=hdr(env.lead["id"]))
    assert r.status_code == 200
    assert r.json()["center_confirmed"] is True
    # 3. 已确认但不允许共享 → 不可见
    e3 = add_event(c, env.day["id"], T(4), category="note", payload={"text": "内部备注"}).json()
    c.post(f"/events/{e3['id']}/center-review", json={}, headers=hdr(env.lead["id"]))

    fam = c.get("/family/timeline", params={"subject_type": "baby", "subject_id": baby}).json()
    assert [e["id"] for e in fam] == [e2["id"]]
    # 家属端不暴露内部字段
    assert "recorded_by" not in fam[0]
    assert "is_important" not in fam[0]

    # 员工时间线可见全部
    staff_tl = c.get("/timeline", params={"subject_type": "baby", "subject_id": baby}).json()
    assert len(staff_tl) == 3


def test_center_review_can_toggle_shareable(env):
    c = env.client
    e = add_event(c, env.day["id"], T(2), category="feeding",
                  payload={"amount_ml": 60}).json()  # 未标记共享
    # 组长确认时一并开放共享
    c.post(f"/events/{e['id']}/center-review", json={"shareable": True}, headers=hdr(env.lead["id"]))
    fam = c.get("/family/timeline", params={"subject_type": "baby", "subject_id": env.baby["id"]}).json()
    assert [x["id"] for x in fam] == [e["id"]]
    # 组长也可以收回共享
    c.post(f"/events/{e['id']}/center-review", json={"shareable": False}, headers=hdr(env.lead["id"]))
    fam = c.get("/family/timeline", params={"subject_type": "baby", "subject_id": env.baby["id"]}).json()
    assert fam == []


def test_only_lead_nurse_can_center_review(env):
    e = add_event(env.client, env.day["id"], T(2), shareable=True).json()
    r = env.client.post(f"/events/{e['id']}/center-review", json={}, headers=hdr(env.day["id"]))
    assert r.status_code == 403


def test_lead_view_requires_lead_role(env):
    c = env.client
    h = c.post("/handovers", json={"shift_id": env.day_shift["id"],
                                   "incoming_staff_id": env.night["id"]},
               headers=hdr(env.day["id"])).json()
    assert c.get(f"/handovers/{h['id']}/lead-view", headers=hdr(env.day["id"])).status_code == 403
    assert c.get(f"/handovers/{h['id']}/lead-view", headers=hdr(env.lead["id"])).status_code == 200
