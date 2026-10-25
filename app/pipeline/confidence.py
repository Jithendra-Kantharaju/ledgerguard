"""Confidence from checkable signals, never from the model's own opinion.

Features come from things we can verify: do two extraction passes agree, is the
document complete, how sure was OCR, how well do vendor and lines match. Until
eval/calibrate.py has fitted a model on the dev set, a fixed weighted average is
used and the result is marked calibrated=False.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from app.schemas import CheckResult, Confidence, Invoice, ParsedDocument, Retrieval

FEATURES = ["agreement_rate", "completeness", "ocr_confidence", "vendor_similarity", "line_match_min",
            "n_review_flags", "is_scanned"]

_DEFAULT_WEIGHTS = {"agreement_rate": 0.4, "completeness": 0.2, "ocr_confidence": 0.2, "vendor_similarity": 0.1,
                    "line_match_min": 0.1}


def compute_features(invoice: Invoice, parsed: ParsedDocument, agreement: dict[str, bool], retrieval: Retrieval,
                     validation: CheckResult, match: CheckResult) -> dict[str, float]:
    present = [getattr(invoice, f) not in (None, "") for f in Invoice.KEY_FIELDS] + [bool(invoice.lines)]
    return {
        "agreement_rate": sum(agreement.values()) / len(agreement) if agreement else 0.0,
        "completeness": sum(present) / len(present),
        "ocr_confidence": 1.0 if not parsed.is_scanned else float(parsed.ocr_confidence or 0.0),
        "vendor_similarity": float(retrieval.vendor_similarity),
        "line_match_min": float(match.details.get("line_match_min", 0.0)),
        "n_review_flags": float(len(validation.review_flags) + len(match.review_flags)),
        "is_scanned": 1.0 if parsed.is_scanned else 0.0,
    }


class Calibration:
    """A logistic model stored as plain JSON (no pickles), produced by eval/calibrate.py."""

    def __init__(self, features: list[str], coef: list[float], intercept: float, threshold: float,
                 mean: list[float], scale: list[float]):
        self.features, self.coef, self.intercept = features, coef, intercept
        self.threshold, self.mean, self.scale = threshold, mean, scale

    @classmethod
    def load(cls, path: str | Path) -> Calibration | None:
        p = Path(path)
        if not p.exists():
            return None
        d = json.loads(p.read_text())
        return cls(d["features"], d["coef"], d["intercept"], d["threshold"], d["mean"], d["scale"])

    def predict(self, features: dict[str, float]) -> float:
        z = self.intercept
        for name, c, m, s in zip(self.features, self.coef, self.mean, self.scale, strict=True):
            z += c * (features[name] - m) / (s or 1.0)
        return 1 / (1 + math.exp(-z))


def score(features: dict[str, float], calibration: Calibration | None = None) -> Confidence:
    if calibration is not None:
        return Confidence(score=round(calibration.predict(features), 4), features=features, calibrated=True)
    s = sum(w * features[k] for k, w in _DEFAULT_WEIGHTS.items())
    return Confidence(score=round(s, 4), features=features, calibrated=False)
