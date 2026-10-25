"""Failure handling: lost responses, outages, bad read-backs, crashed workers, restarts."""

import pytest
from psycopg.rows import dict_row

from app.actions.dispatcher import dispatch_once
from app.db.core import tx
from app.worker import queue
from tests.integration.conftest import demo_pdfs, key, upload

pytestmark = pytest.mark.integration


def _one_clean(api, db_urls):
    p = next(p for p in demo_pdfs(db_urls) if "-clean-ok-001" in p.name)
    return upload(api, db_urls, p)


def _events(api, db_urls, doc_id):
    return [e["event"] for e in api.get(f"/documents/{doc_id}", headers=key(db_urls, "acme/approver")).json()["audit"]]


def test_lost_response_after_commit_is_recovered_without_duplicate(api, worker, ap_http, db_urls):
    ap_http.post("/_faults", json={"lose_response_after_commit": 1})
    doc = _one_clean(api, db_urls)
    worker.drain()
    d = api.get(f"/documents/{doc['id']}", headers=key(db_urls, "acme/approver")).json()
    assert d["status"] == "executed", d
    assert "execution_recovered_by_status_query" in _events(api, db_urls, doc["id"])
    assert len(ap_http.get("/_state").json()["bills"]) == 1


def test_outage_retries_then_dead_letters(api, worker, ap_http, db_urls, admin_conn):
    ap_http.post("/_faults", json={"fail_before_commit": 100})
    doc = _one_clean(api, db_urls)
    for _ in range(10):
        worker.drain()
        admin_conn.execute("UPDATE outbox SET next_attempt_at = now()")  # skip the backoff wait
    d = api.get(f"/documents/{doc['id']}", headers=key(db_urls, "acme/approver")).json()
    assert d["status"] == "failed" and d["outbox"][0]["status"] == "dead"
    assert "execution_dead_lettered" in _events(api, db_urls, doc["id"])
    assert ap_http.get("/_state").json()["bills"] == []


def test_transient_outage_succeeds_on_retry(api, worker, ap_http, db_urls, admin_conn):
    ap_http.post("/_faults", json={"fail_before_commit": 2})
    doc = _one_clean(api, db_urls)
    for _ in range(4):
        worker.drain()
        admin_conn.execute("UPDATE outbox SET next_attempt_at = now()")
    d = api.get(f"/documents/{doc['id']}", headers=key(db_urls, "acme/approver")).json()
    assert d["status"] == "executed" and d["outbox"][0]["attempts"] == 3
    assert len(ap_http.get("/_state").json()["bills"]) == 1


def test_read_back_mismatch_is_not_marked_executed(api, worker, ap_http, db_urls):
    ap_http.post("/_faults", json={"wrong_status_on_read": 1})
    doc = _one_clean(api, db_urls)
    worker.drain()
    d = api.get(f"/documents/{doc['id']}", headers=key(db_urls, "acme/approver")).json()
    assert d["status"] == "failed" and "read-back mismatch" in d["error"]


def test_crash_before_commit_resends_same_key(api, worker, ap_http, db_urls, admin_conn):
    """Simulate a dispatcher that called AP and died before committing: the row stays pending."""
    _one_clean(api, db_urls)
    worker.process_one_job()  # analyze -> approve -> outbox row, not yet dispatched
    row = admin_conn.execute("SELECT * FROM outbox").fetchone()
    from app.actions.ap_client import APClient
    APClient(client=ap_http).create_bill(row["idempotency_key"], row["payload"])  # the "lost" first attempt
    assert dispatch_once(worker.ap) == "confirmed"
    assert len(ap_http.get("/_state").json()["bills"]) == 1


def test_skip_locked_gives_concurrent_workers_different_jobs(api, db_urls):
    a = upload(api, db_urls, demo_pdfs(db_urls)[0])
    b = upload(api, db_urls, demo_pdfs(db_urls)[1])
    with tx() as c1, tx() as c2:
        j1 = queue.claim(c1, "w1", 60)
        j2 = queue.claim(c2, "w2", 60)
        assert j1 and j2 and j1["id"] != j2["id"]
        assert {str(j1["document_id"]), str(j2["document_id"])} == {a["id"], b["id"]}


def test_sweeper_requeues_expired_lease(api, db_urls, admin_conn):
    upload(api, db_urls, demo_pdfs(db_urls)[0])
    with tx() as conn:
        job = queue.claim(conn, "dead-worker", 60)
    admin_conn.execute("UPDATE jobs SET lease_until = now() - interval '1 second' WHERE id = %s", (job["id"],))
    with tx() as conn:
        assert queue.sweep(conn) == 1
    assert admin_conn.execute("SELECT status FROM jobs WHERE id = %s", (job["id"],)).fetchone()["status"] == "queued"


def test_review_pause_survives_worker_restart(api, ap_http, db_urls, clean):
    """Pause at review with one worker, resume with a brand-new worker and a fresh Postgres checkpointer."""
    import psycopg
    from langgraph.checkpoint.postgres import PostgresSaver

    from app.actions.ap_client import APClient
    from app.worker.main import Worker

    def new_worker(name):
        conn = psycopg.connect(db_urls["app"], autocommit=True, prepare_threshold=0, row_factory=dict_row)
        return Worker(checkpointer=PostgresSaver(conn), ap_client=APClient(client=ap_http), worker_id=name), conn

    w1, c1 = new_worker("w1")
    p = next(p for p in demo_pdfs(db_urls) if "-clean-ok-000" in p.name)
    doc = upload(api, db_urls, p)
    w1.drain()
    c1.close()
    del w1
    d = api.get(f"/documents/{doc['id']}", headers=key(db_urls, "acme/approver")).json()
    assert d["status"] == "awaiting_review"
    api.post(f"/documents/{doc['id']}/review", json={"action": "approve", "expected_version": d["version"]},
             headers=key(db_urls, "acme/approver"))
    w2, c2 = new_worker("w2")
    w2.drain()
    c2.close()
    d = api.get(f"/documents/{doc['id']}", headers=key(db_urls, "acme/approver")).json()
    assert d["status"] == "executed", d
