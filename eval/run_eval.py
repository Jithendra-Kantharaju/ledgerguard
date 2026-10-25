"""Evaluate the pipeline (and baselines) on a generated split.

Usage:
  python -m eval.run_eval --split data/generated/test
  python -m eval.run_eval --split data/generated/test --extractor anthropic --secondary rules_alt
  python -m eval.run_eval --split data/generated/test --baselines --max-wrong-approvals 0

Writes records.jsonl, metrics.json and report.md under --out.
Exits non-zero if wrong auto-approvals exceed --max-wrong-approvals (for CI).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.config import load_config
from app.pipeline.confidence import Calibration
from app.pipeline.extract import get_extractor
from app.settings import get_settings
from eval import baselines
from eval.freeze import verify
from eval.harness import run_split
from eval.metrics import compute


def report(name: str, m: dict, base: dict[str, dict]) -> str:
    lines = [f"# Evaluation: {name}", "",
             f"Cases: {m['cases']}  |  pipeline errors: {m['pipeline_errors']}", "",
             "## Safety and automation", "",
             "| system | wrong auto-approvals | approved with misread fields | false rejections "
             "| automation on approvable | review rate |",
             "|---|---|---|---|---|---|"]
    for label, mm in {"pipeline": m, **base}.items():
        lines.append(f"| {label} | {mm['wrong_auto_approvals']} | {mm['approved_with_misread_fields']} | "
                     f"{mm['false_rejections']} | "
                     f"{mm['automation_rate_on_approvable']} | {mm['review_rate']} |")
    lines += ["", "## Seeded errors stopped (not auto-approved)", "", "| error type | n | stopped | outcomes |",
              "|---|---|---|---|"]
    for t, r in m["error_recall"].items():
        lines.append(f"| {t} | {r['n']} | {r['stopped']} | {r['outcomes']} |")
    lines += ["", "## Field accuracy (primary extractor)", ""]
    for kind, acc in m["field_accuracy"].items():
        lines.append(f"**{kind}**: " + ", ".join(f"{k} {v:.0%}" for k, v in acc.items()))
        lines.append("")
    lines += ["## Outcomes by slice", "", "```", json.dumps(m["by_slice"], indent=2), "```", "",
              f"Tokens: {m['tokens']}  |  latency per document: {m['latency_s']}", ""]
    if m["wrong_auto_approval_cases"]:
        lines += ["## Wrong auto-approvals", ""] + [f"- {c}" for c in m["wrong_auto_approval_cases"]] + [""]
    if m["approved_with_misread_fields_cases"]:
        lines += ["## Approved with a misread field", ""] + [f"- {c}" for c in m["approved_with_misread_fields_cases"]]
    return "\n".join(lines) + "\n"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--split", default="data/generated/test")
    p.add_argument("--config", default="app/config/customer_a.yaml")
    p.add_argument("--extractor", default="rules")
    p.add_argument("--secondary", default="rules_alt")
    p.add_argument("--calibration", default=str(get_settings().calibration_file))
    p.add_argument("--threshold", type=float, default=None, help="override the confidence threshold")
    p.add_argument("--baselines", action="store_true", help="also run the naive baseline")
    p.add_argument("--llm-judge", action="store_true", help="also run the LLM-as-judge baseline (needs API key)")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--out", default=None)
    p.add_argument("--max-wrong-approvals", type=int, default=None,
                   help="fail if wrong auto-approvals plus approvals with misread fields exceed this")
    p.add_argument("--require-frozen", action="store_true", help="refuse to run if the split's checksums changed")
    a = p.parse_args()

    split = Path(a.split)
    if a.require_frozen:
        problems = verify(split)
        if problems:
            sys.exit("frozen test set changed:\n  " + "\n  ".join(problems[:20]))
    cfg = load_config(a.config)
    cal = Calibration.load(a.calibration)
    primary, secondary = get_extractor(a.extractor), get_extractor(a.secondary)
    name = f"{split.name}-{a.extractor}-{a.secondary}"
    out = Path(a.out or f"eval/results/{name}")
    out.mkdir(parents=True, exist_ok=True)

    records = run_split(split, cfg, primary, secondary, cal, a.threshold, a.limit)
    m = compute(records)
    m["calibrated"] = cal is not None
    base = {}
    if a.baselines:
        base["naive_po_exists"] = compute(baselines.naive_po_exists(split, primary))
    if a.llm_judge:
        base["llm_judge"] = compute(baselines.llm_judge(split, limit=a.limit))

    with open(out / "records.jsonl", "w") as f:
        for r in records:
            f.write(json.dumps(r, default=str) + "\n")
    (out / "metrics.json").write_text(json.dumps({"pipeline": m, "baselines": base}, indent=2, default=str))
    (out / "report.md").write_text(report(name, m, base))
    print((out / "report.md").read_text())
    print(f"written to {out}/")
    bad = m["wrong_auto_approvals"] + m["approved_with_misread_fields"]
    if a.max_wrong_approvals is not None and bad > a.max_wrong_approvals:
        sys.exit(f"FAIL: {bad} unsafe auto-approvals > {a.max_wrong_approvals}")


if __name__ == "__main__":
    main()
