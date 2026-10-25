"""Postgres job queue: FOR UPDATE SKIP LOCKED claims, leases, retries and a sweeper.

A worker that crashes mid-job leaves a running job with an expired lease; the
sweeper puts it back in the queue (or fails it after max_attempts). Graph nodes
are written to be safe to re-run, so a retried job does not double-act.
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb


def enqueue(conn: psycopg.Connection, tenant_id: str, document_id: str, kind: str, payload: dict | None = None,
            max_attempts: int = 3) -> int:
    row = conn.execute(
        "INSERT INTO jobs (tenant_id, document_id, kind, payload, max_attempts) VALUES (%s, %s, %s, %s, %s) "
        "RETURNING id", (tenant_id, document_id, kind, Jsonb(payload or {}), max_attempts)).fetchone()
    return row["id"]


def claim(conn: psycopg.Connection, worker_id: str, lease_seconds: int) -> dict | None:
    """Claim the oldest ready job. Jobs for a document already being processed are skipped,
    so two jobs for the same document never run at the same time."""
    return conn.execute(
        """
        UPDATE jobs SET status = 'running', locked_by = %s, attempts = attempts + 1,
                        lease_until = now() + make_interval(secs => %s), updated_at = now()
        WHERE id = (
            SELECT j.id FROM jobs j
            WHERE j.status = 'queued' AND j.run_after <= now()
              AND NOT EXISTS (SELECT 1 FROM jobs r WHERE r.document_id = j.document_id AND r.status = 'running')
            ORDER BY j.id
            FOR UPDATE SKIP LOCKED
            LIMIT 1
        )
        RETURNING *
        """, (worker_id, lease_seconds)).fetchone()


def complete(conn: psycopg.Connection, job_id: int) -> None:
    conn.execute("UPDATE jobs SET status = 'done', lease_until = NULL, updated_at = now() WHERE id = %s", (job_id,))


def fail(conn: psycopg.Connection, job: dict, error: str) -> str:
    """Retry with exponential backoff, or mark failed. Returns the new status."""
    if job["attempts"] >= job["max_attempts"]:
        conn.execute("UPDATE jobs SET status = 'failed', last_error = %s, lease_until = NULL, updated_at = now() "
                     "WHERE id = %s", (error[:2000], job["id"]))
        return "failed"
    delay = 2 ** job["attempts"]
    conn.execute(
        "UPDATE jobs SET status = 'queued', last_error = %s, lease_until = NULL, locked_by = NULL, "
        "run_after = now() + make_interval(secs => %s), updated_at = now() WHERE id = %s",
        (error[:2000], delay, job["id"]))
    return "queued"


def sweep(conn: psycopg.Connection) -> int:
    """Return jobs whose lease expired (worker died) to the queue, or fail them if out of attempts."""
    conn.execute(
        "UPDATE jobs SET status = 'failed', last_error = 'lease expired; out of attempts', lease_until = NULL, "
        "updated_at = now() WHERE status = 'running' AND lease_until < now() AND attempts >= max_attempts")
    cur = conn.execute(
        "UPDATE jobs SET status = 'queued', locked_by = NULL, lease_until = NULL, "
        "last_error = 'lease expired; requeued', updated_at = now() "
        "WHERE status = 'running' AND lease_until < now()")
    return cur.rowcount
