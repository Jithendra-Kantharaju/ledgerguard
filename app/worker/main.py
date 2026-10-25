"""Worker process: claims jobs, runs the graph, dispatches the outbox, sweeps expired leases.

Run: python -m app.worker.main            (loop forever)
     python -m app.worker.main --once     (drain queue and outbox, then exit; used in tests and demos)
"""

from __future__ import annotations

import argparse
import logging
import time

import psycopg
from langgraph.types import Command
from psycopg.rows import dict_row

from app.actions.ap_client import APClient
from app.actions.dispatcher import dispatch_once
from app.db.core import audit, tx
from app.pipeline.confidence import Calibration
from app.pipeline.extract import get_extractor
from app.settings import get_settings
from app.storage import get_storage
from app.tracing import configure_tracing, span
from app.worker import queue
from app.worker.graph import Deps, build_graph

log = logging.getLogger("worker")


def default_deps() -> Deps:
    s = get_settings()
    return Deps(primary=get_extractor(s.extractor), secondary=get_extractor(s.secondary_extractor),
                storage=get_storage(), calibration=Calibration.load(s.calibration_file), actor=f"worker:{s.worker_id}",
                shadow_mode=s.shadow_mode)


def postgres_checkpointer():
    from langgraph.checkpoint.postgres import PostgresSaver

    conn = psycopg.connect(get_settings().database_url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
    return PostgresSaver(conn)


class Worker:
    def __init__(self, deps: Deps | None = None, checkpointer=None, ap_client: APClient | None = None,
                 worker_id: str | None = None):
        s = get_settings()
        self.deps = deps or default_deps()
        self.graph = build_graph(self.deps, checkpointer or postgres_checkpointer())
        self.ap = ap_client or APClient()
        self.worker_id = worker_id or s.worker_id
        self.lease = s.job_lease_seconds

    def run_job(self, job: dict) -> None:
        doc_id, tenant = str(job["document_id"]), str(job["tenant_id"])
        config = {"configurable": {"thread_id": doc_id}}
        with span("job", job_id=job["id"], kind=job["kind"], document_id=doc_id):
            if job["kind"] == "analyze":
                self.graph.invoke({"document_id": doc_id, "tenant_id": tenant}, config)
            elif job["kind"] == "resume":
                snapshot = self.graph.get_state(config)
                if "human_review" not in (snapshot.next or ()):
                    with tx(tenant) as conn:
                        if job["payload"].get("review_id"):
                            conn.execute("UPDATE reviews SET outcome = %s WHERE id = %s",
                                         ("ignored: document was not waiting for review", job["payload"]["review_id"]))
                    return
                self.graph.invoke(Command(resume=job["payload"]), config)

    def process_one_job(self) -> bool:
        with tx() as conn:
            job = queue.claim(conn, self.worker_id, self.lease)
        if job is None:
            return False
        try:
            self.run_job(job)
        except Exception as e:  # noqa: BLE001 - every failure is recorded on the job
            log.exception("job %s failed", job["id"])
            with tx() as conn:
                status = queue.fail(conn, job, f"{type(e).__name__}: {e}")
            if status == "failed":
                with tx(str(job["tenant_id"])) as conn:
                    conn.execute("UPDATE documents SET status = 'failed', error = %s, updated_at = now() "
                                 "WHERE id = %s", (f"{type(e).__name__}: {e}"[:2000], job["document_id"]))
                    audit(conn, str(job["tenant_id"]), str(job["document_id"]), self.deps.actor, "job_failed",
                          {"job_id": job["id"], "error": str(e)[:500]})
            return True
        with tx() as conn:
            queue.complete(conn, job["id"])
        return True

    def drain(self, max_iterations: int = 10_000) -> None:
        """Process every ready job and outbox row, then return."""
        for _ in range(max_iterations):
            did_job = self.process_one_job()
            did_dispatch = dispatch_once(self.ap) is not None
            if not did_job and not did_dispatch:
                return

    def run_forever(self) -> None:
        s = get_settings()
        last_sweep = 0.0
        log.info("worker %s started", self.worker_id)
        while True:
            if time.monotonic() - last_sweep > 30:
                with tx() as conn:
                    n = queue.sweep(conn)
                if n:
                    log.warning("requeued %d jobs with expired leases", n)
                last_sweep = time.monotonic()
            busy = self.process_one_job()
            try:
                busy = (dispatch_once(self.ap) is not None) or busy
            except Exception:  # noqa: BLE001
                log.exception("dispatcher error")
            if not busy:
                time.sleep(s.poll_interval_seconds)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--once", action="store_true", help="drain the queue and outbox, then exit")
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    configure_tracing("ap-worker")
    w = Worker()
    if a.once:
        w.drain()
    else:
        w.run_forever()


if __name__ == "__main__":
    main()
