
"""Regression tests for AP safety and QuickBooks."""

import json

import httpx
import pytest

from app.actions.ap_client import APRejected
from app.actions.guard import verify_readback
from app.actions.qbo_client import QBOClient
from app.settings import get_settings


PAYLOAD = {
    "tenant_slug": "acme",
    "vendor_name": "Cedar Supplies",
    "invoice_number": "INV-100",
    "po_number": "PO-200",
    "currency": "USD",
    "amount": "150.00",
    "status": "bill_created",
}


def test_readback_must_match_all_fields():

    assert verify_readback(
        PAYLOAD, PAYLOAD
    ) == []

    for field in (
        "vendor_name",
        "invoice_number",
        "po_number",
        "currency",
        "status",
        "amount",
    ):
        altered = dict(PAYLOAD)
        altered.pop(field)

        assert field in verify_readback(
            PAYLOAD,
            altered,
        )

    assert "amount" in verify_readback(
        PAYLOAD,
        {
            **PAYLOAD,
            "amount": "150.01",
        },
    )


def test_quickbooks_sandbox_adapter(monkeypatch):

    monkeypatch.setenv(
        "QBO_COMPANY_ID", "123"
    )
    monkeypatch.setenv(
        "QBO_ACCESS_TOKEN", "example-token"
    )
    monkeypatch.setenv(
        "QBO_TENANT_SLUG", "acme"
    )
    monkeypatch.setenv(
        "QBO_VENDOR_ID", "41"
    )
    monkeypatch.setenv(
        "QBO_VENDOR_NAME", "Cedar Supplies"
    )
    monkeypatch.setenv(
        "QBO_EXPENSE_ACCOUNT_ID", "7"
    )

    get_settings.cache_clear()

    key = "approve:example:v1"

    note = QBOClient._note(
        key,
        PAYLOAD["po_number"],
    )

    bill = {
        "Id": "99",
        "DocNumber": "INV-100",
        "CurrencyRef": {
            "value": "USD",
        },
        "VendorRef": {
            "value": "41",
        },
        "TotalAmt": 150,
        "PrivateNote": note,
    }

    seen = []

    def send(req: httpx.Request):
        seen.append(
            (req.method, str(req.url))
        )

        if (
            req.method == "POST"
            and req.url.path.endswith("/bill")
        ):
            body = json.loads(
                req.content
            )

            assert (
                body["VendorRef"]["value"]
                == "41"
            )

            assert body["PrivateNote"] == note
            assert "requestid=" in str(req.url)

            return httpx.Response(
                200,
                json={"Bill": bill},
            )

        if req.url.path.endswith("/vendor/41"):
            return httpx.Response(
                200,
                json={
                    "Vendor": {
                        "DisplayName": "Cedar Supplies"
                    }
                },
            )

        if req.url.path.endswith("/bill/99"):
            return httpx.Response(
                200,
                json={"Bill": bill},
            )

        if req.url.path.endswith("/query"):
            return httpx.Response(
                200,
                json={
                    "QueryResponse": {
                        "Bill": [bill]
                    }
                },
            )

        return httpx.Response(
            404,
            json={"error": "missing"},
        )

    with httpx.Client(
        base_url=(
            "https://sandbox-quickbooks.api.intuit.com"
            "/v3/company/123"
        ),
        transport=httpx.MockTransport(send),
    ) as http:

        qbo = QBOClient(client=http)

        assert qbo.create_bill(
            key, PAYLOAD
        )["id"] == "99"

        assert qbo.find_by_key(
            key, PAYLOAD
        )["id"] == "99"

        assert verify_readback(
            PAYLOAD,
            qbo.get_bill("99"),
        ) == []

        with pytest.raises(APRejected):
            qbo.create_bill(
                "other",
                {
                    **PAYLOAD,
                    "tenant_slug": "borealis",
                },
            )

    assert len(seen) == 4
    get_settings.cache_clear()
