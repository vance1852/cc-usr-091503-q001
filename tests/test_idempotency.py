"""重复提交（幂等）：不产生第二条记录。"""

from conftest import T, add_event


def test_duplicate_submission_returns_same_record(env):
    c = env.client
    rid = "feed-2026-09-10-001"
    r1 = add_event(c, env.day["id"], T(3), request_id=rid, payload={"amount_ml": 50})
    r2 = add_event(c, env.day["id"], T(3), request_id=rid, payload={"amount_ml": 50})
    assert r1.status_code == 201
    assert r2.status_code == 200  # 幂等命中，返回原记录
    assert r1.json()["id"] == r2.json()["id"]

    tl = c.get("/timeline", params={"subject_type": "baby", "subject_id": env.baby["id"]}).json()
    assert len(tl) == 1  # 没有生成第二条记录


def test_same_request_id_with_different_content_conflicts(env):
    c = env.client
    rid = "conflict-1"
    r1 = add_event(c, env.day["id"], T(3), request_id=rid, payload={"amount_ml": 50})
    assert r1.status_code == 201
    r2 = add_event(c, env.day["id"], T(3), request_id=rid, payload={"amount_ml": 80})
    assert r2.status_code == 409
    r3 = add_event(c, env.day["id"], T(4), request_id=rid, payload={"amount_ml": 50})
    assert r3.status_code == 409  # 发生时间不同同样冲突
