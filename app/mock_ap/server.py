"""A stand-in for the customer's AP system (QuickBooks, NetSuite, SAP...), with fault injection.

It behaves the way real AP APIs do in the cases that matter:
  * POST /bills is idempotent on the Idempotency-Key header (a repeat returns the same bill).
  * GET /bills?idempotency_key=... answers "did my earlier request land?"
  * Faults can be switched on to test the dispatcher: fail before saving, save but lose
    the response (the classic timeout-after-commit), slow responses, or report a wrong
    status on read-back.

Run: uvicorn app.mock_ap.server:app --port 8001
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from decimal import Decimal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

app = FastAPI(title="Mock AP system")

_lock = threading.Lock()
_bills: dict[str, dict] = {}          # bill id -> bill
_by_key: dict[str, str] = {}          # idempotency key -> bill id
_faults = {"fail_before_commit": 0, "lose_response_after_commit": 0, "wrong_status_on_read": 0,
           "delay_seconds": 0.0}
_calls = {"create": 0}
_token = os.environ.get("MOCK_AP_TOKEN", "dev-ap-token")


class BillIn(BaseModel):
    vendor_name: str
    invoice_number: str
    po_number: str | None = None
    currency: str
    amount: Decimal
    status: str


class Faults(BaseModel):
    fail_before_commit: int = 0
    lose_response_after_commit: int = 0
    wrong_status_on_read: int = 0
    delay_seconds: float = 0.0


def _auth(authorization: str | None) -> None:
    if authorization != f"Bearer {_token}":
        raise HTTPException(401, "bad token")


def _take(name: str) -> bool:
    with _lock:
        if _faults[name] > 0:
            _faults[name] -= 1
            return True
    return False


@app.post("/bills")
def create_bill(bill: BillIn, idempotency_key: str = Header(...), authorization: str | None = Header(None)):
    _auth(authorization)
    _calls["create"] += 1
    if _faults["delay_seconds"]:
        time.sleep(_faults["delay_seconds"])
    if _take("fail_before_commit"):
        return JSONResponse({"error": "temporarily unavailable"}, status_code=503)
    with _lock:
        if idempotency_key in _by_key:
            existing = _bills[_by_key[idempotency_key]]
            return JSONResponse(existing, status_code=200)
        bill_id = "BILL-" + uuid.uuid4().hex[:10].upper()
        record = {"id": bill_id, "idempotency_key": idempotency_key, **bill.model_dump(mode="json")}
        _bills[bill_id] = record
        _by_key[idempotency_key] = bill_id
    if _take("lose_response_after_commit"):
        return JSONResponse({"error": "gateway timeout"}, status_code=504)  # saved, but caller never hears
    return JSONResponse(record, status_code=201)


@app.get("/bills")
def find_bill(idempotency_key: str, authorization: str | None = Header(None)):
    _auth(authorization)
    with _lock:
        bill_id = _by_key.get(idempotency_key)
        if not bill_id:
            raise HTTPException(404, "not found")
        return _bills[bill_id]


@app.get("/bills/{bill_id}")
def get_bill(bill_id: str, authorization: str | None = Header(None)):
    _auth(authorization)
    with _lock:
        bill = _bills.get(bill_id)
        if not bill:
            raise HTTPException(404, "not found")
        bill = dict(bill)
    if _take("wrong_status_on_read"):
        bill["status"] = "draft"
    return bill


# ---- test controls (would not exist in a real AP system)

@app.post("/_faults")
def set_faults(f: Faults):
    with _lock:
        _faults.update(f.model_dump())
    return _faults


@app.post("/_reset")
def reset():
    with _lock:
        _bills.clear()
        _by_key.clear()
        _faults.update(fail_before_commit=0, lose_response_after_commit=0, wrong_status_on_read=0,
                       delay_seconds=0.0)
        _calls["create"] = 0
    return {"ok": True}


@app.get("/_state")
def state():
    with _lock:
        return {"bills": list(_bills.values()), "faults": _faults, "calls": _calls}


@app.get("/healthz")
def healthz(request: Request):
    return {"ok": True}
