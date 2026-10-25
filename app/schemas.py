"""Domain models shared by the pipeline, worker, API and eval harness.

Money is always Decimal. Every extracted field may be None, so an extractor can
say "not on the document" instead of guessing.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar

from pydantic import BaseModel, Field, field_validator


class LineItem(BaseModel):
    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


class Invoice(BaseModel):
    invoice_number: str | None = None
    vendor_name: str | None = None
    po_number: str | None = None
    invoice_date: date | None = None
    currency: str | None = Field(default=None, description="ISO 4217 code")
    subtotal: Decimal | None = None
    tax: Decimal | None = None
    total: Decimal | None = None
    lines: list[LineItem] = Field(default_factory=list)
    notes: str | None = None

    KEY_FIELDS: ClassVar[tuple[str, ...]] = (
        "invoice_number", "vendor_name", "po_number", "invoice_date", "currency", "subtotal", "tax", "total",
    )

    @field_validator("invoice_number", "po_number")
    @classmethod
    def _compact_identifier(cls, v: str | None) -> str | None:
        """Identifiers never contain spaces; OCR often inserts them ("INV- 1700")."""
        if v is None:
            return None
        v = "".join(v.split()).lstrip("#")
        return v or None


class ParsedDocument(BaseModel):
    text: str
    is_scanned: bool
    ocr_confidence: float | None = None  # None for digital PDFs
    method: str  # "pymupdf", "docling", "tesseract", "none"


class ExtractionResult(BaseModel):
    invoice: Invoice
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0


class Vendor(BaseModel):
    id: str
    name: str
    aliases: list[str] = Field(default_factory=list)


class POLine(BaseModel):
    description: str
    quantity: Decimal
    unit_price: Decimal


class PurchaseOrder(BaseModel):
    id: str
    po_number: str
    vendor_id: str
    currency: str
    status: str  # "open" | "closed"
    lines: list[POLine]
    version: int = 1


class ReceiptLine(BaseModel):
    description: str
    quantity: Decimal


class Receipt(BaseModel):
    id: str
    po_id: str
    lines: list[ReceiptLine]


class Retrieval(BaseModel):
    vendor: Vendor | None = None
    vendor_similarity: float = 0.0
    po: PurchaseOrder | None = None
    po_vendor: Vendor | None = None
    receipts: list[Receipt] = Field(default_factory=list)
    duplicate_of: str | None = None  # document id of an already-processed invoice


class CheckResult(BaseModel):
    """Output of validation or matching.

    hard_failures: business-rule violations on trusted data -> reject.
    review_flags: problems a human should look at -> review.
    """

    hard_failures: list[str] = Field(default_factory=list)
    review_flags: list[str] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class Confidence(BaseModel):
    score: float
    features: dict[str, float]
    calibrated: bool


class Outcome(str, Enum):
    approve = "approve"
    review = "review"
    reject = "reject"


class Decision(BaseModel):
    outcome: Outcome
    gate: str
    reasons: list[str]
    confidence: float


class AnalysisResult(BaseModel):
    """Everything the pipeline produced for one document, up to the decision."""

    parsed: ParsedDocument
    extraction: ExtractionResult
    secondary: ExtractionResult | None = None
    field_agreement: dict[str, bool] = Field(default_factory=dict)
    validation: CheckResult
    retrieval: Retrieval
    match: CheckResult
    confidence: Confidence
    decision: Decision
