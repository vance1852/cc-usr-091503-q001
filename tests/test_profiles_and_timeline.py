"""建档与统一照护时间线。"""

from datetime import datetime, timedelta, timezone

from conftest import T, add_event, add_shift, hdr


def test_mother_and_baby_have_separate_timelines(env):
    c = env.client
    r1 = add_event(c, env.day["id"], T(1), subject_type="baby", subject_id=env.baby["id"],
                   category="feeding", payload={"amount_ml": 60, "method": "瓶喂"})
    assert r1.status_code == 201
    r2 = add_event(c, env.day["id"], T(2), subject_type="mother", subject_id=env.mother["id"],
                   category="medication", payload={"drug": "益母草颗粒"})
    assert r2.status_code == 201

    baby_tl = c.get("/timeline", params={"subject_type": "baby", "subject_id": env.baby["id"]}).json()
    mother_tl = c.get("/timeline", params={"subject_type": "mother", "subject_id": env.mother["id"]}).json()
    assert [e["category"] for e in baby_tl] == ["feeding"]
    assert [e["category"] for e in mother_tl] == ["medication"]
    assert baby_tl[0]["payload"] == {"amount_ml": 60, "method": "瓶喂"}


def test_all_categories_in_one_timeline_ordered_by_occurred_at(env):
    """喂养/睡眠/排泄/用药/情绪/备注六类事件进入同一时间线，
    按实际发生时间排序，与录入先后无关。"""
    c = env.client
    baby, mother = env.baby["id"], env.mother["id"]
    # 故意乱序录入
    add_event(c, env.day["id"], T(10), subject_type="baby", subject_id=baby, category="sleep")
    add_event(c, env.day["id"], T(3), subject_type="baby", subject_id=baby, category="feeding")
    add_event(c, env.day["id"], T(7), subject_type="baby", subject_id=baby, category="excretion",
              payload={"kind": "大便", "times": 1})
    add_event(c, env.day["id"], T(4), subject_type="mother", subject_id=mother, category="medication")
    add_event(c, env.day["id"], T(1), subject_type="mother", subject_id=mother, category="mood")
    add_event(c, env.day["id"], T(9), subject_type="mother", subject_id=mother, category="note")

    baby_tl = c.get("/timeline", params={"subject_type": "baby", "subject_id": baby}).json()
    assert [e["category"] for e in baby_tl] == ["feeding", "excretion", "sleep"]
    mother_tl = c.get("/timeline", params={"subject_type": "mother", "subject_id": mother}).json()
    assert [e["category"] for e in mother_tl] == ["mood", "medication", "note"]


def test_event_keeps_occurred_and_recorded_time(env):
    """迟到补录：发生时间与录入时间都被保留，且标记 late_entry。"""
    c = env.client
    r = add_event(c, env.day["id"], T(5), category="feeding", payload={"amount_ml": 45})
    body = r.json()
    assert body["occurred_at"].startswith("2026-09-10")
    assert body["recorded_at"]  # 服务端落库时间
    assert body["late_entry"] is True  # 发生时间在历史日期，属于补录

    # 围绕当前时间建班次，及时录入不算迟到
    now = datetime.now(timezone.utc)
    add_shift(c, env.day["id"], (now - timedelta(hours=1)).isoformat(),
              (now + timedelta(hours=1)).isoformat())
    r2 = add_event(c, env.day["id"], (now - timedelta(minutes=3)).isoformat(), category="sleep")
    assert r2.json()["late_entry"] is False


def test_timeline_supports_time_window_filter(env):
    c = env.client
    add_event(c, env.day["id"], T(1), category="feeding")
    add_event(c, env.day["id"], T(5), category="feeding")
    add_event(c, env.day["id"], T(9), category="feeding")
    tl = c.get("/timeline", params={
        "subject_type": "baby", "subject_id": env.baby["id"], "start": T(2), "end": T(8),
    }).json()
    assert len(tl) == 1
    assert tl[0]["occurred_at"].startswith("2026-09-10T13")
