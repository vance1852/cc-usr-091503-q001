"""并发安全：并发确认同一事项只有一个成功；并发重复提交只生成一条记录。"""
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

from fastapi.testclient import TestClient

from conftest import (confirm_all, hdr, iso, make_baby, make_handover,
                      make_mother, make_shift, make_staff, post_event,
                      submit_handover, utcnow)


def _submitted_handover(client):
    outgoing = make_staff(client, "交班护士")
    incoming = make_staff(client, "接班护士")
    mother = make_mother(client)
    baby = make_baby(client, mother)
    now = utcnow()
    shift = make_shift(client, outgoing, now - timedelta(hours=8), now - timedelta(hours=1))
    post_event(client, outgoing, "baby", baby, "feeding", now - timedelta(hours=2),
               needs_followup=True)
    hid = make_handover(client, outgoing, incoming, shift)
    detail = submit_handover(client, outgoing, hid)
    item_id = detail["items"][0]["id"]
    return hid, item_id, incoming


def test_concurrent_confirm_only_one_wins(app):
    setup = TestClient(app)
    hid, item_id, incoming = _submitted_handover(setup)

    barrier = threading.Barrier(2)
    clients = [TestClient(app), TestClient(app)]

    def confirm(c):
        barrier.wait(timeout=10)
        return c.post(f"/handovers/{hid}/items/{item_id}/confirm", headers=hdr(incoming))

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(confirm, clients))

    assert sorted(r.status_code for r in results) == [200, 409]
    detail = setup.get(f"/handovers/{hid}", headers=hdr(incoming)).json()
    item = detail["items"][0]
    assert item["status"] == "confirmed"
    assert item["confirmed_by"] == incoming


def test_concurrent_duplicate_event_creates_single_record(app):
    setup = TestClient(app)
    nurse = make_staff(setup)
    mother = make_mother(setup)
    baby = make_baby(setup, mother)
    now = utcnow()
    make_shift(setup, nurse, now - timedelta(hours=1))

    barrier = threading.Barrier(4)
    clients = [TestClient(app) for _ in range(4)]

    def send(c):
        barrier.wait(timeout=10)
        return post_event(c, nurse, "baby", baby, "feeding",
                          now - timedelta(minutes=5), key="offline-batch-7")

    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(send, clients))

    ids = {r.json()["id"] for r in results}
    assert len(ids) == 1
    assert sorted(r.status_code for r in results) == [200, 200, 200, 201]
    tl = setup.get("/timeline", params={"subject_type": "baby", "subject_id": baby},
                   headers=hdr(nurse)).json()
    assert len(tl) == 1


def test_concurrent_sign_only_one_wins(app):
    setup = TestClient(app)
    hid, _, incoming = _submitted_handover(setup)
    confirm_all(setup, incoming, hid)

    barrier = threading.Barrier(2)
    clients = [TestClient(app), TestClient(app)]

    def do_sign(c):
        barrier.wait(timeout=10)
        return c.post(f"/handovers/{hid}/sign", headers=hdr(incoming))

    with ThreadPoolExecutor(max_workers=2) as ex:
        results = list(ex.map(do_sign, clients))

    assert sorted(r.status_code for r in results) == [200, 409]
    detail = setup.get(f"/handovers/{hid}", headers=hdr(incoming)).json()
    assert detail["status"] == "signed"
