"""并发安全：并发确认、并发签署、并发重复提交。

每个线程使用独立的 TestClient（独立事件循环与数据库连接），
对同一 SQLite 数据库文件发起真实并发请求。
"""

import concurrent.futures as cf

from fastapi.testclient import TestClient

from app.main import create_app
from conftest import T, add_event, hdr

THREADS = 8


def _thread_client(db_path):
    return TestClient(create_app(db_path))


def test_concurrent_item_confirmation_only_one_wins(client, db_path, env):
    e = add_event(client, env.day["id"], T(9), subject_type="mother",
                  subject_id=env.mother["id"], category="medication",
                  payload={"drug": "钙片"}, is_important=True).json()
    h = client.post("/handovers", json={"shift_id": env.day_shift["id"],
                                        "incoming_staff_id": env.night["id"]},
                    headers=hdr(env.day["id"])).json()
    item_id = h["items"][0]["id"]

    def confirm(_):
        with _thread_client(db_path) as cc:
            return cc.post(f"/handovers/{h['id']}/items/{item_id}/confirm",
                           headers=hdr(env.night["id"])).status_code

    with cf.ThreadPoolExecutor(max_workers=THREADS) as ex:
        codes = list(ex.map(confirm, range(THREADS)))

    assert codes.count(200) == 1
    assert codes.count(409) == THREADS - 1

    after = client.get(f"/handovers/{h['id']}").json()
    item = after["items"][0]
    assert item["status"] == "confirmed"
    assert item["confirmed_by"] == env.night["id"]
    # 重要提醒只产生一条接收记录
    ev = client.get(f"/events/{e['id']}").json()
    assert len(ev["acknowledged_by"]) == 1


def test_concurrent_sign_only_one_wins(client, db_path, env):
    h = client.post("/handovers", json={"shift_id": env.day_shift["id"],
                                        "incoming_staff_id": env.night["id"]},
                    headers=hdr(env.day["id"])).json()

    def sign(_):
        with _thread_client(db_path) as cc:
            return cc.post(f"/handovers/{h['id']}/sign", headers=hdr(env.night["id"])).status_code

    with cf.ThreadPoolExecutor(max_workers=THREADS) as ex:
        codes = list(ex.map(sign, range(THREADS)))

    assert codes.count(200) == 1
    assert codes.count(409) == THREADS - 1
    assert client.get(f"/handovers/{h['id']}").json()["status"] == "signed"


def test_concurrent_duplicate_event_creation(client, db_path, env):
    body = {
        "client_request_id": "concurrent-sync-1",
        "subject_type": "baby",
        "subject_id": env.baby["id"],
        "category": "feeding",
        "occurred_at": T(3),
        "payload": {"amount_ml": 50},
    }

    def submit(_):
        with _thread_client(db_path) as cc:
            r = cc.post("/events", json=body, headers=hdr(env.day["id"]))
            return r.status_code, r.json()["id"]

    with cf.ThreadPoolExecutor(max_workers=THREADS) as ex:
        results = list(ex.map(submit, range(THREADS)))

    codes, ids = zip(*results)
    assert set(codes) <= {200, 201}
    assert len(set(ids)) == 1  # 全部指向同一条记录
    tl = client.get("/timeline", params={"subject_type": "baby",
                                         "subject_id": env.baby["id"]}).json()
    assert len(tl) == 1
