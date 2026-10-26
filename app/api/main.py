
"""LedgerGuard FastAPI application.

Features:
- Local API-key and AWS Cognito authentication
- Tenant-scoped database access through PostgreSQL RLS
- PDF invoice upload and retrieval
- Document review and approval
- Audited matching exceptions
- Invoice processing status and audit history
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Header,
    HTTPException,
    Query,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.db.core import audit, pool, tx
from app.pipeline.parse import sha256
from app.settings import get_settings
from app.storage import get_storage
from app.tracing import configure_tracing
from app.worker import queue


MAX_UPLOAD_BYTES = 20 * 1024 * 1024


# --------------------------------------------------
# Application lifecycle
# --------------------------------------------------

@asynccontextmanager
async def lifespan(_: FastAPI):
    configure_tracing("ap-api")
    yield


app = FastAPI(
    title="LedgerGuard AP Invoice Agent",
    description=(
        "Enterprise AI-powered accounts payable "
        "automation with human approval and "
        "auditable execution."
    ),
    version="0.2.0",
    lifespan=lifespan,
)


# --------------------------------------------------
# CORS
# --------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in get_settings().cors_origins.split(",")
        if origin.strip()
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=[
        "Authorization",
        "X-API-Key",
        "Content-Type",
    ],
)


# --------------------------------------------------
# User model
# --------------------------------------------------

class User(BaseModel):
    id: str
    tenant_id: str
    email: str
    role: str
    approval_limit: str


def hash_key(key: str) -> str:
    return hashlib.sha256(
        key.encode("utf-8")
    ).hexdigest()


# --------------------------------------------------
# Authentication
# --------------------------------------------------

def current_user(
    x_api_key: str | None = Header(default=None),
    authorization: str | None = Header(default=None),
) -> User:
    """
    Local development:
        AUTH_MODE=api_key
        X-API-Key: <local-key>

    AWS deployment:
        AUTH_MODE=cognito
        Authorization: Bearer <access-token>

    API keys are never accepted in Cognito mode.
    """

    settings = get_settings()

    # AWS Cognito authentication
    if settings.auth_mode == "cognito":
        from app.api.cognito_auth import get_cognito_user

        identity = get_cognito_user(
            authorization
        )

        return User(**identity)

    # Local development authentication
    if settings.auth_mode != "api_key":
        raise HTTPException(
            status_code=503,
            detail="Unsupported authentication configuration",
        )

    if not x_api_key:
        raise HTTPException(
            status_code=401,
            detail="Missing X-API-Key",
        )

    with tx() as conn:
        key = conn.execute(
            """
            SELECT user_id, tenant_id
            FROM api_keys
            WHERE key_hash = %s
              AND revoked_at IS NULL
            """,
            (hash_key(x_api_key),),
        ).fetchone()

    if not key:
        raise HTTPException(
            status_code=401,
            detail="Invalid API key",
        )

    tenant_id = str(key["tenant_id"])

    with tx(tenant_id) as conn:
        user = conn.execute(
            """
            SELECT *
            FROM users
            WHERE id = %s
              AND active = true
            """,
            (key["user_id"],),
        ).fetchone()

    if not user:
        raise HTTPException(
            status_code=401,
            detail="Inactive user",
        )

    return User(
        id=str(user["id"]),
        tenant_id=str(user["tenant_id"]),
        email=user["email"],
        role=user["role"],
        approval_limit=str(user["approval_limit"]),
    )


# --------------------------------------------------
# Document summary
# --------------------------------------------------

def _doc_summary(
    document: dict,
) -> dict[str, Any]:
    invoice = document.get("invoice") or {}
    decision = document.get("decision") or {}

    return {
        "id": str(document["id"]),
        "filename": document["filename"],
        "status": document["status"],
        "version": document["version"],
        "invoice_number": invoice.get(
            "invoice_number"
        ),
        "vendor_name": invoice.get(
            "vendor_name"
        ),
        "total": invoice.get("total"),
        "currency": invoice.get("currency"),
        "outcome": decision.get("outcome"),
        "reasons": decision.get("reasons", []),
        "confidence": decision.get("confidence"),
        "created_at": document[
            "created_at"
        ].isoformat(),
        "updated_at": document[
            "updated_at"
        ].isoformat(),
    }


# --------------------------------------------------
# Health checks
# --------------------------------------------------

@app.get("/healthz")
def healthz():
    return {
        "ok": True
    }


@app.get("/readyz")
def readyz():
    try:
        with pool().connection() as conn:
            conn.execute("SELECT 1")
    except Exception:
        raise HTTPException(
            status_code=503,
            detail="Database unavailable",
        )

    return {
        "ok": True
    }


# --------------------------------------------------
# Authenticated user information
# --------------------------------------------------

@app.get("/me", response_model=User)
def me(
    user: User = Depends(current_user),
):
    return user


# --------------------------------------------------
# Invoice upload
# --------------------------------------------------

@app.post(
    "/documents",
    status_code=202,
)
def upload(
    file: UploadFile = File(...),
    user: User = Depends(current_user),
):
    if user.role not in (
        "uploader",
        "reviewer",
        "approver",
        "admin",
    ):
        raise HTTPException(
            status_code=403,
            detail="Not allowed to upload",
        )

    data = file.file.read(
        MAX_UPLOAD_BYTES + 1
    )

    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail="File too large",
        )

    if not data.startswith(b"%PDF"):
        raise HTTPException(
            status_code=415,
            detail="Only PDF files are accepted",
        )

    digest = sha256(data)

    # Tenant-specific content-addressed storage.
    storage_key = (
        f"{user.tenant_id}/{digest}.pdf"
    )

    # Local mode uses shared storage.
    # AWS mode uses S3 through the configured backend.
    get_storage().put(
        storage_key,
        data,
    )

    with tx(user.tenant_id) as conn:
        row = conn.execute(
            """
            INSERT INTO documents (
                tenant_id,
                sha256,
                filename,
                storage_key,
                status
            )
            VALUES (
                %s, %s, %s, %s, 'queued'
            )
            ON CONFLICT (tenant_id, sha256)
            DO NOTHING
            RETURNING *
            """,
            (
                user.tenant_id,
                digest,
                (file.filename or "upload.pdf")[:255],
                storage_key,
            ),
        ).fetchone()

        # Do not reprocess an identical upload.
        if row is None:
            existing = conn.execute(
                """
                SELECT *
                FROM documents
                WHERE sha256 = %s
                """,
                (digest,),
            ).fetchone()

            if existing is None:
                raise HTTPException(
                    status_code=409,
                    detail="Duplicate lookup failed",
                )

            return {
                **_doc_summary(existing),
                "duplicate_upload": True,
            }

        queue.enqueue(
            conn,
            user.tenant_id,
            str(row["id"]),
            "analyze",
        )

        audit(
            conn,
            user.tenant_id,
            str(row["id"]),
            user.id,
            "uploaded",
            {
                "filename": row["filename"],
                "sha256": digest,
            },
        )

    return {
        **_doc_summary(row),
        "duplicate_upload": False,
    }


# --------------------------------------------------
# List documents
# --------------------------------------------------

@app.get("/documents")
def list_documents(
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    user: User = Depends(current_user),
):
    with tx(user.tenant_id) as conn:
        if status:
            rows = conn.execute(
                """
                SELECT *
                FROM documents
                WHERE status = %s
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT *
                FROM documents
                ORDER BY created_at DESC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()

    return [
        _doc_summary(row)
        for row in rows
    ]


