"""Deterministic checks on the extracted invoice, before anything is matched.

A failed check is a hard failure (reject) only when the data it rests on is
trusted: a digital PDF where both extraction passes agree on those fields.
On scans or disagreeing passes the same failure becomes a review flag, because
the error may be in our reading of the document rather than in the invoice.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from decimal import Decimal

from app.config import CustomerConfig
from app.schemas import CheckResult, Invoice, ParsedDocument

REQUIRED = ("invoice_number", "vendor_name", "po_number", "total")

_INJECTION_RE = re.compile(
    r"(?i)(ignore (all |any )?(previous|prior|above) instructions|you are (an? )?(ai|assistant|language model)"
    r"|system (note|prompt|message)|pre-?approved|approve (it|this invoice) (immediately|now)"
    r"|disregard (the )?(rules|instructions)|as an ai)"
)


def trusted(parsed: ParsedDocument, agreement: dict[str, bool], fields: tuple[str, ...]) -> bool:
    return not parsed.is_scanned and all(agreement.get(f, False) for f in fields)


def validate(invoice: Invoice, parsed: ParsedDocument, agreement: dict[str, bool], cfg: CustomerConfig,
             today: date) -> CheckResult:
    result = CheckResult()

    def fail(code: str, fields: tuple[str, ...]) -> None:
        if trusted(parsed, agreement, fields):
            result.hard_failures.append(code)
        else:
            result.review_flags.append(f"{code} (unverified reading)")

    if parsed.is_scanned and not parsed.text.strip():
        result.review_flags.append("unreadable_document")
        return result

    if parsed.is_scanned and not cfg.allow_auto_approve_scanned:
        result.review_flags.append("scanned_document_policy_requires_review")

    if _INJECTION_RE.search(parsed.text):
        result.review_flags.append("suspicious_instructions_in_document")

    missing = [f for f in REQUIRED if getattr(invoice, f) in (None, "")]
    if missing:
        result.review_flags.append("missing_fields: " + ", ".join(missing))

    tol = cfg.total_tolerance_abs
    if invoice.lines:
        for ln in invoice.lines:
            if abs(ln.quantity * ln.unit_price - ln.amount) > tol:
                fail("line_amount_mismatch", ("lines",))
                break
        if invoice.subtotal is not None:
            line_sum = sum((ln.amount for ln in invoice.lines), Decimal("0"))
            if abs(line_sum - invoice.subtotal) > tol:
                fail("subtotal_mismatch", ("subtotal", "lines"))
    if None not in (invoice.subtotal, invoice.tax, invoice.total):
        if abs(invoice.subtotal + invoice.tax - invoice.total) > tol:
            fail("total_mismatch", ("subtotal", "tax", "total"))

    if invoice.currency and invoice.currency not in cfg.allowed_currencies:
        fail("currency_not_allowed", ("currency",))

    if invoice.invoice_date:
        if invoice.invoice_date > today + timedelta(days=7):
            result.review_flags.append("invoice_dated_in_future")
        elif invoice.invoice_date < today - timedelta(days=cfg.max_invoice_age_days):
            result.review_flags.append("invoice_too_old")
    else:
        result.review_flags.append("missing_fields: invoice_date")

    if invoice.total is not None and invoice.total <= 0:
        fail("non_positive_total", ("total",))
    return result
