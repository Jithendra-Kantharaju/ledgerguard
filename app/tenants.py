"""Per-tenant customer configuration, looked up from the tenants table."""

from __future__ import annotations

from functools import lru_cache

import psycopg

from app.config import CustomerConfig, load_config
from app.settings import ROOT

CONFIG_DIR = ROOT / "app" / "config"


@lru_cache
def _load(config_file: str) -> CustomerConfig:
    path = (CONFIG_DIR / config_file).resolve()
    if CONFIG_DIR.resolve() not in path.parents:
        raise ValueError("config file must live in app/config")
    return load_config(path)


def config_for(conn: psycopg.Connection, tenant_id: str) -> CustomerConfig:
    row = conn.execute("SELECT config_file FROM tenants WHERE id = %s", (tenant_id,)).fetchone()
    if not row:
        raise LookupError(f"unknown tenant {tenant_id}")
    return _load(row["config_file"])
