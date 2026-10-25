"""The document workflow as a LangGraph state machine with a durable human-review pause.

    analyze ──approve──────────────────────────────► execute ─► END
       │ ──reject───────────────────────────────────► reject ─► END
       └──review──► human_review ◄───────────┐
                     │ interrupt() (worker   │
                     │ is released; state    │
                     │ lives in Postgres)    │
                     ├─approve─► revalidate ─┤ (fails: back to review, version bumped)
                     │                └──────────► execute
                     ├─correct─► apply_correction ─┘ (never auto-executes)
                     └─reject──► reject

thread_id = document id. Every node is safe to re-run (a retried job or a resumed
interrupt re-executes the current node from the top).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Callable, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt
from psycopg.types.json import Jsonb

from app.db.core import audit, tx
from app.db.repo import SqlRepository
from app.pipeline.confidence import Calibration
from app.pipeline.extract import Extractor
from app.pipeline.parse import parse_document
from app.pipeline.run import assess, extract_invoice, from_human
from app.schemas import Invoice, Outcome, ParsedDocument
from app.storage import Storage
from app.tenants import config_for
from app.tracing import span

APPROVE_ROLES = {"approver", "admin"}
REVIEW_ROLES = {"reviewer", "approver", "admin"}
STATUS_FOR = {Outcome.approve: "approved", Outcome.review: "awaiting_review", Outcome.reject: "rejected"}


class State(TypedDict, total=False):
    document_id: str
    tenant_id: str
    outcome: str
    review: dict
    review_error: str | None
    next: str


@dataclass
class Deps:
    primary: Extractor
    secondary: Extractor | None
    storage: Storage
    calibration: Calibration | None = None
    today: Callable[[], date] = field(default=date.today)
    actor: str = "worker"
    shadow_mode: bool = False


def _doc(conn, document_id: str) -> dict:
    row = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
    if row is None:
        raise LookupError(f"document {document_id} not visible for this tenant")
    return row


def _save_analysis(conn, doc_id: str, result, status: str, bump: bool, extra: dict | None = None) -> None:
    analysis = result.model_dump(mode="json")
    analysis.update(extra or {})
    conn.execute(
        "UPDATE documents SET analysis = %s, invoice = %s, decision = %s, status = %s, error = NULL, "
        "version = version + %s, updated_at = now() WHERE id = %s",
        (Jsonb(analysis), Jsonb(result.extraction.invoice.model_dump(mode="json")),
         Jsonb(result.decision.model_dump(mode="json")), status, 1 if bump else 0, doc_id))


def _review_outcome(conn, review: dict, outcome: str) -> None:
    if review.get("review_id"):
        conn.execute("UPDATE reviews SET outcome = %s WHERE id = %s", (outcome[:500], review["review_id"]))


def build_graph(deps: Deps, checkpointer):
    def analyze(state: State) -> State:
        doc_id, tenant = state["document_id"], state["tenant_id"]
        with tx(tenant) as conn:
            doc = _doc(conn, doc_id)
            conn.execute("UPDATE documents SET status = 'processing', updated_at = now() WHERE id = %s", (doc_id,))
        with span("analyze", document_id=doc_id):
            parsed = parse_document(deps.storage.get(doc["storage_key"]))
            ex = extract_invoice(parsed, deps.primary, deps.secondary)  # no transaction held during LLM calls
            with tx(tenant) as conn:
                cfg = config_for(conn, tenant)
                result = assess(parsed, ex, SqlRepository(conn), cfg, deps.today(), deps.calibration, doc_id)
                d = result.decision
                _save_analysis(conn, doc_id, result, STATUS_FOR[d.outcome], bump=False)
                audit(conn, tenant, doc_id, deps.actor, "analyzed", {
                    "outcome": d.outcome.value, "gate": d.gate, "reasons": d.reasons, "confidence": d.confidence,
                    "calibrated": result.confidence.calibrated, "model": ex.primary.model,
                    "secondary_model": ex.secondary.model if ex.secondary else None,
                    "input_tokens": ex.primary.input_tokens + (ex.secondary.input_tokens if ex.secondary else 0),
                    "output_tokens": ex.primary.output_tokens + (ex.secondary.output_tokens if ex.secondary else 0),
                    "parse_method": parsed.method})
        return {"outcome": d.outcome.value}

    def human_review(state: State) -> State:
        doc_id, tenant = state["document_id"], state["tenant_id"]
        with tx(tenant) as conn:
            doc = _doc(conn, doc_id)
            if doc["status"] != "awaiting_review":
                conn.execute("UPDATE documents SET status = 'awaiting_review', updated_at = now() WHERE id = %s",
                             (doc_id,))
        review = interrupt({"document_id": doc_id, "version": doc["version"]})
        # Resumed: check who is acting and that they saw the current version.
        with tx(tenant) as conn:
            doc = _doc(conn, doc_id)
            user = conn.execute("SELECT * FROM users WHERE id = %s", (review.get("user_id"),)).fetchone()
            error = None
            if user is None or not user["active"]:
                error = "unknown or inactive user"
            elif user["role"] not in REVIEW_ROLES:
                error = f"role {user['role']} cannot review"
            elif review.get("expected_version") != doc["version"]:
                error = f"stale review: reviewed version {review.get('expected_version')}, current {doc['version']}"
            elif review.get("action") not in ("approve", "reject", "correct"):
                error = "unknown action"
            if error:
                _review_outcome(conn, review, f"refused: {error}")
                audit(conn, tenant, doc_id, str(review.get("user_id")), "review_refused", {"error": error})
        return {"review": review, "review_error": error}

    def after_review(state: State) -> str:
        if state.get("review_error"):
            return "human_review"
        return {"approve": "revalidate", "correct": "apply_correction", "reject": "reject"}[state["review"]["action"]]

    def apply_correction(state: State) -> State:
        doc_id, tenant, review = state["document_id"], state["tenant_id"], state["review"]
        with tx(tenant) as conn:
            doc = _doc(conn, doc_id)
            if doc["version"] != review["expected_version"]:  # already applied (node re-run)
                return {}
            try:
                merged = Invoice(**{**(doc["invoice"] or {}), **(review.get("fields") or {})})
            except Exception as e:  # invalid values: refuse, stay in review
                _review_outcome(conn, review, f"refused: invalid fields: {e}")
                return {}
            conn.execute("INSERT INTO corrections (tenant_id, document_id, user_id, fields) VALUES (%s, %s, %s, %s)",
                         (tenant, doc_id, review["user_id"], Jsonb(review.get("fields") or {})))
            parsed = ParsedDocument(**doc["analysis"]["parsed"])
            cfg = config_for(conn, tenant)
            result = assess(parsed, from_human(parsed, merged), SqlRepository(conn), cfg, deps.today(),
                            deps.calibration, doc_id)
            machine = doc["analysis"].get("machine_extraction") or doc["analysis"]["extraction"]
            _save_analysis(conn, doc_id, result, "awaiting_review", bump=True,
                           extra={"machine_extraction": machine})
            _review_outcome(conn, review, "applied; checks rerun; awaiting review")
            audit(conn, tenant, doc_id, str(review["user_id"]), "corrected",
                  {"fields": review.get("fields"), "new_decision": result.decision.model_dump(mode="json")})
        return {}

    def revalidate(state: State) -> State:
        """Nothing that was true when the reviewer looked is assumed still true."""
        doc_id, tenant, review = state["document_id"], state["tenant_id"], state["review"]
        with tx(tenant) as conn:
            doc = _doc(conn, doc_id)
            if doc["status"] in ("executing", "executed"):  # node re-run after execute committed
                return {"next": "execute"}
            user = conn.execute("SELECT * FROM users WHERE id = %s", (review["user_id"],)).fetchone()
            inv = Invoice(**doc["invoice"])
            problems = []
            if user["role"] not in APPROVE_ROLES:
                problems.append(f"role {user['role']} cannot approve")
            elif inv.total is None or inv.total > Decimal(user["approval_limit"]):
                problems.append(f"total {inv.total} exceeds approver limit {user['approval_limit']}")
            if problems:  # not the document's fault: no version bump
                _review_outcome(conn, review, "refused: " + "; ".join(problems))
                audit(conn, tenant, doc_id, str(review["user_id"]), "approval_refused", {"problems": problems})
                return {"next": "human_review"}
            parsed = ParsedDocument(**doc["analysis"]["parsed"])
            cfg = config_for(conn, tenant)
            result = assess(parsed, from_human(parsed, inv), SqlRepository(conn), cfg, deps.today(),
                            deps.calibration, doc_id)
            failures = result.validation.hard_failures + result.match.hard_failures
            if result.retrieval.po is None:
                failures.append("purchase order required to execute")
            if failures:
                machine = doc["analysis"].get("machine_extraction") or doc["analysis"]["extraction"]
                _save_analysis(conn, doc_id, result, "awaiting_review", bump=True,
                               extra={"machine_extraction": machine})
                _review_outcome(conn, review, "refused: revalidation failed: " + ", ".join(failures))
                audit(conn, tenant, doc_id, deps.actor, "revalidation_failed", {"failures": failures})
                return {"next": "human_review"}
            conn.execute("UPDATE documents SET status = 'approved', updated_at = now() WHERE id = %s", (doc_id,))
            _review_outcome(conn, review, "accepted")
            audit(conn, tenant, doc_id, str(review["user_id"]), "approved_by_reviewer",
                  {"version": doc["version"], "comment": review.get("comment")})
        return {"next": "execute"}

    def execute(state: State) -> State:
        """Record the invoice as processed and write the outbox row, in one transaction."""
        doc_id, tenant = state["document_id"], state["tenant_id"]
        with tx(tenant) as conn:
            doc = _doc(conn, doc_id)
            if doc["status"] in ("executing", "executed", "shadow_approved"):
                return {}
            cfg = config_for(conn, tenant)
            inv = Invoice(**doc["invoice"])
            retrieval = doc["analysis"]["retrieval"]
            vendor = retrieval.get("po_vendor") or retrieval.get("vendor")
            if deps.shadow_mode:
                conn.execute("UPDATE documents SET status = 'shadow_approved', updated_at = now() WHERE id = %s",
                             (doc_id,))
                audit(conn, tenant, doc_id, deps.actor, "shadow_would_execute",
                      {"vendor": vendor["name"], "invoice_number": inv.invoice_number, "amount": str(inv.total)})
                return {}
            inserted = conn.execute(
                "INSERT INTO processed_invoices (tenant_id, vendor_id, invoice_number, document_id) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING RETURNING document_id",
                (tenant, vendor["id"], inv.invoice_number, doc_id)).fetchone()
            if inserted is None:
                existing = conn.execute(
                    "SELECT document_id FROM processed_invoices WHERE vendor_id = %s AND invoice_number = %s",
                    (vendor["id"], inv.invoice_number)).fetchone()
                if existing is None or str(existing["document_id"]) != doc_id:
                    conn.execute("UPDATE documents SET status = 'rejected', error = %s, updated_at = now() "
                                 "WHERE id = %s", ("duplicate_invoice detected at execution", doc_id))
                    audit(conn, tenant, doc_id, deps.actor, "rejected", {"reasons": ["duplicate_invoice"]})
                    return {}
            key = f"approve:{doc_id}:v{doc['version']}"
            payload = {"vendor_name": vendor["name"], "invoice_number": inv.invoice_number,
                       "po_number": inv.po_number, "currency": inv.currency, "amount": str(inv.total),
                       "status": cfg.ap.approved_status}
            conn.execute("INSERT INTO outbox (tenant_id, document_id, idempotency_key, action, payload) "
                         "VALUES (%s, %s, %s, 'create_approved_bill', %s) ON CONFLICT (idempotency_key) DO NOTHING",
                         (tenant, doc_id, key, Jsonb(payload)))
            conn.execute("UPDATE documents SET status = 'executing', updated_at = now() WHERE id = %s", (doc_id,))
            audit(conn, tenant, doc_id, deps.actor, "execution_queued", {"idempotency_key": key})
        return {}

    def reject(state: State) -> State:
        doc_id, tenant, review = state["document_id"], state["tenant_id"], state.get("review")
        with tx(tenant) as conn:
            conn.execute("UPDATE documents SET status = 'rejected', updated_at = now() WHERE id = %s", (doc_id,))
            if review and review.get("action") == "reject":
                _review_outcome(conn, review, "accepted")
                audit(conn, tenant, doc_id, str(review["user_id"]), "rejected_by_reviewer",
                      {"comment": review.get("comment")})
            else:
                audit(conn, tenant, doc_id, deps.actor, "rejected", {})
        return {}

    g = StateGraph(State)
    for name, fn in [("analyze", analyze), ("human_review", human_review), ("apply_correction", apply_correction),
                     ("revalidate", revalidate), ("execute", execute), ("reject", reject)]:
        g.add_node(name, fn)
    g.add_edge(START, "analyze")
    g.add_conditional_edges("analyze", lambda s: {"approve": "execute", "review": "human_review",
                                                  "reject": "reject"}[s["outcome"]],
                            ["execute", "human_review", "reject"])
    g.add_conditional_edges("human_review", after_review,
                            ["human_review", "revalidate", "apply_correction", "reject"])
    g.add_edge("apply_correction", "human_review")
    g.add_conditional_edges("revalidate", lambda s: s["next"], ["execute", "human_review"])
    g.add_edge("execute", END)
    g.add_edge("reject", END)
    return g.compile(checkpointer=checkpointer)
