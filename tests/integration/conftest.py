"""Integration tests run against a real Postgres with pgvector.

Set TEST_ADMIN_DATABASE_URL to a superuser URL on the server, for example
postgresql://postgres:postgres@localhost:5432/postgres. A database named apagent_test
is dropped and recreated. Tests are skipped if the server is unreachable.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.rows import dict_row

pytestmark = pytest.mark.integration

ADMIN = os.environ.get("TEST_ADMIN_DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/postgres")
DB = "apagent_test"


def _with_db(url: str, db: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(url)
    netloc = parts.netloc
    if user:
        host = netloc.split("@")[-1]
        netloc = f"{user}:{password}@{host}"
    return urlunsplit((parts.scheme, netloc, f"/{db}", parts.query, ""))


@pytest.fixture(scope="session")
def db_urls(tmp_path_factory):
    try:
        with psycopg.connect(ADMIN, autocommit=True, connect_timeout=3) as conn:
            conn.execute(f"DROP DATABASE IF EXISTS {DB} WITH (FORCE)")
            conn.execute(f"CREATE DATABASE {DB}")
    except psycopg.OperationalError as e:
        pytest.skip(f"Postgres not reachable at TEST_ADMIN_DATABASE_URL: {e}")
    admin = _with_db(ADMIN, DB)
    app_url = _with_db(ADMIN, DB, "ap_app", "ap_app")
    os.environ["ADMIN_DATABASE_URL"] = admin
    os.environ["DATABASE_URL"] = app_url
    os.environ["STORAGE_DIR"] = str(tmp_path_factory.mktemp("storage"))
    os.environ["CALIBRATION_FILE"] = str(tmp_path_factory.mktemp("cal") / "none.json")

    from app.db import core
    from app.settings import get_settings
    from app.storage import get_storage

    get_settings.cache_clear()
    core.pool.cache_clear()
    get_storage.cache_clear()
    core.init_db(admin)

    from scripts.seed import seed_all

    demo = tmp_path_factory.mktemp("demo")
    keys = seed_all(admin, demo_dir=demo, quiet=True)
    yield {"admin": admin, "app": app_url, "keys": keys, "demo": demo}
    core.pool().close()


@pytest.fixture
def admin_conn(db_urls):
    with psycopg.connect(db_urls["admin"], row_factory=dict_row, autocommit=True) as conn:
        yield conn


@pytest.fixture
def clean(db_urls, admin_conn):
    """Empty all workflow tables and restore the master data before each test."""
    import json

    from app.db.repo import load_master
    tables = ["outbox", "jobs", "reviews", "corrections", "audit_log", "processed_invoices", "documents"]
    admin_conn.execute("TRUNCATE " + ", ".join(tables) + " CASCADE")
    rows = admin_conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                              "AND tablename LIKE 'checkpoint%%' AND tablename <> 'checkpoint_migrations'").fetchall()
    for r in rows:
        admin_conn.execute(f'TRUNCATE "{r["tablename"]}"')
    for slug, tid in db_urls["keys"]["_tenants"].items():
        master = json.loads((db_urls["demo"] / f"demo_{slug}" / "master.json").read_text())
        load_master(admin_conn, tid, master)
    admin_conn.execute("UPDATE users SET approval_limit = 50000 WHERE role = 'approver'")
    return db_urls


@pytest.fixture
def ap_http():
    from app.mock_ap.server import app as ap_app
    c = TestClient(ap_app, base_url="http://ap")
    c.post("/_reset")
    return c


@pytest.fixture
def api(clean):
    from app.api.main import app
    return TestClient(app)


@pytest.fixture
def worker(clean, ap_http):
    from langgraph.checkpoint.memory import InMemorySaver

    from app.actions.ap_client import APClient
    from app.worker.main import Worker
    return Worker(checkpointer=InMemorySaver(), ap_client=APClient(client=ap_http), worker_id="test-worker")


def key(db_urls, who: str) -> dict:
    return {"X-API-Key": db_urls["keys"][who]}


def demo_pdfs(db_urls, slug="acme") -> list[Path]:
    return sorted((db_urls["demo"] / f"demo_{slug}" / "cases").glob("*.pdf"))


def upload(api, db_urls, path: Path, who="acme/uploader") -> dict:
    r = api.post("/documents", files={"file": (path.name, path.read_bytes(), "application/pdf")},
                 headers=key(db_urls, who))
    assert r.status_code == 202, r.text
    return r.json()
