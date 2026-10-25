import pytest
from fastapi.testclient import TestClient

from app.actions.ap_client import APClient, APUnknownOutcome
from app.mock_ap.server import app

BILL = {"vendor_name": "Cedar", "invoice_number": "INV-1", "currency": "USD", "amount": "54.00", "status": "approved"}


@pytest.fixture
def ap():
    c = TestClient(app, base_url="http://ap")
    c.post("/_reset")
    return c


def test_idempotent_create(ap):
    client = APClient(client=ap)
    a = client.create_bill("k1", BILL)
    b = client.create_bill("k1", BILL)
    assert a["id"] == b["id"]
    assert len(ap.get("/_state").json()["bills"]) == 1


def test_lost_response_is_unknown_but_findable(ap):
    client = APClient(client=ap)
    ap.post("/_faults", json={"lose_response_after_commit": 1})
    with pytest.raises(APUnknownOutcome):
        client.create_bill("k2", BILL)
    assert client.find_by_key("k2")["invoice_number"] == "INV-1"
    assert client.find_by_key("nope") is None


def test_requires_token(ap):
    assert ap.post("/bills", json=BILL, headers={"Idempotency-Key": "x"}).status_code == 401
