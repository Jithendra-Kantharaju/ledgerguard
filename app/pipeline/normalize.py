"""Parse money and dates the way they appear on real invoices."""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_MONEY_RE = re.compile(r"-?[\d.,]+")


def parse_money(raw: str | int | float | Decimal | None) -> Decimal | None:
    """Handle '1,234.56', '1.234,56', '1234.56', '$1,234.56', '1 234,56'."""
    if raw is None:
        return None
    if isinstance(raw, Decimal):
        return raw
    if isinstance(raw, (int, float)):
        return Decimal(str(raw))
    s = raw.strip().replace(" ", "").replace(" ", "")
    m = _MONEY_RE.search(s)
    if not m:
        return None
    s = m.group(0)
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):  # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:  # 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        head, _, tail = s.rpartition(",")
        s = f"{head.replace(',', '')}.{tail}" if len(tail) == 2 else s.replace(",", "")
    elif s.count(".") > 1:  # 1.234.567
        s = s.replace(".", "")
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


_DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y", "%d.%m.%Y"]


def parse_date(raw: str | date | None) -> date | None:
    if raw is None:
        return None
    if isinstance(raw, date):
        return raw
    s = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def norm_text(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
