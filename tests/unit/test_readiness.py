
"""Regression tests for database readiness."""

from contextlib import contextmanager
from importlib import import_module

import pytest
from fastapi import HTTPException

api = import_module("app.api.main")


class FakeConnection:
    def execute(self, query):
        assert query == "SELECT 1"


class HealthyPool:
    @contextmanager
    def connection(self):
        yield FakeConnection()


class UnhealthyPool:
    def connection(self):
        raise ConnectionError("Simulated database outage")


def test_readyz_uses_pool_factory(monkeypatch):
    monkeypatch.setattr(
        api,
        "pool",
        lambda: HealthyPool(),
    )

    assert api.readyz() == {"ok": True}


def test_readyz_rejects_database_failure(monkeypatch):
    monkeypatch.setattr(
        api,
        "pool",
        lambda: UnhealthyPool(),
    )

    with pytest.raises(HTTPException) as exc:
        api.readyz()

    assert exc.value.status_code == 503
    assert exc.value.detail == "Database unavailable"