# --------------------------------------------------
# Document details
# --------------------------------------------------

@app.get("/documents/{document_id}")
def get_document(
    document_id: str,
    user: User = Depends(current_user),
):
    with tx(user.tenant_id) as conn:
        document = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = %s
            """,
            (document_id,),
        ).fetchone()

        if not document:
            raise HTTPException(
                status_code=404,
                detail="Document not found",
            )

        reviews = conn.execute(
            """
            SELECT
                r.*,
                u.email
            FROM reviews r
            JOIN users u
              ON u.id = r.user_id
            WHERE r.document_id = %s
            ORDER BY r.created_at
            """,
            (document_id,),
        ).fetchall()

        events = conn.execute(
            """
            SELECT
                actor,
                event,
                data,
                created_at
            FROM audit_log
            WHERE document_id = %s
            ORDER BY id
            """,
            (document_id,),
        ).fetchall()

        outbox = conn.execute(
            """
            SELECT
                idempotency_key,
                status,
                attempts,
                ap_reference,
                last_error
            FROM outbox
            WHERE document_id = %s
            ORDER BY id
            """,
            (document_id,),
        ).fetchall()

    analysis = document["analysis"] or {}

    # Avoid returning the entire parsed PDF text.
    if analysis.get("parsed"):
        analysis = {
            **analysis,
            "parsed": {
                **analysis["parsed"],
                "text": None,
            },
        }

    return {
        **_doc_summary(document),
        "invoice": document["invoice"],
        "decision": document["decision"],
        "analysis": analysis,
        "error": document["error"],
        "reviews": [
            {
                "id": str(review["id"]),
                "user": review["email"],
                "action": review["action"],
                "document_version": review[
                    "document_version"
                ],
                "comment": review["comment"],
                "outcome": review["outcome"],
                "created_at": review[
                    "created_at"
                ].isoformat(),
            }
            for review in reviews
        ],
        "audit": [
            {
                **event,
                "created_at": event[
                    "created_at"
                ].isoformat(),
            }
            for event in events
        ],
        "outbox": outbox,
    }


# --------------------------------------------------
# Download or preview invoice
# --------------------------------------------------

@app.get("/documents/{document_id}/file")
def get_file(
    document_id: str,
    user: User = Depends(current_user),
):
    with tx(user.tenant_id) as conn:
        document = conn.execute(
            """
            SELECT storage_key
            FROM documents
            WHERE id = %s
            """,
            (document_id,),
        ).fetchone()

    if not document:
        raise HTTPException(
            status_code=404,
            detail="Document not found",
        )

    data = get_storage().get(
        document["storage_key"]
    )

    return Response(
        content=data,
        media_type="application/pdf",
        headers={
            "Content-Disposition":
                f'inline; filename="{document_id}.pdf"',
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


# --------------------------------------------------
# Human review schema
# --------------------------------------------------

class ReviewIn(BaseModel):
    action: Literal[
        "approve",
        "reject",
        "correct",
    ]

    expected_version: int = Field(ge=1)

    fields: dict[str, Any] | None = None
    comment: str | None = None

    # Explicit human approval of matching exceptions.
    exception_codes: list[str] | None = None
    exception_reason: str | None = None


# --------------------------------------------------
# Submit human review
# --------------------------------------------------

@app.post(
    "/documents/{document_id}/review",
    status_code=202,
)
def review(
    document_id: str,
    body: ReviewIn,
    user: User = Depends(current_user),
):
    """
    Store human review and enqueue the worker.

    The worker revalidates all business evidence
    before allowing external AP execution.
    """

    if user.role not in (
        "reviewer",
        "approver",
        "admin",
    ):
        raise HTTPException(
            status_code=403,
            detail="Not allowed to review",
        )

    if (
        body.action == "approve"
        and user.role not in ("approver", "admin")
    ):
        raise HTTPException(
            status_code=403,
            detail="Approver role required",
        )

    if (
        body.action == "correct"
        and not body.fields
    ):
        raise HTTPException(
            status_code=422,
            detail="Corrections require fields",
        )

    if (
        body.exception_codes
        and body.action != "approve"
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Matching exceptions can only "
                "be submitted with approval"
            ),
        )

    if (
        body.exception_codes
        and len(
            (body.exception_reason or "").strip()
        ) < 12
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "Matching exceptions require "
                "a reason of at least 12 characters"
            ),
        )

    if body.exception_codes and (
        len(body.exception_codes)
        != len(set(body.exception_codes))
    ):
        raise HTTPException(
            status_code=422,
            detail="Duplicate matching exception codes",
        )

    with tx(user.tenant_id) as conn:
        document = conn.execute(
            """
            SELECT *
            FROM documents
            WHERE id = %s
            FOR UPDATE
            """,
            (document_id,),
        ).fetchone()

        if not document:
            raise HTTPException(
                status_code=404,
                detail="Document not found",
            )

        if document["status"] != "awaiting_review":
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Document status is {document['status']}; "
                    "not awaiting review"
                ),
            )

        if document["version"] != body.expected_version:
            raise HTTPException(
                status_code=409,
                detail=(
                    "Document changed. "
                    f"Reviewed version: {body.expected_version}. "
                    f"Current version: {document['version']}. "
                    "Reload before reviewing."
                ),
            )

        review_row = conn.execute(
            """
            INSERT INTO reviews (
                tenant_id,
                document_id,
                user_id,
                action,
                document_version,
                comment
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                user.tenant_id,
                document_id,
                user.id,
                body.action,
                body.expected_version,
                body.comment,
            ),
        ).fetchone()

        payload = {
            "review_id": str(review_row["id"]),
            "user_id": user.id,
            "action": body.action,
            "expected_version": body.expected_version,
            "fields": body.fields,
            "comment": body.comment,
            "exception_codes": body.exception_codes or [],
            "exception_reason": body.exception_reason,
        }

        queue.enqueue(
            conn,
            user.tenant_id,
            document_id,
            "resume",
            payload,
        )

        audit(
            conn,
            user.tenant_id,
            document_id,
            user.id,
            "review_submitted",
            {
                "action": body.action,
                "version": body.expected_version,
                "fields": body.fields,
                "exception_codes":
                    body.exception_codes or [],
                "exception_reason": body.exception_reason,
            },
        )

    return {
        "review_id": str(review_row["id"]),
        "status": "queued",
    }


# --------------------------------------------------
# Tenant statistics
# --------------------------------------------------

@app.get("/stats")
def stats(
    user: User = Depends(current_user),
):
    with tx(user.tenant_id) as conn:
        rows = conn.execute(
            """
            SELECT
                status,
                count(*) AS n
            FROM documents
            GROUP BY status
            """
        ).fetchall()

        dead = conn.execute(
            """
            SELECT count(*) AS n
            FROM outbox o
            JOIN documents d
              ON d.id = o.document_id
            WHERE o.status = 'dead'
            """
        ).fetchone()

    return {
        "documents_by_status": {
            row["status"]: row["n"]
            for row in rows
        },
        "dead_letters": dead["n"],
    }
