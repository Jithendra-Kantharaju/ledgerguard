"""Outbox dispatcher: delivers approved invoices to the AP system exactly once, then verifies.

For each pending outbox row (locked with FOR UPDATE SKIP LOCKED for the whole attempt):
  1. POST the bill with the row's idempotency key.
  2. If the outcome is unknown (timeout / 5xx), ask the AP system whether that key landed.
     Found -> carry on as success. Not found -> retry later with backoff.
  3. Read the bill back and check status and amount. Only then mark the document executed.
  4. After max attempts, or on a 4xx or a failed read-back, dead-letter the row and flag the
     document for a person. Nothing is ever silently dropped.

If this process dies after the AP call but before commit, the row is still pending; the
retry sends the same key and the AP system returns the bill it already made.
"""

from __future__ import annotations

from decimal import Decimal

import psycopg

from app.actions.ap_client import APClient, APRejected, APUnknownOutcome
from app.db.core import audit, tx
from app.settings import get_settings
from app.tracing import span

BACKOFF_CAP_SECONDS = 600


def _set_tenant(conn: psycopg.Connection, tenant_id: str) -> None:
    conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))


def _finish(conn: psycopg.Connection, row: dict, status: str, doc_status: str, event: str, data: dict,
            ap_ref: str | None = None, error: str | None = None) -> None:
    conn.execute("UPDATE outbox SET status = %s, ap_reference = coalesce(%s, ap_reference), last_error = %s, "
                 "attempts = %s, updated_at = now() WHERE id = %s",
                 (status, ap_ref, error, row["attempts"] + 1, row["id"]))
    conn.execute("UPDATE documents SET status = %s, error = %s, updated_at = now() WHERE id = %s",
                 (doc_status, error, row["document_id"]))
    audit(conn, row["tenant_id"], row["document_id"], "dispatcher", event, data)


def dispatch_once(client: APClient | None = None) -> str | None:
    """Process one pending outbox row. Returns what happened, or None if nothing was ready."""
    settings = get_settings()
    client = client or APClient()
    with tx() as conn:
        row = conn.execute(
            "SELECT * FROM outbox WHERE status = 'pending' AND next_attempt_at <= now() "
            "ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 1").fetchone()
        if row is None:
            return None
        _set_tenant(conn, row["tenant_id"])
        key, payload = row["idempotency_key"], row["payload"]
        with span("dispatch", outbox_id=row["id"], attempt=row["attempts"] + 1):
            bill = None
            try:
                bill = client.create_bill(key, payload)
            except APRejected as e:
                _finish(conn, row, "dead", "failed", "execution_rejected_by_ap", {"error": str(e)}, error=str(e))
                return "dead"
            except APUnknownOutcome as e:
                try:
                    bill = client.find_by_key(key)
                except (APUnknownOutcome, APRejected):
                    bill = None
                if bill is None:
                    attempts = row["attempts"] + 1
                    if attempts >= settings.outbox_max_attempts:
                        _finish(conn, row, "dead", "failed", "execution_dead_lettered",
                                {"error": str(e), "attempts": attempts}, error=str(e))
                        return "dead"
                    delay = min(2 ** attempts, BACKOFF_CAP_SECONDS)
                    conn.execute(
                        "UPDATE outbox SET attempts = %s, last_error = %s, "
                        "next_attempt_at = now() + make_interval(secs => %s), updated_at = now() WHERE id = %s",
                        (attempts, str(e), delay, row["id"]))
                    audit(conn, row["tenant_id"], row["document_id"], "dispatcher", "execution_retry_scheduled",
                          {"error": str(e), "attempt": attempts, "retry_in_s": delay})
                    return "retry"
                audit(conn, row["tenant_id"], row["document_id"], "dispatcher", "execution_recovered_by_status_query",
                      {"bill_id": bill["id"]})

            # Read-back: trust what the AP system says it has, not what we sent.
            try:
                stored = client.get_bill(bill["id"])
            except (APUnknownOutcome, APRejected) as e:
                if row["attempts"] + 1 >= settings.outbox_max_attempts:
                    _finish(conn, row, "dead", "failed", "execution_dead_lettered",
                            {"error": f"read-back failed: {e}"}, ap_ref=bill["id"], error=f"read-back failed: {e}")
                    return "dead"
                conn.execute("UPDATE outbox SET attempts = attempts + 1, last_error = %s, ap_reference = %s, "
                             "next_attempt_at = now() + interval '5 seconds', updated_at = now() WHERE id = %s",
                             (f"read-back failed: {e}", bill["id"], row["id"]))
                return "retry"
            ok = (stored.get("status") == payload["status"]
                  and Decimal(str(stored.get("amount"))) == Decimal(str(payload["amount"])))
            if not ok:
                err = (f"read-back mismatch: status={stored.get('status')!r} amount={stored.get('amount')!r}, "
                       f"expected status={payload['status']!r} amount={payload['amount']!r}")
                _finish(conn, row, "dead", "failed", "execution_verification_failed",
                        {"bill_id": bill["id"], "error": err}, ap_ref=bill["id"], error=err)
                return "dead"
            _finish(conn, row, "confirmed", "executed", "executed",
                    {"bill_id": bill["id"], "status": stored["status"], "amount": str(stored["amount"])},
                    ap_ref=bill["id"])
            return "confirmed"
