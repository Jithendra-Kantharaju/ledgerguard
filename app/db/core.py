"""Postgres access: a connection pool for the app role, and tenant-scoped transactions."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.settings import get_settings

SCHEMA = Path(__file__).with_name("schema.sql")

TENANT_TABLES = ["users", "vendors", "purchase_orders", "receipts", "documents", "corrections", "reviews",
                 "processed_invoices", "audit_log"]


@lru_cache
def pool() -> ConnectionPool:
    p = ConnectionPool(get_settings().database_url, min_size=1, max_size=10, open=True,
                       kwargs={"row_factory": dict_row, "autocommit": False})
    return p


@contextmanager
def tx(tenant_id: str | None = None) -> Iterator[psycopg.Connection]:
    """One transaction. With tenant_id, row-level security limits every query to that tenant."""
    with pool().connection() as conn:
        with conn.transaction():
            if tenant_id is not None:
                conn.execute("SELECT set_config('app.tenant_id', %s, true)", (str(tenant_id),))
            yield conn


def audit(conn: psycopg.Connection, tenant_id: str, document_id: str | None, actor: str, event: str,
          data: dict | None = None) -> None:
    from psycopg.types.json import Jsonb

    conn.execute(
        "INSERT INTO audit_log (tenant_id, document_id, actor, event, data) VALUES (%s, %s, %s, %s, %s)",
        (tenant_id, document_id, actor, event, Jsonb(data or {})),
    )


def init_db(admin_url: str, app_user: str = "ap_app", app_password: str = "ap_app") -> None:
    """Create the schema, the non-owner app role and its grants, and LangGraph's checkpoint tables."""
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(SCHEMA.read_text())
        exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (app_user,)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(sql.Identifier(app_user),
                                                                             sql.Literal(app_password)))
        dbname = conn.execute("SELECT current_database()").fetchone()[0]
        conn.execute(f'GRANT CONNECT ON DATABASE "{dbname}" TO "{app_user}"')
        conn.execute(f'GRANT USAGE ON SCHEMA public TO "{app_user}"')
        conn.execute(f'GRANT SELECT ON tenants, api_keys TO "{app_user}"')
        for t in ["users", "vendors", "purchase_orders", "receipts"]:
            conn.execute(f'GRANT SELECT ON {t} TO "{app_user}"')
        conn.execute(f'GRANT SELECT, INSERT, UPDATE ON documents, reviews TO "{app_user}"')
        conn.execute(f'GRANT SELECT, INSERT ON corrections, processed_invoices, audit_log TO "{app_user}"')
        conn.execute(f'GRANT SELECT, INSERT, UPDATE ON jobs, outbox TO "{app_user}"')
        conn.execute(f'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO "{app_user}"')
        conn.execute(f'GRANT EXECUTE ON FUNCTION app_tenant() TO "{app_user}"')

    # LangGraph checkpoint tables (keyed by thread_id = document id).
    from langgraph.checkpoint.postgres import PostgresSaver

    with PostgresSaver.from_conn_string(admin_url) as saver:
        saver.setup()
    with psycopg.connect(admin_url, autocommit=True) as conn:
        for (table,) in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'checkpoint%'"):
            conn.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON "{table}" TO "{app_user}"')
