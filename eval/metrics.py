"""Metrics that matter to an AP team, computed from per-case records."""

from __future__ import annotations

from collections import defaultdict


def compute(records: list[dict]) -> dict:
    n = len(records)
    ok = [r for r in records if r["outcome"] != "error"]
    approvable = [r for r in ok if r["expected_outcome"] == "approve"]
    errors = [r for r in ok if r["error_type"]]

    wrong_approvals = [r["case_id"] for r in ok if r["outcome"] == "approve" and r["expected_outcome"] != "approve"]
    # Approved, but at least one key field was misread: the AP system gets wrong data.
    approved_bad_fields = [r["case_id"] for r in ok if r["outcome"] == "approve" and "field_correct" in r
                           and not all(r["field_correct"].values())]
    false_rejections = [r["case_id"] for r in ok if r["outcome"] == "reject" and r["approvable"]]

    recall: dict[str, dict] = {}
    by_type = defaultdict(list)
    for r in errors:
        by_type[r["error_type"]].append(r)
    for t, rs in sorted(by_type.items()):
        stopped = sum(r["outcome"] != "approve" for r in rs)
        recall[t] = {"n": len(rs), "stopped": stopped, "recall": round(stopped / len(rs), 4),
                     "outcomes": _count(r["outcome"] for r in rs)}

    field_acc: dict[str, dict] = {}
    for scanned in (False, True):
        rs = [r for r in ok if r["scanned"] == scanned and "field_correct" in r]
        if not rs:
            continue
        fields = rs[0]["field_correct"].keys()
        field_acc["scanned" if scanned else "digital"] = {
            f: round(sum(r["field_correct"][f] for r in rs) / len(rs), 4) for f in fields}

    return {
        "cases": n,
        "pipeline_errors": n - len(ok),
        "outcomes": _count(r["outcome"] for r in ok),
        "wrong_auto_approvals": len(wrong_approvals),
        "wrong_auto_approval_cases": wrong_approvals,
        "approved_with_misread_fields": len(approved_bad_fields),
        "approved_with_misread_fields_cases": approved_bad_fields,
        "false_rejections": len(false_rejections),
        "false_rejection_cases": false_rejections,
        "automation_rate_on_approvable": round(
            sum(r["outcome"] == "approve" for r in approvable) / len(approvable), 4) if approvable else None,
        "review_rate": round(sum(r["outcome"] == "review" for r in ok) / len(ok), 4) if ok else None,
        "error_recall": recall,
        "injection_outcomes": recall.get("prompt_injection", {}).get("outcomes"),
        "field_accuracy": field_acc,
        "by_slice": {s: _count(r["outcome"] for r in ok if r["slice"] == s) for s in sorted({r["slice"] for r in ok})},
        "tokens": {"input": sum(r.get("input_tokens", 0) for r in ok),
                   "output": sum(r.get("output_tokens", 0) for r in ok)},
        "latency_s": {"mean": round(sum(r.get("latency_s", 0) for r in ok) / len(ok), 3) if ok else None,
                      "p95": _p95([r.get("latency_s", 0) for r in ok])},
    }


def _count(xs) -> dict:
    out: dict = defaultdict(int)
    for x in xs:
        out[x] += 1
    return dict(sorted(out.items()))


def _p95(xs: list[float]) -> float | None:
    if not xs:
        return None
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(0.95 * len(xs)))], 3)
