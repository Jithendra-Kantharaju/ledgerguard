
"""Integration tests for queues, retries and AP safety."""

import pytest
from psycopg.rows import dict_row

from app.actions.dispatcher import dispatch_once
from app.db.core import tx
from app.worker import queue
from tests.integration.conftest import (
    demo_pdfs,
    key,
    upload,
)

pytestmark = pytest.mark.integration


def _one_clean(api, db_urls):
    pdf = next(
        p
        for p in demo_pdfs(db_urls)
        if "-clean-ok-001" in p.name
    )

    return upload(
        api,
        db_urls,
        pdf,
    )


def _events(
    api,
    db_urls,
    doc_id,
):
    response = api.get(
        f"/documents/{doc_id}",
        headers=key(
            db_urls,
            "acme/approver",
        ),
    )

    return [
        event["event"]
        for event in response.json()["audit"]
    ]


def test_lost_response_after_commit_is_recovered_without_duplicate(
    api,
    worker,
    ap_http,
    db_urls,
):
    ap_http.post(
        "/_faults",
        json={
            "lose_response_after_commit": 1
        },
    )

    doc = _one_clean(
        api,
        db_urls,
    )

    worker.drain()

    response = api.get(
        f"/documents/{doc['id']}",
        headers=key(
            db_urls,
            "acme/approver",
        ),
    )

    document = response.json()

    assert document["status"] == "executed", document

    assert (
        "execution_recovered_by_status_query"
        in _events(
            api,
            db_urls,
            doc["id"],
        )
    )

    assert len(
        ap_http.get(
            "/_state"
        ).json()["bills"]
    ) == 1


def test_outage_retries_then_dead_letters(
    api,
    worker,
    ap_http,
    db_urls,
    admin_conn,
):
    ap_http.post(
        "/_faults",
        json={
            "fail_before_commit": 100
        },
    )

    doc = _one_clean(
        api,
        db_urls,
    )

    for _ in range(10):
        worker.drain()

        admin_conn.execute(
            """
            UPDATE outbox
            SET next_attempt_at = now()
            """
        )

    response = api.get(
        f"/documents/{doc['id']}",
        headers=key(
            db_urls,
            "acme/approver",
        ),
    )

    document = response.json()

    assert document["status"] == "failed"

    assert (
        document["outbox"][0]["status"]
        == "dead"
    )

    assert (
        "execution_dead_lettered"
        in _events(
            api,
            db_urls,
            doc["id"],
        )
    )

    assert (
        ap_http.get(
            "/_state"
        ).json()["bills"]
        == []
    )


def test_transient_outage_succeeds_on_retry(
    api,
    worker,
    ap_http,
    db_urls,
    admin_conn,
):
    ap_http.post(
        "/_faults",
        json={
            "fail_before_commit": 2
        },
    )

    doc = _one_clean(
        api,
        db_urls,
    )

    for _ in range(4):
        worker.drain()

        admin_conn.execute(
            """
            UPDATE outbox
            SET next_attempt_at = now()
            """
        )

    response = api.get(
        f"/documents/{doc['id']}",
        headers=key(
            db_urls,
            "acme/approver",
        ),
    )

    document = response.json()

    assert document["status"] == "executed"

    assert (
        document["outbox"][0]["attempts"]
        == 3
    )

    assert len(
        ap_http.get(
            "/_state"
        ).json()["bills"]
    ) == 1


def test_read_back_mismatch_is_not_marked_executed(
    api,
    worker,
    ap_http,
    db_urls,
):
    ap_http.post(
        "/_faults",
        json={
            "wrong_status_on_read": 1
        },
    )

    doc = _one_clean(
        api,
        db_urls,
    )

    worker.drain()

    response = api.get(
        f"/documents/{doc['id']}",
        headers=key(
            db_urls,
            "acme/approver",
        ),
    )

    document = response.json()

    assert document["status"] == "failed"

    assert (
        "read-back mismatch"
        in document["error"]
    )


def test_crash_before_commit_resends_same_key(
    api,
    worker,
    ap_http,
    db_urls,
    admin_conn,
):
    """
    Simulate an AP bill being created before
    the dispatcher's database commit.
    """

    _one_clean(
        api,
        db_urls,
    )

    worker.process_one_job()

    row = admin_conn.execute(
        """
        SELECT *
        FROM outbox
        """
    ).fetchone()

    from app.actions.ap_client import APClient

    APClient(
        client=ap_http
    ).create_bill(
        row["idempotency_key"],
        row["payload"],
    )

    assert (
        dispatch_once(worker.ap)
        == "confirmed"
    )

    assert len(
        ap_http.get(
            "/_state"
        ).json()["bills"]
    ) == 1


