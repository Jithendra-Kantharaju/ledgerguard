"""Fit the confidence model on the dev split and pick the gate-3 threshold.

Label: 1 if every key field was extracted correctly, else 0. Confidence answers one
question only, "did we read this document right?". Whether the invoice is payable is
the rules' job (gates 1 and 2), so business errors are not negatives here.
The model is fitted on all dev cases; the threshold is chosen on the cases that reach
gate 3 (no hard failure, no review flag, under the amount limit), because gates 1 and 2
already decided the rest.

The threshold is the lowest value at which the share of wrongly-approved cases among
those reaching gate 3 is at most --max-error-rate (default 0), plus a safety margin.

Usage: python -m eval.calibrate --split data/generated/dev
Writes eval/results/calibration.json and eval/results/threshold_sweep.png.
Never fit on the test split.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from app.config import load_config
from app.pipeline.confidence import FEATURES
from app.pipeline.extract import get_extractor
from eval.harness import run_split


def label(r: dict) -> int:
    return int(all(r["field_correct"].values()))


def sweep(scores: np.ndarray, y: np.ndarray) -> list[dict]:
    rows = []
    for t in np.round(np.linspace(0, 1, 101), 2):
        approved = scores >= t
        n = int(approved.sum())
        wrong = int((approved & (y == 0)).sum())
        rows.append({"threshold": float(t), "approved": n, "wrong": wrong,
                     "automation": round(n / len(y), 4) if len(y) else 0.0,
                     "error_rate": round(wrong / n, 4) if n else 0.0})
    return rows


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--split", default="data/generated/dev")
    p.add_argument("--config", default="app/config/customer_a.yaml")
    p.add_argument("--extractor", default="rules")
    p.add_argument("--secondary", default="rules_alt")
    p.add_argument("--max-error-rate", type=float, default=0.0)
    p.add_argument("--margin", type=float, default=0.05)
    p.add_argument("--out", default="eval/results")
    a = p.parse_args()
    if Path(a.split).name == "test":
        raise SystemExit("refusing to calibrate on the test split")

    cfg = load_config(a.config)
    records = [r for r in run_split(Path(a.split), cfg, get_extractor(a.extractor), get_extractor(a.secondary),
                                    calibration=None, threshold=0.0) if r["outcome"] != "error"]
    X = np.array([[r["features"][f] for f in FEATURES] for r in records], dtype=float)
    y = np.array([label(r) for r in records])
    at_gate3 = np.array([r["gate"] in ("passed", "gate3_confidence") for r in records])

    mean, scale = X.mean(axis=0), X.std(axis=0)
    scale[scale == 0] = 1.0
    degenerate = len(set(y.tolist())) < 2
    if degenerate:
        coef, intercept = np.zeros(len(FEATURES)), math.log((y.mean() + 1e-3) / (1 - y.mean() + 1e-3))
    else:
        from sklearn.linear_model import LogisticRegression

        model = LogisticRegression(C=1.0, max_iter=1000, class_weight="balanced").fit((X - mean) / scale, y)
        coef, intercept = model.coef_[0], float(model.intercept_[0])
    scores = 1 / (1 + np.exp(-(((X - mean) / scale) @ coef + intercept)))

    rows = sweep(scores[at_gate3], y[at_gate3])
    ok = [r for r in rows if r["approved"] == 0 or r["error_rate"] <= a.max_error_rate]
    threshold = min(1.0, (ok[0]["threshold"] if ok else 1.0) + a.margin)
    threshold = max(threshold, 0.5)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    cal = {"features": FEATURES, "coef": [float(c) for c in coef], "intercept": float(intercept),
           "mean": mean.tolist(), "scale": scale.tolist(), "threshold": round(float(threshold), 4),
           "fitted_on": str(a.split), "n": int(len(y)), "positives": int(y.sum()),
           "reaching_gate3": int(at_gate3.sum()),
           "degenerate": degenerate, "max_error_rate": a.max_error_rate, "margin": a.margin, "sweep": rows}
    (out / "calibration.json").write_text(json.dumps(cal, indent=2))

    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(7, 4))
    ts = [r["threshold"] for r in rows]
    ax.plot(ts, [r["automation"] for r in rows], label="share auto-approved (of cases reaching gate 3)")
    ax.plot(ts, [r["error_rate"] for r in rows], label="wrong among auto-approved")
    ax.axvline(threshold, linestyle="--", color="gray", label=f"chosen threshold {threshold:.2f}")
    ax.set_xlabel("confidence threshold")
    ax.set_ylim(-0.02, 1.02)
    ax.legend(loc="center left", fontsize=8)
    ax.set_title("Gate 3 threshold sweep (dev split)")
    fig.tight_layout()
    fig.savefig(out / "threshold_sweep.png", dpi=120)
    print(json.dumps({k: v for k, v in cal.items() if k != "sweep"}, indent=2))


if __name__ == "__main__":
    main()
