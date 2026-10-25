"""Run the production pipeline over a generated split and record everything per case."""

from __future__ import annotations

import json
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.config import CustomerConfig
from app.pipeline.confidence import Calibration
from app.pipeline.extract import Extractor
from app.pipeline.parse import parse_document
from app.pipeline.retrieve import InMemoryRepository
from app.pipeline.run import assess, extract_invoice
from app.schemas import Invoice

FIELDS = ["invoice_number", "vendor_name", "po_number", "invoice_date", "currency", "subtotal", "tax", "total"]


def field_correct(inv: Invoice, truth: dict[str, Any]) -> dict[str, bool]:
    out = {}
    for f in FIELDS:
        got, want = getattr(inv, f), truth.get(f)
        if f in ("subtotal", "tax", "total"):
            out[f] = got is not None and want is not None and got == Decimal(str(want))
        elif f == "invoice_date":
            out[f] = got is not None and got.isoformat() == want
        else:
            out[f] = got is not None and want is not None and "".join(str(got).split()) == "".join(str(want).split())
    want_lines = truth.get("lines", [])
    out["lines"] = len(inv.lines) == len(want_lines) and all(
        a.amount == Decimal(str(b["amount"])) for a, b in zip(sorted(inv.lines, key=lambda x: x.amount),
                         sorted(want_lines, key=lambda x: Decimal(x["amount"])), strict=True))
    return out


def load_cases(split_dir: Path) -> list[tuple[dict, Path]]:
    return [(json.loads(j.read_text()), j.with_suffix(".pdf")) for j in sorted((split_dir / "cases").glob("*.json"))]


def run_split(split_dir: Path, cfg: CustomerConfig, primary: Extractor, secondary: Extractor | None,
              calibration: Calibration | None = None, threshold: float | None = None,
              limit: int | None = None) -> list[dict]:
    master = json.loads((split_dir / "master.json").read_text())
    repo = InMemoryRepository.from_master(master)
    today = date.fromisoformat(master["reference_date"])
    records = []
    for case, pdf in load_cases(split_dir)[:limit]:
        t0 = time.perf_counter()
        parsed = parse_document(pdf.read_bytes())
        try:
            ex = extract_invoice(parsed, primary, secondary)
            r = assess(parsed, ex, repo, cfg, today, calibration, threshold=threshold)
            error = None
        except Exception as e:  # noqa: BLE001 - an extraction failure is a result, not a crash
            records.append({**_case_info(case), "outcome": "error", "error": f"{type(e).__name__}: {e}"})
            continue
        tokens_in = ex.primary.input_tokens + (ex.secondary.input_tokens if ex.secondary else 0)
        tokens_out = ex.primary.output_tokens + (ex.secondary.output_tokens if ex.secondary else 0)
        records.append({
            **_case_info(case),
            "outcome": r.decision.outcome.value, "gate": r.decision.gate, "reasons": r.decision.reasons,
            "confidence": r.confidence.score, "calibrated": r.confidence.calibrated,
            "features": r.confidence.features, "field_correct": field_correct(ex.primary.invoice, case["truth"]),
            "parse_method": parsed.method, "model": ex.primary.model, "input_tokens": tokens_in,
            "output_tokens": tokens_out, "latency_s": round(time.perf_counter() - t0, 3), "error": error,
        })
    return records


def _case_info(case: dict) -> dict:
    return {k: case[k] for k in ("case_id", "slice", "error_type", "scanned", "approvable", "expected_outcome")}
