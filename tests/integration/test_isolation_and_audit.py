import psycopg
import pytest

from tests.integration.conftest import demo_pdfs, key, upload

pytestmark = pytest.mark.integration


def test_tenant_cannot_see_other_tenants_documents(api, worker, db_urls):
    doc = upload(api, db_urls, demo_pdfs(db_urls)[0])
    assert api.get(f"/documents/{doc['id']}", headers=key(db_urls, "acme/approver")).status_code == 200
    assert api.get(f"/documents/{doc['id']}", headers=key(db_urls, "borealis/approver")).status_code == 404
    assert api.get("/documents", headers=key(db_urls, "borealis/approver")).json() == []


def test_rls_applies_even_without_a_where_clause(clean, db_urls):
    with psycopg.connect(db_urls["app"]) as conn:
        assert conn.execute("SELECT count(*) FROM vendors").fetchone()[0] == 0  # no tenant set: nothing visible
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (db_urls["keys"]["_tenants"]["acme"],))
        acme = conn.execute("SELECT count(*) FROM vendors").fetchone()[0]
        total = conn.execute("SELECT count(*) FROM vendors WHERE true").fetchone()[0]
        assert acme == total == 25


def test_app_role_cannot_rewrite_audit_log(api, worker, db_urls):
    upload(api, db_urls, demo_pdfs(db_urls)[0])
    with psycopg.connect(db_urls["app"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (db_urls["keys"]["_tenants"]["acme"],))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE audit_log SET event = 'nothing happened'")
    with psycopg.connect(db_urls["app"]) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM audit_log")


def test_app_role_cannot_write_into_another_tenant(clean, db_urls):
    t = db_urls["keys"]["_tenants"]
    with psycopg.connect(db_urls["app"]) as conn:
        conn.execute("SELECT set_config('app.tenant_id', %s, false)", (t["acme"],))
        with pytest.raises(psycopg.errors.InsufficientPrivilege):  # RLS WITH CHECK violation
            conn.execute("INSERT INTO audit_log (tenant_id, actor, event) VALUES (%s, 'x', 'y')", (t["borealis"],))
