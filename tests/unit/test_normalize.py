from datetime import date
from decimal import Decimal

import pytest

from app.pipeline.normalize import parse_date, parse_money


@pytest.mark.parametrize("raw,expected", [
    ("1,234.56", "1234.56"), ("$ 1,234.56", "1234.56"), ("1.234,56", "1234.56"), ("EUR 99", "99"),
    ("0.50", "0.50"), (None, None), ("", None), ("n/a", None),
])
def test_parse_money(raw, expected):
    got = parse_money(raw)
    assert got == (Decimal(expected) if expected else None)


@pytest.mark.parametrize("raw,expected", [
    ("2026-08-14", date(2026, 8, 14)), ("08/14/2026", date(2026, 8, 14)), ("August 14, 2026", date(2026, 8, 14)),
    ("14.08.2026", date(2026, 8, 14)), ("garbage", None), (None, None),
])
def test_parse_date(raw, expected):
    assert parse_date(raw) == expected
