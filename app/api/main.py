"""HTTP API: upload invoices, read results, submit reviews.

Every request is authenticated with an X-API-Key header that maps to one user in one
tenant. All tenant data is read inside a transaction with app.tenant_id set, so
Postgres row-level security keeps tenants apart even if a query forgets a filter.

Run: uvicorn app.api.main:app --port 8000
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel

from app.db.core import audit, pool, tx
from app.pipeline.parse import sha256
from app.settings import get_settings
from app.storage import get_storage
from app.tracing import configure_tracing
from app.worker import queue

MAX_UPLOAD_BYTES = 20 * 1024 * 1024



@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_tracing("ap-api")
    yield


app = FastAPI(title="AP Invoice Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins.split(","),
                   allow_methods=["GET", "POST"], allow_headers=["X-API-Key", "Content-Type"])


class User(BaseModel):
    id: str
    tenant_id: str
    email: str
    role: str
    approval_limit: str


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def current_user(x_api_key: str | None = Header(None)) -> User:
    if not x_api_key:
        raise HTTPException(401, "missing X-API-Key")
    with tx() as conn:
        key = conn.execute("SELECT user_id, tenant_id FROM api_keys WHERE key_hash = %s AND revoked_at IS NULL",
                           (hash_key(x_api_key),)).fetchone()
    if not key:
        raise HTTPException(401, "invalid API key")
    with tx(str(key["tenant_id"])) as conn:
        u = conn.execute("SELECT * FROM users WHERE id = %s AND active", (key["user_id"],)).fetchone()
    if not u:
        raise HTTPException(401, "inactive user")
    return User(id=str(u["id"]), tenant_id=str(u["tenant_id"]), email=u["email"], role=u["role"],
                approval_limit=str(u["approval_limit"]))


def _doc_summary(d: dict) -> dict[str, Any]:
    inv = d.get("invoice") or {}
    dec = d.get("decision") or {}
    return {"id": str(d["id"]), "filename": d["filename"], "status": d["status"], "version": d["version"],
            "invoice_number": inv.get("invoice_number"), "vendor_name": inv.get("vendor_name"),
            "total": inv.get("total"), "currency": inv.get("currency"), "outcome": dec.get("outcome"),
            "reasons": dec.get("reasons", []), "confidence": dec.get("confidence"),
            "created_at": d["created_at"].isoformat(), "updated_at": d["updated_at"].isoformat()}


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/readyz")
def readyz():
    with pool().connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/me")
def me(user: User = Depends(current_user)):
    return user


@app.post("/documents", status_code=202)
def upload(file: UploadFile = File(...), user: User = Depends(current_user)):
    if user.role not in ("uploader", "reviewer", "approver", "admin"):
        raise HTTPException(403, "not allowed to upload")
    data = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large")
    if not data.startswith(b"%PDF"):
        raise HTTPException(415, "only PDF files are accepted")
    digest = sha256(data)
    key = f"{user.tenant_id}/{digest}.pdf"
    get_storage().put(key, data)
    with tx(user.tenant_id) as conn:
        row = conn.execute(
            "INSERT INTO documents (tenant_id, sha256, filename, storage_key, status) "
            "VALUES (%s, %s, %s, %s, 'queued') ON CONFLICT (tenant_id, sha256) DO NOTHING RETURNING *",
            (user.tenant_id, digest, (file.filename or "upload.pdf")[:255], key)).fetchone()
        if row is None:  # same file uploaded before: return the existing document, do not reprocess
            row = conn.execute("SELECT * FROM documents WHERE sha256 = %s", (digest,)).fetchone()
            return {**_doc_summary(row), "duplicate_upload": True}
        queue.enqueue(conn, user.tenant_id, str(row["id"]), "analyze")
        audit(conn, user.tenant_id, str(row["id"]), user.id, "uploaded", {"filename": row["filename"],
                                                                          "sha256": digest})
    return {**_doc_summary(row), "duplicate_upload": False}


@app.get("/documents")
def list_documents(status: str | None = Query(None), limit: int = Query(50, le=200),
                   user: User = Depends(current_user)):
    with tx(user.tenant_id) as conn:
        if status:
            rows = conn.execute("SELECT * FROM documents WHERE status = %s ORDER BY created_at DESC LIMIT %s",
                                (status, limit)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM documents ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    return [_doc_summary(r) for r in rows]


@app.get("/documents/{document_id}")
def get_document(document_id: str, user: User = Depends(current_user)):
    with tx(user.tenant_id) as conn:
        d = conn.execute("SELECT * FROM documents WHERE id = %s", (document_id,)).fetchone()
        if not d:
            raise HTTPException(404, "not found")
        reviews = conn.execute("SELECT r.*, u.email FROM reviews r JOIN users u ON u.id = r.user_id "
                               "WHERE r.document_id = %s ORDER BY r.created_at", (document_id,)).fetchall()
        events = conn.execute("SELECT actor, event, data, created_at FROM audit_log WHERE document_id = %s "
                              "ORDER BY id", (document_id,)).fetchall()
        outbox = conn.execute("SELECT idempotency_key, status, attempts, ap_reference, last_error FROM outbox "
                              "WHERE document_id = %s ORDER BY id", (document_id,)).fetchall()
    analysis = d["analysis"] or {}
    if analysis.get("parsed"):
        analysis = {**analysis, "parsed": {**analysis["parsed"], "text": None}}  # big; fetch the file instead
    return {**_doc_summary(d), "invoice": d["invoice"], "decision": d["decision"], "analysis": analysis,
            "error": d["error"],
            "reviews": [{"id": str(r["id"]), "user": r["email"], "action": r["action"],
                         "document_version": r["document_version"], "comment": r["comment"],
                         "outcome": r["outcome"], "created_at": r["created_at"].isoformat()} for r in reviews],
            "audit": [{**e, "created_at": e["created_at"].isoformat()} for e in events],
            "outbox": outbox}


@app.get("/documents/{document_id}/file")
def get_file(document_id: str, user: User = Depends(current_user)):
    with tx(user.tenant_id) as conn:
        d = conn.execute("SELECT storage_key, filename FROM documents WHERE id = %s", (document_id,)).fetchone()
    if not d:
        raise HTTPException(404, "not found")
    return Response(get_storage().get(d["storage_key"]), media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{document_id}.pdf"'})


class ReviewIn(BaseModel):
    action: Literal["approve", "reject", "correct"]
    expected_version: int
    fields: dict[str, Any] | None = None
    comment: str | None = None


@app.post("/documents/{document_id}/review", status_code=202)
def review(document_id: str, body: ReviewIn, user: User = Depends(current_user)):
    """Record the decision and hand it to the worker, which revalidates before anything executes."""
    if user.role not in ("reviewer", "approver", "admin"):
        raise HTTPException(403, "not allowed to review")
    if body.action == "approve" and user.role not in ("approver", "admin"):
        raise HTTPException(403, "reviewers can correct or reject; approving needs the approver role")
    if body.action == "correct" and not body.fields:
        raise HTTPException(422, "correct needs fields")
    with tx(user.tenant_id) as conn:
        d = conn.execute("SELECT * FROM documents WHERE id = %s FOR UPDATE", (document_id,)).fetchone()
        if not d:
            raise HTTPException(404, "not found")
        if d["status"] != "awaiting_review":
            raise HTTPException(409, f"document is {d['status']}, not awaiting review")
        if d["version"] != body.expected_version:
            raise HTTPException(409, f"document changed: you reviewed version {body.expected_version}, "
                                     f"current is {d['version']}; reload and review again")
        r = conn.execute(
            "INSERT INTO reviews (tenant_id, document_id, user_id, action, document_version, comment) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (user.tenant_id, document_id, user.id, body.action, body.expected_version, body.comment)).fetchone()
        payload = {"review_id": str(r["id"]), "user_id": user.id, "action": body.action,
                   "expected_version": body.expected_version, "fields": body.fields, "comment": body.comment}
        queue.enqueue(conn, user.tenant_id, document_id, "resume", payload)
        audit(conn, user.tenant_id, document_id, user.id, "review_submitted",
              {"action": body.action, "version": body.expected_version, "fields": body.fields})
    return {"review_id": str(r["id"]), "status": "queued"}


@app.get("/stats")
def stats(user: User = Depends(current_user)):
    with tx(user.tenant_id) as conn:
        rows = conn.execute("SELECT status, count(*) AS n FROM documents GROUP BY status").fetchall()
        dead = conn.execute("SELECT count(*) AS n FROM outbox o JOIN documents d ON d.id = o.document_id "
                            "WHERE o.status = 'dead'").fetchone()
    return {"documents_by_status": {r["status"]: r["n"] for r in rows}, "dead_letters": dead["n"]}

