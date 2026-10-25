"""Freeze a test split: record a SHA-256 of every file so later runs can prove nothing changed.

Usage:
  python -m eval.freeze data/generated/test          # write MANIFEST.sha256
  python -m eval.freeze data/generated/test --check  # verify
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

MANIFEST = "MANIFEST.sha256"


def _files(split: Path) -> list[Path]:
    return sorted(p for p in split.rglob("*") if p.is_file() and p.name != MANIFEST)


def freeze(split: Path) -> int:
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(split)}" for p in _files(split)]
    (split / MANIFEST).write_text("\n".join(lines) + "\n")
    return len(lines)


def verify(split: Path) -> list[str]:
    m = split / MANIFEST
    if not m.exists():
        return [f"no {MANIFEST} in {split}; run python -m eval.freeze {split}"]
    expected = dict(reversed(line.split("  ", 1)) for line in m.read_text().splitlines() if line)
    actual = {str(p.relative_to(split)): hashlib.sha256(p.read_bytes()).hexdigest() for p in _files(split)}
    problems = [f"changed: {k}" for k in expected if k in actual and actual[k] != expected[k]]
    problems += [f"missing: {k}" for k in expected if k not in actual]
    problems += [f"added: {k}" for k in actual if k not in expected]
    return problems


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("split")
    p.add_argument("--check", action="store_true")
    a = p.parse_args()
    if a.check:
        probs = verify(Path(a.split))
        if probs:
            sys.exit("\n".join(probs))
        print("frozen test set verified")
    else:
        print(f"froze {freeze(Path(a.split))} files")
