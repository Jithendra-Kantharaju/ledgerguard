"""Client for the AP system. Separates "it failed" from "we don't know if it worked"."""

from __future__ import annotations

import httpx

from app.settings import get_settings


class APRejected(Exception):
    """The AP system refused the request (4xx). Retrying will not help."""


class APUnknownOutcome(Exception):
    """Timeout, connection error or 5xx: the bill may or may not have been created."""


class APClient:
    def __init__(self, base_url: str | None = None, timeout: float | None = None, client: httpx.Client | None = None,
                 token: str | None = None):
        s = get_settings()
        token = token or s.ap_api_token
        self.http = client or httpx.Client(base_url=base_url or s.ap_api_url,
                                           timeout=timeout or s.ap_timeout_seconds)
        self.headers = {"Authorization": f"Bearer {token}"}

    def _call(self, method: str, url: str, **kw) -> httpx.Response:
        try:
            resp = self.http.request(method, url, headers={**self.headers, **kw.pop("headers", {})}, **kw)
        except httpx.HTTPError as e:  # timeouts, connection resets
            raise APUnknownOutcome(f"{type(e).__name__}: {e}") from e
        if resp.status_code >= 500:
            raise APUnknownOutcome(f"HTTP {resp.status_code}")
        return resp

    def create_bill(self, idempotency_key: str, payload: dict) -> dict:
        resp = self._call("POST", "/bills", json=payload, headers={"Idempotency-Key": idempotency_key})
        if resp.status_code >= 400:
            raise APRejected(f"HTTP {resp.status_code}: {resp.text[:300]}")
        return resp.json()

    def find_by_key(self, idempotency_key: str) -> dict | None:
        resp = self._call("GET", "/bills", params={"idempotency_key": idempotency_key})
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise APRejected(f"HTTP {resp.status_code}")
        return resp.json()

    def get_bill(self, bill_id: str) -> dict:
        resp = self._call("GET", f"/bills/{bill_id}")
        if resp.status_code >= 400:
            raise APRejected(f"HTTP {resp.status_code}")
        return resp.json()
