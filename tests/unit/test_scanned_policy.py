
"""Ensure scanned invoices cannot bypass human review."""

from datetime import date

from app.config import load_config
from app.pipeline.validate import validate
from app.schemas import Invoice, ParsedDocument


def test_scanned_invoice_requires_human_review():
    cfg = load_config("app/config/customer_a.yaml")

    parsed = ParsedDocument(
        text="Invoice Number: TEST-001\nCurrency: USD",
        is_scanned=True,
        method="tesseract",
        ocr_confidence=1.0,
    )

    result = validate(
        invoice=Invoice(),
        parsed=parsed,
        agreement={},
        cfg=cfg,
        today=date(2026, 10, 27),
    )

    assert "scanned_document_policy_requires_review" in (
        result.review_flags
    )
