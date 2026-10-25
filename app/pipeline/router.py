"""Risk router: ordered gates. Business rules always win over confidence.

Gate 1  any hard rule failure                      -> reject
Gate 2  amount above limit, or any review flag     -> review
Gate 3  calibrated confidence below threshold      -> review
        otherwise                                  -> approve
"""

from __future__ import annotations

from app.config import CustomerConfig
from app.schemas import CheckResult, Confidence, Decision, Invoice, Outcome


def route(validation: CheckResult, match: CheckResult, conf: Confidence, invoice: Invoice, cfg: CustomerConfig,
          threshold: float | None = None) -> Decision:
    threshold = cfg.confidence_threshold if threshold is None else threshold

    failures = validation.hard_failures + match.hard_failures
    if failures:
        return Decision(outcome=Outcome.reject, gate="gate1_hard_rule", reasons=failures, confidence=conf.score)

    reasons = []
    if invoice.total is not None and invoice.total > cfg.auto_approve_limit:
        reasons.append(f"amount {invoice.total} above auto-approve limit {cfg.auto_approve_limit}")
    reasons += validation.review_flags + match.review_flags
    if reasons:
        return Decision(outcome=Outcome.review, gate="gate2_amount_or_flags", reasons=reasons, confidence=conf.score)

    if conf.score < threshold:
        return Decision(outcome=Outcome.review, gate="gate3_confidence",
                        reasons=[f"confidence {conf.score:.2f} below threshold {threshold:.2f}"],
                        confidence=conf.score)

    return Decision(outcome=Outcome.approve, gate="passed", reasons=["all gates passed"], confidence=conf.score)
