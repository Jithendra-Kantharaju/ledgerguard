
"""QuickBooks Online sandbox Bill adapter.

Creates Bills only. Never creates BillPayments.
Restricted to one configured tenant and vendor.
"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal

import httpx

from app.actions.ap_client import (
    APRejected,
    APUnknownOutcome,
)
from app.settings import get_settings


class QBOClient:

    def __init__(
        self,
        client: httpx.Client | None = None,
    ):
        s = get_settings()

        required = (
            s.qbo_company_id,
            s.qbo_access_token,
            s.qbo_vendor_id,
            s.qbo_vendor_name,
            s.qbo_expense_account_id,
        )

        if not all(required):
            raise ValueError(
                "QuickBooks sandbox requires company, "
                "access token, vendor ID/name and "
                "expense account ID"
            )

        self.tenant = s.qbo_tenant_slug
        self.vendor_id = s.qbo_vendor_id
        self.vendor_name = s.qbo_vendor_name
        self.expense_account = (
            s.qbo_expense_account_id
        )

        self.http = client or httpx.Client(
            base_url=(
                "https://sandbox-quickbooks.api.intuit.com"
                f"/v3/company/{s.qbo_company_id}"
            ),
            timeout=s.ap_timeout_seconds,
        )

        self.headers = {
            "Authorization": (
                f"Bearer {s.qbo_access_token}"
            ),
            "Accept": "application/json",
        }

    def _call(
        self,
        method: str,
        path: str,
        **kwargs,
    ) -> dict:

        try:
            response = self.http.request(
                method,
                path,
                headers=self.headers,
                **kwargs,
            )
        except httpx.HTTPError as exc:
            raise APUnknownOutcome(
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if (
            response.status_code >= 500
            or response.status_code == 429
        ):
            raise APUnknownOutcome(
                f"QuickBooks HTTP {response.status_code}"
            )

        if response.status_code >= 400:
            raise APRejected(
                f"QuickBooks HTTP {response.status_code}: "
                f"{response.text[:250]}"
            )

        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
            raise APUnknownOutcome(
                "QuickBooks returned invalid JSON"
            ) from exc

        if "Fault" in data:
            raise APRejected(
                "QuickBooks fault: "
                + str(data["Fault"])[:250]
            )

        return data

    def _validate(self, payload: dict) -> None:

        if payload.get("tenant_slug") != self.tenant:
            raise APRejected(
                "QuickBooks tenant is not mapped "
                "to this company"
            )

        if (
            payload.get("vendor_name")
            != self.vendor_name
        ):
            raise APRejected(
                "QuickBooks vendor name has "
                "no verified mapping"
            )

        number = payload.get("invoice_number")

        if (
            not isinstance(number, str)
            or not re.fullmatch(
                r"[A-Za-z0-9._/-]{1,21}",
                number,
            )
        ):
            raise APRejected(
                "QuickBooks DocNumber requires "
                "1-21 supported characters"
            )

        if (
            payload.get("status") != "bill_created"
            or not payload.get("po_number")
        ):
            raise APRejected(
                "invalid QuickBooks action or missing PO"
            )

        try:
            amount = Decimal(
                str(payload.get("amount", "0"))
            )
        except Exception as exc:
            raise APRejected(
                "invalid amount"
            ) from exc

        if (
            not payload.get("currency")
            or not amount.is_finite()
            or amount <= 0
        ):
            raise APRejected(
                "invalid currency/amount"
            )

    @staticmethod
    def _note(
        key: str,
        po_number: str,
    ) -> str:

        return "LEDGERGUARD1:" + json.dumps(
            {
                "key": key,
                "po": po_number,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _read_note(bill: dict) -> dict:

        note = bill.get("PrivateNote", "")

        if (
            not isinstance(note, str)
            or not note.startswith("LEDGERGUARD1:")
        ):
            raise APRejected(
                "bill is missing LedgerGuard "
                "PO/idempotency evidence"
            )

        try:
            obj = json.loads(
                note[len("LEDGERGUARD1:"):]
            )

            if (
                not isinstance(obj, dict)
                or not obj.get("key")
                or not obj.get("po")
            ):
                raise ValueError("incomplete note")

            return obj

        except (ValueError, TypeError) as exc:
            raise APRejected(
                "bill note is malformed"
            ) from exc

    def create_bill(
        self,
        idempotency_key: str,
        payload: dict,
    ) -> dict:

        self._validate(payload)

        body = {
            "VendorRef": {
                "value": self.vendor_id,
            },
            "DocNumber": payload[
                "invoice_number"
            ],
            "CurrencyRef": {
                "value": payload["currency"],
            },
            "PrivateNote": self._note(
                idempotency_key,
                payload["po_number"],
            ),
            "Line": [
                {
                    "Amount": float(
                        Decimal(
                            str(payload["amount"])
                        )
                    ),
                    "DetailType": (
                        "AccountBasedExpenseLineDetail"
                    ),
                    "AccountBasedExpenseLineDetail": {
                        "AccountRef": {
                            "value": self.expense_account,
                        }
                    },
                }
            ],
        }

        request_id = hashlib.sha256(
            idempotency_key.encode()
        ).hexdigest()[:32]

        result = self._call(
            "POST",
            "/bill",
            json=body,
            params={
                "requestid": request_id,
            },
        )

        bill = result.get("Bill") or {}

        if not bill.get("Id"):
            raise APUnknownOutcome(
                "QuickBooks response did not "
                "include Bill.Id"
            )

        return {
            "id": str(bill["Id"])
        }

    def find_by_key(
        self,
        idempotency_key: str,
        payload: dict | None = None,
    ) -> dict | None:

        if payload is None:
            raise APRejected(
                "QuickBooks recovery needs "
                "the original outbox payload"
            )

        self._validate(payload)

        number = payload["invoice_number"]

        query = (
            "SELECT * FROM Bill "
            f"WHERE DocNumber = '{number}' "
            "MAXRESULTS 100"
        )

        results = self._call(
            "GET",
            "/query",
            params={"query": query},
        )

        bills = results.get(
            "QueryResponse", {}
        ).get("Bill", [])

        for bill in bills:
            try:
                note = self._read_note(bill)

                if (
                    note["key"]
                    == idempotency_key
                ):
                    return {
                        "id": str(bill["Id"])
                    }

            except APRejected:
                continue

        return None

    def get_bill(
        self,
        bill_id: str,
    ) -> dict:

        bill = self._call(
            "GET",
            f"/bill/{bill_id}",
        ).get("Bill") or {}

        if (
            str(bill.get("Id"))
            != str(bill_id)
        ):
            raise APRejected(
                "unexpected Bill.Id"
            )

        ref = bill.get("VendorRef") or {}

        if (
            str(ref.get("value"))
            != str(self.vendor_id)
        ):
            raise APRejected(
                "QuickBooks VendorRef differs "
                "from configured vendor"
            )

        vendor = self._call(
            "GET",
            f"/vendor/{self.vendor_id}",
        ).get("Vendor") or {}

        note = self._read_note(bill)

        return {
            "id": str(bill["Id"]),
            "vendor_name": vendor.get(
                "DisplayName"
            ),
            "invoice_number": bill.get(
                "DocNumber"
            ),
            "po_number": note["po"],
            "currency": (
                bill.get("CurrencyRef") or {}
            ).get("value"),
            "amount": bill.get("TotalAmt"),
            "status": "bill_created",
        }
