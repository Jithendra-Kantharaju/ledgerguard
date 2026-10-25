"""Three-way match: invoice vs purchase order vs goods receipt, with tolerances."""

from __future__ import annotations

import difflib
from collections import defaultdict
from decimal import Decimal

from app.config import CustomerConfig
from app.pipeline.normalize import norm_text
from app.pipeline.retrieve import name_similarity
from app.pipeline.validate import trusted
from app.schemas import CheckResult, Invoice, ParsedDocument, Retrieval

LINE_MATCH_MIN = 0.6


def _desc_similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, norm_text(a), norm_text(b)).ratio()


def three_way_match(invoice: Invoice, r: Retrieval, cfg: CustomerConfig, parsed: ParsedDocument,
                    agreement: dict[str, bool]) -> CheckResult:
    result = CheckResult()

    def fail(code: str, fields: tuple[str, ...]) -> None:
        if trusted(parsed, agreement, fields):
            result.hard_failures.append(code)
        else:
            result.review_flags.append(f"{code} (unverified reading)")

    if r.duplicate_of:
        fail("duplicate_invoice", ("invoice_number", "vendor_name"))

    if r.po is None:
        if invoice.po_number:
            fail("po_not_found", ("po_number",))
        else:
            result.review_flags.append("no_po_number")
        result.details["line_match_min"] = 0.0
        return result

    po = r.po
    if po.status != "open":
        result.hard_failures.append("po_closed")  # read from our own system: always trusted

    if r.po_vendor:
        sim = name_similarity(invoice.vendor_name, r.po_vendor)
        result.details["po_vendor_similarity"] = round(sim, 3)
        if sim < cfg.vendor_match_min:
            fail("vendor_mismatch", ("vendor_name", "po_number"))

    if invoice.currency and invoice.currency != po.currency:
        fail("currency_mismatch", ("currency",))

    received: dict[str, Decimal] = defaultdict(Decimal)
    for rec in r.receipts:
        for rl in rec.lines:
            received[norm_text(rl.description)] += rl.quantity
    if not r.receipts:
        result.review_flags.append("no_goods_receipt")

    available = list(po.lines)
    line_sims = []
    max_price_factor = Decimal(1) + cfg.price_tolerance_pct / 100
    for ln in invoice.lines:
        if not available:
            fail("line_not_on_po", ("lines",))
            break
        sims = [_desc_similarity(ln.description, pl.description) for pl in available]
        best = max(range(len(available)), key=lambda i: sims[i])
        if sims[best] < LINE_MATCH_MIN:
            fail("line_not_on_po", ("lines",))
            line_sims.append(sims[best])
            continue
        pl = available.pop(best)
        line_sims.append(sims[best])
        if ln.unit_price > pl.unit_price * max_price_factor:
            fail("price_over_tolerance", ("lines",))
        if r.receipts and ln.quantity > received[norm_text(pl.description)] + cfg.quantity_tolerance:
            fail("qty_over_received", ("lines",))
        if ln.quantity > pl.quantity + cfg.quantity_tolerance:
            fail("qty_over_ordered", ("lines",))

    result.details["line_match_min"] = round(min(line_sims), 3) if line_sims else 0.0
    if not invoice.lines:
        result.review_flags.append("no_line_items")
    # Deduplicate while keeping order.
    result.hard_failures = list(dict.fromkeys(result.hard_failures))
    result.review_flags = list(dict.fromkeys(result.review_flags))
    return result
