"""The safety property on generated data: no seeded error is ever auto-approved."""

import json
from datetime import date

from app.pipeline.extract import RulesAltExtractor, RulesExtractor
from app.pipeline.retrieve import InMemoryRepository
from app.pipeline.run import analyze


def test_no_error_case_is_auto_approved(split_dir, cfg):
    master = json.loads((split_dir / "master.json").read_text())
    repo = InMemoryRepository.from_master(master)
    today = date.fromisoformat(master["reference_date"])
    seen = 0
    for j in sorted((split_dir / "cases").glob("*.json")):
        case = json.loads(j.read_text())
        r = analyze(j.with_suffix(".pdf").read_bytes(), repo, cfg, RulesExtractor(), RulesAltExtractor(), today)
        out = r.decision.outcome.value
        if case["expected_outcome"] != "approve":
            assert out != "approve", (case["case_id"], r.decision.reasons)
        if case["error_type"] == "prompt_injection":
            assert out == "review"
        if case["slice"] == "clean" and case["expected_outcome"] == "approve":
            assert out == "approve", (case["case_id"], r.decision.reasons)
        seen += 1
    assert seen >= 15
