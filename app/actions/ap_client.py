
"""AP client with explicit rejected and unknown outcomes."""

from __future__ import annotations

import httpx

from app.settings import get_settings


class APRejected(Exception):
    """AP system rejected the request."""


class APUnknownOutcome(Exception):
    """Request may or may not have succeeded."""


class APClient:
    """HTTP client for the existing mock AP service."""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
        token: str | None = None,
    ):
        settings = get_settings()

        token = token or settings.ap_api_token

        self.http = client or httpx.Client(
            base_url=(
                base_url or settings.ap_api_url
            ),
            timeout=(
                timeout
                or settings.ap_timeout_seconds
            ),
        )

        self.headers = {
            "Authorization": f"Bearer {token}"
        }

    def _call(
        self,
        method: str,
        url: str,
        **kwargs,
    ) -> httpx.Response:

        try:
            response = self.http.request(
                method,
                url,
                headers={
                    **self.headers,
                    **kwargs.pop("headers", {}),
                },
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
                f"HTTP {response.status_code}"
            )

        return response

    def create_bill(
        self,
        idempotency_key: str,
        payload: dict,
    ) -> dict:

        response = self._call(
            "POST",
            "/bills",
            json=payload,
            headers={
                "Idempotency-Key": idempotency_key
            },
        )

        if response.status_code >= 400:
            raise APRejected(
                f"HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )

        return response.json()

    def find_by_key(
        self,
        idempotency_key: str,
        payload: dict | None = None,
    ) -> dict | None:

        response = self._call(
            "GET",
            "/bills",
            params={
                "idempotency_key": idempotency_key
            },
        )

        if response.status_code == 404:
            return None

        if response.status_code >= 400:
            raise APRejected(
                f"HTTP {response.status_code}"
            )

        return response.json()

    def get_bill(
        self,
        bill_id: str,
    ) -> dict:

        response = self._call(
            "GET",
            f"/bills/{bill_id}",
        )

        if response.status_code >= 400:
            raise APRejected(
                f"HTTP {response.status_code}"
            )

        return response.json()


def make_ap_client():
    """Choose AP adapter using AP_PROVIDER."""

    provider = get_settings().ap_provider

    if provider == "mock":
        return APClient()

    if provider == "quickbooks":
        from app.actions.qbo_client import QBOClient

        return QBOClient()

    raise ValueError(
        f"unsupported AP_PROVIDER={provider!r}"
    )
