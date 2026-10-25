"""Per-customer configuration: tolerances, approval limits and AP field mapping.

Onboarding a new customer should only need a new YAML file, not code changes.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import yaml
from pydantic import BaseModel


class APMapping(BaseModel):
    """How this customer's AP system names things."""

    approved_status: str = "approved"


class CustomerConfig(BaseModel):
    name: str
    tenant_slug: str
    allowed_currencies: list[str]
    price_tolerance_pct: Decimal  # unit price may exceed PO price by this %
    total_tolerance_abs: Decimal  # arithmetic slack for totals checks
    quantity_tolerance: Decimal = Decimal("0")  # billed may exceed received by this many units
    auto_approve_limit: Decimal  # gate 2: totals above this always go to review
    confidence_threshold: float  # gate 3: below this goes to review
    vendor_match_min: float = 0.85  # minimum similarity to accept a vendor name match
    max_invoice_age_days: int = 365
    allow_auto_approve_scanned: bool = False  # if False, every OCR'd invoice goes to a person
    ap: APMapping = APMapping()


def load_config(path: str | Path) -> CustomerConfig:
    with open(path) as f:
        return CustomerConfig(**yaml.safe_load(f))
