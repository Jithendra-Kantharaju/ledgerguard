import json

from app.pipeline.confidence import Calibration, score
from app.tracing import redact


def test_redaction():
    text = "Bank account: 123456789012, routing number 021000021, IBAN DE89 3704 0044 0532 0130 00, EIN 12-3456789"
    out = redact(text)
    for secret in ("123456789012", "021000021", "3704 0044", "12-3456789"):
        assert secret not in out
    assert redact({"total": "54.00"}) == '{"total": "54.00"}'


def test_uncalibrated_score_is_marked(tmp_path):
    f = {"agreement_rate": 1, "completeness": 1, "ocr_confidence": 1, "vendor_similarity": 1, "line_match_min": 1,
         "n_review_flags": 0, "is_scanned": 0}
    assert score(f).calibrated is False and score(f).score == 1.0
    path = tmp_path / "cal.json"
    path.write_text(json.dumps({"features": list(f), "coef": [2.0] + [0.0] * 6, "intercept": 0.0,
                                "threshold": 0.7, "mean": [0.5] + [0.0] * 6, "scale": [0.5] + [1.0] * 6}))
    cal = Calibration.load(path)
    c = score(f, cal)
    assert c.calibrated and 0.85 < c.score < 0.9
    assert Calibration.load(tmp_path / "missing.json") is None
