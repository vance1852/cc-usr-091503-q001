"""离线补录：保留双时间、幂等去重、按发生时间排序、重要提醒结转。"""

from conftest import T, add_event, hdr


def test_offline_backfill_preserves_both_timestamps(env):
    c = env.client
    rid = "offline-sync-1"
    r = add_event(c, env.day["id"], T(11, 30), subject_type="mother",
                  subject_id=env.mother["id"], category="medication",
                  payload={"drug": "退烧药"}, is_important=True, request_id=rid)
    assert r.status_code == 201
    body = r.json()
    # 发生时间（19:30）与录入时间（现在）都保留，且标记为迟到补录
    assert body["occurred_at"].startswith("2026-09-10T19:30")
    assert body["recorded_at"] > body["occurred_at"]
    assert body["late_entry"] is True
    # 归属发生时刻所在的班次（白班），而非录入时刻
    assert body["shift_id"] == env.day_shift["id"]


def test_offline_retry_does_not_duplicate(env):
    c = env.client
    rid = "offline-sync-2"
    for _ in range(3):  # 移动端重试/重复同步
        add_event(c, env.day["id"], T(10), category="feeding",
                  payload={"amount_ml": 55}, request_id=rid)
    tl = c.get("/timeline", params={"subject_type": "baby", "subject_id": env.baby["id"]}).json()
    assert len(tl) == 1


def test_backfilled_event_sorts_by_occurred_at(env):
    c = env.client
    # 先录入夜班 20:30 的事件，再补录白班 19:30 的事件
    add_event(c, env.night["id"], T(12, 30), subject_type="mother",
              subject_id=env.mother["id"], category="note", payload={"text": "夜间观察"})
    late = add_event(c, env.day["id"], T(11, 30), subject_type="mother",
                     subject_id=env.mother["id"], category="medication",
                     payload={"drug": "退烧药"}).json()
    tl = c.get("/timeline", params={"subject_type": "mother", "subject_id": env.mother["id"]}).json()
    assert tl[0]["id"] == late["id"]  # 后录入的 19:30 事件排在 20:30 之前


def test_unacked_backfilled_reminder_carries_to_next_handover(env):
    """交接签署后才补录上来的重要提醒，自动结转到下一班次的交接清单。"""
    c = env.client
    # 白班交接已签署（当时无未决事项）
    h1 = c.post("/handovers", json={"shift_id": env.day_shift["id"],
                                    "incoming_staff_id": env.night["id"]}, headers=hdr(env.day["id"])).json()
    assert c.post(f"/handovers/{h1['id']}/sign", headers=hdr(env.night["id"])).status_code == 200

    # 离线补录：白班 19:30 的重要提醒，交班后才同步
    late = add_event(c, env.day["id"], T(11, 30), subject_type="mother",
                     subject_id=env.mother["id"], category="medication",
                     payload={"drug": "退烧药"}, is_important=True).json()

    # 夜班交班时，这条未被接收的提醒出现在夜班交接清单里
    h2 = c.post("/handovers", json={"shift_id": env.night_shift["id"],
                                    "incoming_staff_id": env.day["id"]}, headers=hdr(env.night["id"])).json()
    carried = [it for it in h2["items"] if it["event_id"] == late["id"]]
    assert len(carried) == 1
    assert carried[0]["kind"] == "important_reminder"
