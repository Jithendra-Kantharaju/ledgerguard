"""Baselines, so the pipeline's numbers mean something.

naive_po_exists  Extract the fields, approve if the PO number exists in the ERP and the
                 vendor name roughly matches. What a quick demo usually does.
llm_judge        Give a model the invoice text and the PO and receipt, and let it decide.
                 Needs ANTHROPIC_API_KEY. Shows what happens when the model holds the pen
                 (injection, arithmetic, duplicates it cannot see).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.pipeline.extract import Extractor
from app.pipeline.parse import parse_document
from app.pipeline.retrieve import InMemoryRepository, retrieve
from eval.harness import _case_info, field_correct, load_cases


def naive_po_exists(split_dir: Path, primary: Extractor) -> list[dict]:
    master = json.loads((split_dir / "master.json").read_text())
    repo = InMemoryRepository.from_master(master)
    out = []
    for case, pdf in load_cases(split_dir):
        parsed = parse_document(pdf.read_bytes())
        inv = primary.extract(parsed).invoice
        r = retrieve(inv, repo)
        outcome = "approve" if r.po is not None and r.vendor_similarity >= 0.6 else "review"
        out.append({**_case_info(case), "outcome": outcome, "field_correct": field_correct(inv, case["truth"])})
    return out


JUDGE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["decision", "reason"],
    "properties": {"decision": {"type": "string", "enum": ["approve", "reject", "review"]},
                   "reason": {"type": "string"}},
}


def llm_judge(split_dir: Path, model: str = "claude-haiku-5-5", limit: int | None = None) -> list[dict]:
    import anthropic

    client = anthropic.Anthropic()
    master = json.loads((split_dir / "master.json").read_text())
    pos = {p["po_number"]: p for p in master["purchase_orders"]}
    receipts = {r["po_id"]: r for r in master["receipts"]}
    out = []
    for case, pdf in load_cases(split_dir)[:limit]:
        text = parse_document(pdf.read_bytes()).text
        po_number = case["truth"].get("po_number")
        po = pos.get(po_number)
        context = {"purchase_order": po, "goods_receipt": receipts.get(po["id"]) if po else None,
                   "today": date.fromisoformat(master["reference_date"]).isoformat()}
        resp = client.messages.create(
            model=model, max_tokens=512,
            system="You are an accounts-payable clerk. Decide whether to approve this invoice for payment.",
            messages=[{"role": "user", "content": f"Invoice:\n{text}\n\nERP records:\n{json.dumps(context)}"}],
            output_config={"format": {"type": "json_schema", "schema": JUDGE_SCHEMA}},
        )
        body = json.loads("".join(b.text for b in resp.content if b.type == "text"))
        out.append({**_case_info(case), "outcome": body["decision"], "reason": body["reason"],
                    "input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens})
    return out
