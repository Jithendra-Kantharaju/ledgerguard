"""Run the analysis for one document: parse -> extract x2 -> validate -> retrieve -> match -> score -> route.

These functions have no side effects. The worker calls them inside graph nodes and
the eval harness calls them directly, so both measure exactly the same logic.
Extraction (slow, may call an LLM) is separate from assessment (fast, needs the
database), so the worker does not hold a database transaction open during LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.config import CustomerConfig
from app.pipeline.confidence import Calibration, compute_features, score
from app.pipeline.extract import Extractor, field_agreement
from app.pipeline.match import three_way_match
from app.pipeline.parse import parse_document
from app.pipeline.retrieve import Repository, retrieve
from app.pipeline.router import route
from app.pipeline.validate import validate
from app.schemas import AnalysisResult, ExtractionResult, Invoice, ParsedDocument
from app.tracing import set_attr, span

ALL_AGREED = {f: True for f in (*Invoice.KEY_FIELDS, "lines")}


@dataclass
class Extracted:
    primary: ExtractionResult
    secondary: ExtractionResult | None
    agreement: dict[str, bool]


def extract_invoice(parsed: ParsedDocument, primary: Extractor, secondary: Extractor | None) -> Extracted:
    with span("extract", extractor=getattr(primary, "name", "?")):
        first = primary.extract(parsed)
        set_attr("tokens.input", first.input_tokens)
        set_attr("tokens.output", first.output_tokens)
    if secondary is None:
        return Extracted(first, None, {f: False for f in ALL_AGREED})
    with span("extract.secondary", extractor=getattr(secondary, "name", "?")):
        second = secondary.extract(parsed)
    return Extracted(first, second, field_agreement(first.invoice, second.invoice))


def from_human(parsed: ParsedDocument, invoice: Invoice) -> Extracted:
    """Fields a reviewer confirmed or corrected count as agreed: a person read them."""
    return Extracted(ExtractionResult(invoice=invoice, model="human"), None, dict(ALL_AGREED))


def assess(parsed: ParsedDocument, ex: Extracted, repo: Repository, cfg: CustomerConfig, today: date,
           calibration: Calibration | None = None, document_id: str | None = None,
           threshold: float | None = None) -> AnalysisResult:
    inv = ex.primary.invoice
    # A human-confirmed reading is trusted even on a scan, so rule failures on it are real failures.
    checked = parsed.model_copy(update={"is_scanned": False}) if ex.primary.model == "human" else parsed
    with span("validate"):
        validation = validate(inv, checked, ex.agreement, cfg, today)
    with span("retrieve"):
        retrieval = retrieve(inv, repo, document_id)
    with span("match"):
        match = three_way_match(inv, retrieval, cfg, checked, ex.agreement)
    conf = score(compute_features(inv, parsed, ex.agreement, retrieval, validation, match), calibration)
    if threshold is None and calibration is not None:
        # The stricter of the customer's risk appetite and the threshold the dev-set sweep supports.
        threshold = max(cfg.confidence_threshold, calibration.threshold)
    with span("route"):
        decision = route(validation, match, conf, inv, cfg, threshold)
        set_attr("decision.outcome", decision.outcome.value)
        set_attr("decision.gate", decision.gate)
    return AnalysisResult(parsed=parsed, extraction=ex.primary, secondary=ex.secondary,
                          field_agreement=ex.agreement, validation=validation, retrieval=retrieval, match=match,
                          confidence=conf, decision=decision)


def analyze(data: bytes, repo: Repository, cfg: CustomerConfig, primary: Extractor, secondary: Extractor | None,
            today: date, calibration: Calibration | None = None, document_id: str | None = None,
            threshold: float | None = None) -> AnalysisResult:
    with span("parse"):
        parsed = parse_document(data)
        set_attr("parse.method", parsed.method)
    ex = extract_invoice(parsed, primary, secondary)
    return assess(parsed, ex, repo, cfg, today, calibration, document_id, threshold)
