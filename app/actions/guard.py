
"""Fail-closed approval guard using current tenant-scoped ERP data."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation

from app.db.repo import SqlRepository
from app.pipeline.run import assess, from_human
from app.schemas import Invoice, ParsedDocument
from app.tenants import config_for


def reassess(conn, doc):
    if not doc.get("analysis") or not doc.get("invoice"):
        raise ValueError("document has no saved analysis/invoice")

    parsed = ParsedDocument(**doc["analysis"]["parsed"])
    inv = Invoice(**doc["invoice"])
    cfg = config_for(conn, str(doc["tenant_id"]))

    return assess(
        parsed,
        from_human(parsed, inv),
        SqlRepository(conn),
        cfg,
        date.today(),
        calibration=None,
        document_id=str(doc["id"]),
    )


def hard_blockers(result) -> list[str]:
    inv = result.extraction.invoice
    retrieval = result.retrieval

    errors = (
        list(result.validation.hard_failures)
        + list(result.match.hard_failures)
    )

    required = (
        "invoice_number",
        "vendor_name",
        "po_number",
        "invoice_date",
        "currency",
        "total",
    )

    for name in required:
        if getattr(inv, name) in (None, ""):
            errors.append(f"missing_required_{name}")

    if not inv.lines:
        errors.append("missing_invoice_lines")

    if retrieval.po is None:
        errors.append("purchase_order_required")

    if retrieval.po_vendor is None:
        errors.append("verified_po_vendor_required")

    return list(dict.fromkeys(errors))


def preflight(conn, doc, payload: dict | None = None):
    """Reassess current evidence immediately before execution."""

    if doc["status"] not in ("approved", "executing"):
        return None, [
            f"invalid_execution_status:{doc['status']}"
        ]

    try:
        result = reassess(conn, doc)
    except (ValueError, KeyError, TypeError) as exc:
        return None, [
            f"reassessment_failed:{type(exc).__name__}"
        ]

    problems = hard_blockers(result)

    meta = (doc["analysis"] or {}).get("approval_review")
    flags = set(result.match.review_flags)

    if meta:
        if int(meta.get("version", -1)) != doc["version"]:
            problems.append("approval_version_changed")

        review = conn.execute(
            """
            SELECT *
            FROM reviews
            WHERE id = %s AND document_id = %s
            """,
            (meta.get("review_id"), doc["id"]),
        ).fetchone()

        user = conn.execute(
            "SELECT * FROM users WHERE id = %s",
            (meta.get("user_id"),),
        ).fetchone()

        if (
            not review
            or review["outcome"] != "accepted"
            or str(review["user_id"])
            != str(meta.get("user_id"))
        ):
            problems.append("approval_not_audited")

        if (
            not user
            or not user["active"]
            or user["role"] not in ("approver", "admin")
        ):
            problems.append("approver_no_longer_authorized")

        elif (
            result.extraction.invoice.total is None
            or Decimal(user["approval_limit"])
            < result.extraction.invoice.total
        ):
            problems.append("approver_limit_changed")

        granted = set(meta.get("exception_codes") or [])

        if granted != flags:
            problems.append("matching_exceptions_changed")

        if (
            granted
            and len(
                str(
                    meta.get("exception_reason") or ""
                ).strip()
            ) < 12
        ):
            problems.append("missing_exception_reason")

    elif flags:
        problems.append(
            "matching_exception_approval_required"
        )

    parsed = ParsedDocument(**doc["analysis"]["parsed"])

    if parsed.is_scanned and not meta:
        problems.append(
            "scanned_invoice_requires_manual_approval"
        )

    if payload is not None:
        inv = result.extraction.invoice
        vendor = result.retrieval.po_vendor

        expected = {
            "vendor_name": vendor.name if vendor else None,
            "invoice_number": inv.invoice_number,
            "po_number": inv.po_number,
            "currency": inv.currency,
        }

        for field, value in expected.items():
            if payload.get(field) != value:
                problems.append(
                    f"outbox_{field}_changed"
                )

        try:
            if (
                inv.total is None
                or Decimal(str(payload.get("amount")))
                != inv.total
            ):
                problems.append(
                    "outbox_amount_changed"
                )
        except (InvalidOperation, TypeError):
            problems.append(
                "outbox_amount_invalid"
            )

        cfg = config_for(
            conn,
            str(doc["tenant_id"]),
        )

        if (
            payload.get("tenant_slug")
            != cfg.tenant_slug
        ):
            problems.append(
                "outbox_tenant_changed"
            )

    return result, list(dict.fromkeys(problems))


def verify_readback(
    payload: dict,
    stored: dict,
) -> list[str]:
    """Verify all required fields, including missing values."""

    mismatches = []

    fields = (
        "status",
        "vendor_name",
        "invoice_number",
        "po_number",
        "currency",
    )

    for name in fields:
        if (
            not isinstance(stored, dict)
            or stored.get(name) != payload.get(name)
        ):
            mismatches.append(name)

    try:
        if (
            Decimal(str(stored.get("amount")))
            != Decimal(str(payload.get("amount")))
        ):
            mismatches.append("amount")
    except (
        InvalidOperation,
        AttributeError,
        TypeError,
    ):
        mismatches.append("amount")

    return mismatches
