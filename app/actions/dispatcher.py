
"""Transactional outbox with fail-closed AP verification."""

from __future__ import annotations

import psycopg

from app.actions.ap_client import (
    APClient,
    APRejected,
    APUnknownOutcome,
    make_ap_client,
)
from app.actions.guard import (
    preflight,
    verify_readback,
)
from app.db.core import audit, tx
from app.settings import get_settings
from app.tracing import span

BACKOFF_CAP_SECONDS = 600


def _set_tenant(
    conn: psycopg.Connection,
    tenant_id: str,
) -> None:
    conn.execute(
        "SELECT set_config('app.tenant_id', %s, true)",
        (str(tenant_id),),
    )


def _finish(
    conn: psycopg.Connection,
    row: dict,
    status: str,
    doc_status: str,
    event: str,
    data: dict,
    ap_ref: str | None = None,
    error: str | None = None,
) -> None:

    conn.execute(
        """
        UPDATE outbox
        SET status = %s,
            ap_reference = coalesce(%s, ap_reference),
            last_error = %s,
            attempts = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (
            status,
            ap_ref,
            error,
            row["attempts"] + 1,
            row["id"],
        ),
    )

    conn.execute(
        """
        UPDATE documents
        SET status = %s,
            error = %s,
            updated_at = now()
        WHERE id = %s
        """,
        (
            doc_status,
            error,
            row["document_id"],
        ),
    )

    audit(
        conn,
        row["tenant_id"],
        row["document_id"],
        "dispatcher",
        event,
        data,
    )


def dispatch_once(
    client: APClient | None = None,
) -> str | None:

    settings = get_settings()
    client = client or make_ap_client()

    with tx() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM outbox
            WHERE status = 'pending'
              AND next_attempt_at <= now()
            ORDER BY id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
            """
        ).fetchone()

        if row is None:
            return None

        _set_tenant(
            conn,
            row["tenant_id"],
        )

        key = row["idempotency_key"]
        payload = row["payload"]

        with span(
            "dispatch",
            outbox_id=row["id"],
            attempt=row["attempts"] + 1,
        ):

            doc = conn.execute(
                """
                SELECT *
                FROM documents
                WHERE id = %s
                """,
                (row["document_id"],),
            ).fetchone()

            if not doc:
                _finish(
                    conn,
                    row,
                    "dead",
                    "failed",
                    "execution_blocked_before_ap",
                    {"reason": "document not visible"},
                    error="document not visible",
                )
                return "dead"

            # Recheck immediately before the external call.
            _, blockers = preflight(
                conn,
                doc,
                payload,
            )

            if blockers:
                reason = "; ".join(blockers)

                _finish(
                    conn,
                    row,
                    "dead",
                    "failed",
                    "execution_blocked_before_ap",
                    {"problems": blockers},
                    error=reason,
                )

                return "dead"

            bill = None

            try:
                bill = client.create_bill(
                    key,
                    payload,
                )

            except APRejected as exc:
                _finish(
                    conn,
                    row,
                    "dead",
                    "failed",
                    "execution_rejected_by_ap",
                    {"error": str(exc)},
                    error=str(exc),
                )
                return "dead"

            except APUnknownOutcome as exc:
                try:
                    bill = client.find_by_key(
                        key,
                        payload,
                    )
                except (
                    APUnknownOutcome,
                    APRejected,
                ):
                    bill = None

                if bill is None:
                    attempts = row["attempts"] + 1

                    if (
                        attempts
                        >= settings.outbox_max_attempts
                    ):
                        _finish(
                            conn,
                            row,
                            "dead",
                            "failed",
                            "execution_dead_lettered",
                            {
                                "error": str(exc),
                                "attempts": attempts,
                            },
                            error=str(exc),
                        )
                        return "dead"

                    delay = min(
                        2 ** attempts,
                        BACKOFF_CAP_SECONDS,
                    )

                    conn.execute(
                        """
                        UPDATE outbox
                        SET attempts = %s,
                            last_error = %s,
                            next_attempt_at =
                                now() + make_interval(
                                    secs => %s
                                ),
                            updated_at = now()
                        WHERE id = %s
                        """,
                        (
                            attempts,
                            str(exc),
                            delay,
                            row["id"],
                        ),
                    )

                    audit(
                        conn,
                        row["tenant_id"],
                        row["document_id"],
                        "dispatcher",
                        "execution_retry_scheduled",
                        {
                            "error": str(exc),
                            "attempt": attempts,
                            "retry_in_s": delay,
                        },
                    )

                    return "retry"

                audit(
                    conn,
                    row["tenant_id"],
                    row["document_id"],
                    "dispatcher",
                    "execution_recovered_by_status_query",
                    {"bill_id": bill["id"]},
                )

            # Read the bill back from the external system.
            try:
                stored = client.get_bill(
                    bill["id"]
                )

            except (
                APUnknownOutcome,
                APRejected,
            ) as exc:

                err = f"read-back failed: {exc}"

                if (
                    row["attempts"] + 1
                    >= settings.outbox_max_attempts
                ):
                    _finish(
                        conn,
                        row,
                        "dead",
                        "failed",
                        "execution_dead_lettered",
                        {"error": err},
                        ap_ref=bill["id"],
                        error=err,
                    )

                    return "dead"

                conn.execute(
                    """
                    UPDATE outbox
                    SET attempts = attempts + 1,
                        last_error = %s,
                        ap_reference = %s,
                        next_attempt_at =
                            now() + interval '5 seconds',
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        err,
                        bill["id"],
                        row["id"],
                    ),
                )

                audit(
                    conn,
                    row["tenant_id"],
                    row["document_id"],
                    "dispatcher",
                    "execution_readback_retry",
                    {
                        "error": err,
                        "bill_id": bill["id"],
                    },
                )

                return "retry"

            mismatches = verify_readback(
                payload,
                stored,
            )

            if mismatches:
                reason = (
                    "read-back mismatch: "
                    + ", ".join(mismatches)
                )

                _finish(
                    conn,
                    row,
                    "dead",
                    "failed",
                    "execution_verification_failed",
                    {
                        "bill_id": bill["id"],
                        "fields": mismatches,
                    },
                    ap_ref=bill["id"],
                    error=reason,
                )

                return "dead"

            _finish(
                conn,
                row,
                "confirmed",
                "executed",
                "executed",
                {
                    "bill_id": bill["id"],
                    "verified_fields": [
                        "status",
                        "vendor_name",
                        "invoice_number",
                        "po_number",
                        "currency",
                        "amount",
                    ],
                },
                ap_ref=bill["id"],
            )

            return "confirmed"