def test_skip_locked_gives_concurrent_workers_different_jobs(
    api,
    db_urls,
):
    a = upload(
        api,
        db_urls,
        demo_pdfs(db_urls)[0],
    )

    b = upload(
        api,
        db_urls,
        demo_pdfs(db_urls)[1],
    )

    with tx() as c1, tx() as c2:
        j1 = queue.claim(
            c1,
            "w1",
            60,
        )

        j2 = queue.claim(
            c2,
            "w2",
            60,
        )

        assert j1 and j2

        assert j1["id"] != j2["id"]

        assert {
            str(j1["document_id"]),
            str(j2["document_id"]),
        } == {
            a["id"],
            b["id"],
        }


def test_sweeper_requeues_expired_lease(
    api,
    db_urls,
    admin_conn,
):
    upload(
        api,
        db_urls,
        demo_pdfs(db_urls)[0],
    )

    with tx() as conn:
        job = queue.claim(
            conn,
            "dead-worker",
            60,
        )

    admin_conn.execute(
        """
        UPDATE jobs
        SET lease_until =
            now() - interval '1 second'
        WHERE id = %s
        """,
        (job["id"],),
    )

    with tx() as conn:
        assert queue.sweep(conn) == 1

    row = admin_conn.execute(
        """
        SELECT status
        FROM jobs
        WHERE id = %s
        """,
        (job["id"],),
    ).fetchone()

    assert row["status"] == "queued"


def test_review_pause_survives_worker_restart(
    api,
    ap_http,
    db_urls,
    clean,
):
    """
    Review starts with one worker and resumes
    after a new worker loads the checkpoint.
    """

    import psycopg

    from langgraph.checkpoint.postgres import (
        PostgresSaver,
    )

    from app.actions.ap_client import APClient
    from app.worker.main import Worker

    def new_worker(name):
        connection = psycopg.connect(
            db_urls["app"],
            autocommit=True,
            prepare_threshold=0,
            row_factory=dict_row,
        )

        instance = Worker(
            checkpointer=PostgresSaver(
                connection
            ),
            ap_client=APClient(
                client=ap_http
            ),
            worker_id=name,
        )

        return instance, connection

    w1, c1 = new_worker("w1")

    pdf = next(
        p
        for p in demo_pdfs(db_urls)
        if "-clean-ok-000" in p.name
    )

    doc = upload(
        api,
        db_urls,
        pdf,
    )

    w1.drain()
    c1.close()
    del w1

    response = api.get(
        f"/documents/{doc['id']}",
        headers=key(
            db_urls,
            "acme/approver",
        ),
    )

    document = response.json()

    assert (
        document["status"]
        == "awaiting_review"
    )

    review_response = api.post(
        f"/documents/{doc['id']}/review",
        json={
            "action": "approve",
            "expected_version":
                document["version"],
        },
        headers=key(
            db_urls,
            "acme/approver",
        ),
    )

    assert review_response.status_code == 202

    w2, c2 = new_worker("w2")

    w2.drain()
    c2.close()

    response = api.get(
        f"/documents/{doc['id']}",
        headers=key(
            db_urls,
            "acme/approver",
        ),
    )

    document = response.json()

    assert (
        document["status"]
        == "executed"
    ), document


def test_po_closed_after_queue_blocks_ap(
    api,
    worker,
    ap_http,
    db_urls,
    admin_conn,
):
    """
    If the PO closes after analysis but before
    AP dispatch, the external action is blocked.
    """

    doc = _one_clean(
        api,
        db_urls,
    )

    # First process analysis and queue the action.
    assert worker.process_one_job()

    row = admin_conn.execute(
        """
        SELECT payload
        FROM outbox
        WHERE document_id = %s
        """,
        (doc["id"],),
    ).fetchone()

    assert row is not None

    po_number = row["payload"]["po_number"]

    # Simulate a change in the source system.
    admin_conn.execute(
        """
        UPDATE purchase_orders
        SET status = 'closed',
            version = version + 1
        WHERE po_number = %s
        """,
        (po_number,),
    )

    # Dispatcher must revalidate before POST.
    assert (
        dispatch_once(worker.ap)
        == "dead"
    )

    response = api.get(
        f"/documents/{doc['id']}",
        headers=key(
            db_urls,
            "acme/approver",
        ),
    )

    document = response.json()

    assert document["status"] == "failed"

    assert (
        "po_closed"
        in document["error"]
    )

    # No bill should reach the mock AP.
    assert (
        ap_http.get(
            "/_state"
        ).json()["bills"]
        == []
    )

    assert (
        "execution_blocked_before_ap"
        in _events(
            api,
            db_urls,
            doc["id"],
        )
    )
