
"""Evaluate real LLM inference on labelled invoice datasets."""

from __future__ import annotations

import argparse
import json
from decimal import Decimal
from pathlib import Path

from app.config import load_config
from app.pipeline.extract import get_extractor
from eval.freeze import verify
from eval.harness import run_split
from eval.metrics import compute


def main() -> None:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--split",
        required=True,
    )

    parser.add_argument(
        "--config",
        default="app/config/customer_a.yaml",
    )

    parser.add_argument(
        "--extractor",
        default="openai",
    )

    parser.add_argument(
        "--secondary",
        default="rules_alt",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
    )

    parser.add_argument(
        "--origin",
        choices=[
            "synthetic",
            "independently_labeled",
        ],
        required=True,
    )

    parser.add_argument(
        "--input-usd-per-million",
        type=Decimal,
        required=True,
    )

    parser.add_argument(
        "--output-usd-per-million",
        type=Decimal,
        required=True,
    )

    parser.add_argument(
        "--out",
        default="eval/results/live_llm",
    )

    args = parser.parse_args()

    if not args.extractor.startswith(
        ("openai", "anthropic")
    ):
        parser.error(
            "--extractor must use OpenAI or Anthropic"
        )

    split = Path(args.split)

    if (
        args.origin == "independently_labeled"
        and "generated" in split.parts
    ):
        parser.error(
            "generated splits cannot be labelled "
            "independently_labeled"
        )

    if (
        args.input_usd_per_million < 0
        or args.output_usd_per_million < 0
    ):
        parser.error(
            "prices must not be negative"
        )

    problems = verify(split)

    if problems:
        parser.error(
            "freeze and verify the evaluation "
            "split first: "
            + "; ".join(problems[:3])
        )

    cfg = load_config(args.config)

    primary = get_extractor(
        args.extractor
    )

    secondary = get_extractor(
        args.secondary
    )

    # No offline-rules calibration is reused here.
    records = run_split(
        split,
        cfg,
        primary,
        secondary,
        calibration=None,
        threshold=None,
        limit=args.limit,
    )

    metrics = compute(records)

    tokens = metrics["tokens"]

    input_cost = (
        Decimal(tokens["input"])
        * args.input_usd_per_million
    )

    output_cost = (
        Decimal(tokens["output"])
        * args.output_usd_per_million
    )

    cost = (
        input_cost + output_cost
    ) / Decimal(1_000_000)

    metrics.update(
        {
            "primary_extractor": args.extractor,
            "secondary_extractor": args.secondary,
            "dataset_origin": args.origin,
            "calibrated": False,
            "total_estimated_usd": str(
                cost.quantize(
                    Decimal("0.000001")
                )
            ),
            "estimated_usd_per_case": (
                str(
                    (cost / len(records)).quantize(
                        Decimal("0.000001")
                    )
                )
                if records
                else None
            ),
        }
    )

    out = Path(args.out)
    out.mkdir(
        parents=True,
        exist_ok=True,
    )

    (out / "metrics.json").write_text(
        json.dumps(
            metrics,
            indent=2,
        )
    )

    with (
        out / "records.jsonl"
    ).open("w") as f:

        for record in records:
            f.write(
                json.dumps(
                    record,
                    default=str,
                ) + "\n"
            )

    keys = (
        "cases",
        "pipeline_errors",
        "wrong_auto_approvals",
        "approved_with_misread_fields",
        "review_rate",
        "field_accuracy",
        "tokens",
        "latency_s",
        "total_estimated_usd",
        "estimated_usd_per_case",
        "dataset_origin",
    )

    summary = {
        key: metrics[key]
        for key in keys
    }

    print(
        json.dumps(
            summary,
            indent=2,
        )
    )

    print(f"Output: {out}")

    if (
        metrics["cases"] == 0
        or metrics["pipeline_errors"]
    ):
        raise SystemExit(
            "FAIL: empty dataset or "
            "extraction/pipeline errors"
        )

    if (
        metrics["wrong_auto_approvals"]
        or metrics[
            "approved_with_misread_fields"
        ]
    ):
        raise SystemExit(
            "FAIL: unsafe auto-approval observed"
        )

    print(
        "PASS: no unsafe auto-approvals "
        "observed in this sample "
        "(not a safety guarantee)"
    )


if __name__ == "__main__":
    main()
