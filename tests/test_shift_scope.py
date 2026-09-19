"""交班人只能提交自己负责时段内的事实。"""

from datetime import datetime, timedelta, timezone

from conftest import T, add_event, add_shift


def test_can_only_submit_facts_within_own_shift(env):
    c = env.client
    # 夜班 20:00 → 次日08:00 内：允许
    assert add_event(c, env.night["id"], T(13)).status_code == 201
    assert add_event(c, env.night["id"], T(16, 30)).status_code == 201  # 跨午夜后 00:30
    # 19:59 不在夜班时段：拒绝
    assert add_event(c, env.night["id"], T(11, 59)).status_code == 403
    # 班次结束时刻（不含）：拒绝
    assert add_event(c, env.night["id"], T(36)).status_code == 403
    # 白班护士不能提交夜班时段的事实
    assert add_event(c, env.day["id"], T(13)).status_code == 403


def test_staff_without_covering_shift_is_rejected(env):
    # 组长没有排班，不能录入事实类事件
    r = add_event(env.client, env.lead["id"], T(5))
    assert r.status_code == 403


def test_cannot_record_future_events(env):
    c = env.client
    now = datetime.now(timezone.utc)
    add_shift(c, env.day["id"], (now - timedelta(hours=1)).isoformat(),
              (now + timedelta(hours=8)).isoformat())
    r = add_event(c, env.day["id"], (now + timedelta(hours=2)).isoformat())
    assert r.status_code == 400


def test_unknown_staff_identity_is_rejected(env):
    r = env.client.post("/events", json={
        "client_request_id": "x-1", "subject_type": "baby", "subject_id": 1,
        "category": "feeding", "occurred_at": T(3),
    }, headers={"X-Staff-Id": "9999"})
    assert r.status_code == 401
    # 缺少身份头
    r2 = env.client.post("/events", json={
        "client_request_id": "x-2", "subject_type": "baby", "subject_id": 1,
        "category": "feeding", "occurred_at": T(3),
    })
    assert r2.status_code == 422
