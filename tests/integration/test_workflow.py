"""End to end through the API, the worker graph, the outbox and the mock AP system."""

import pytest

from tests.integration.conftest import demo_pdfs, key, upload

pytestmark = pytest.mark.integration


def _by_name(db_urls, part):
    return next(p for p in demo_pdfs(db_urls) if part in p.name)


def _doc(api, db_urls, doc_id, who="acme/approver"):
    r = api.get(f"/documents/{doc_id}", headers=key(db_urls, who))
    assert r.status_code == 200, r.text
    return r.json()


def test_full_batch(api, worker, ap_http, db_urls):
    ids = {p.name: upload(api, db_urls, p)["id"] for p in demo_pdfs(db_urls)}
    worker.drain()
    status = {name: _doc(api, db_urls, i)["status"] for name, i in ids.items()}
    for name, s in status.items():
        if "-error-" in name and "prompt_injection" not in name:
            assert s == "rejected", (name, s)
        if "prompt_injection" in name or "-scanned-" in name:
            assert s == "awaiting_review", (name, s)
        assert s != "failed", name
    executed = [n for n, s in status.items() if s == "executed"]
    assert executed and all("-clean-" in n for n in executed)
    assert len(ap_http.get("/_state").json()["bills"]) == len(executed)


def test_same_file_twice_is_one_document(api, worker, db_urls):
    p = demo_pdfs(db_urls)[0]
    a, b = upload(api, db_urls, p), upload(api, db_urls, p)
    assert a["id"] == b["id"] and b["duplicate_upload"] is True


def _awaiting_big(api, worker, db_urls):
    """Upload clean invoices and return one that went to review for being above the limit."""
    for p in demo_pdfs(db_urls):
        if "-clean-" in p.name:
            upload(api, db_urls, p)
    worker.drain()
    docs = api.get("/documents?status=awaiting_review", headers=key(db_urls, "acme/approver")).json()
    big = [d for d in docs if any("auto-approve limit" in r for r in d["reasons"])]
    assert big, "demo data should contain an invoice above the auto-approve limit"
    return big[0]


def test_approve_executes_exactly_once(api, worker, ap_http, db_urls):
    d = _awaiting_big(api, worker, db_urls)
    before = len(ap_http.get("/_state").json()["bills"])
    r = api.post(f"/documents/{d['id']}/review", json={"action": "approve", "expected_version": d["version"]},
                 headers=key(db_urls, "acme/approver"))
    assert r.status_code == 202
    worker.drain()
    doc = _doc(api, db_urls, d["id"])
    assert doc["status"] == "executed", doc
    assert doc["reviews"][-1]["outcome"] == "accepted"
    assert doc["outbox"][0]["idempotency_key"] == f"approve:{d['id']}:v{d['version']}"
    assert len(ap_http.get("/_state").json()["bills"]) == before + 1
    # A second approval of the same version is refused: the document is no longer awaiting review.
    r = api.post(f"/documents/{d['id']}/review", json={"action": "approve", "expected_version": d["version"]},
                 headers=key(db_urls, "acme/approver"))
    assert r.status_code == 409


def test_reviewer_cannot_approve_and_stale_version_is_refused(api, worker, db_urls):
    d = _awaiting_big(api, worker, db_urls)
    r = api.post(f"/documents/{d['id']}/review", json={"action": "approve", "expected_version": d["version"]},
                 headers=key(db_urls, "acme/reviewer"))
    assert r.status_code == 403
    r = api.post(f"/documents/{d['id']}/review", json={"action": "approve", "expected_version": d["version"] + 7},
                 headers=key(db_urls, "acme/approver"))
    assert r.status_code == 409


def test_correction_reruns_checks_and_never_auto_executes(api, worker, ap_http, db_urls):
    d = _awaiting_big(api, worker, db_urls)
    bills = len(ap_http.get("/_state").json()["bills"])
    r = api.post(f"/documents/{d['id']}/review",
                 json={"action": "correct", "expected_version": d["version"], "fields": {"notes": "checked"}},
                 headers=key(db_urls, "acme/reviewer"))
    assert r.status_code == 202
    worker.drain()
    doc = _doc(api, db_urls, d["id"])
    assert doc["status"] == "awaiting_review"
    assert doc["version"] == d["version"] + 1
    assert doc["analysis"]["machine_extraction"]["model"] != "human"
    assert len(ap_http.get("/_state").json()["bills"]) == bills
    # A correction that breaks the arithmetic is caught when checks rerun.
    r = api.post(f"/documents/{d['id']}/review",
                 json={"action": "correct", "expected_version": doc["version"], "fields": {"total": "1.00"}},
                 headers=key(db_urls, "acme/reviewer"))
    worker.drain()
    doc = _doc(api, db_urls, d["id"])
    assert "total_mismatch" in doc["reasons"] and doc["status"] == "awaiting_review"


def test_revalidation_catches_po_closed_after_review_started(api, worker, ap_http, db_urls, admin_conn):
    d = _awaiting_big(api, worker, db_urls)
    po_number = _doc(api, db_urls, d["id"])["invoice"]["po_number"]
    admin_conn.execute("UPDATE purchase_orders SET status = 'closed', version = version + 1 WHERE po_number = %s",
                       (po_number,))
    bills = len(ap_http.get("/_state").json()["bills"])
    api.post(f"/documents/{d['id']}/review", json={"action": "approve", "expected_version": d["version"]},
             headers=key(db_urls, "acme/approver"))
    worker.drain()
    doc = _doc(api, db_urls, d["id"])
    assert doc["status"] == "awaiting_review"
    assert doc["version"] == d["version"] + 1
    assert "revalidation failed" in doc["reviews"][-1]["outcome"] and "po_closed" in doc["reviews"][-1]["outcome"]
    assert len(ap_http.get("/_state").json()["bills"]) == bills


def test_approval_limit_enforced_at_execution_time(api, worker, db_urls, admin_conn):
    d = _awaiting_big(api, worker, db_urls)
    admin_conn.execute("UPDATE users SET approval_limit = 100 WHERE role = 'approver'")
    api.post(f"/documents/{d['id']}/review", json={"action": "approve", "expected_version": d["version"]},
             headers=key(db_urls, "acme/approver"))
    worker.drain()
    doc = _doc(api, db_urls, d["id"])
    assert doc["status"] == "awaiting_review"
    assert "exceeds approver limit" in doc["reviews"][-1]["outcome"]


def test_reject_by_reviewer(api, worker, db_urls):
    d = _awaiting_big(api, worker, db_urls)
    api.post(f"/documents/{d['id']}/review", json={"action": "reject", "expected_version": d["version"],
                                                   "comment": "not ours"}, headers=key(db_urls, "acme/reviewer"))
    worker.drain()
    doc = _doc(api, db_urls, d["id"])
    assert doc["status"] == "rejected"
    assert any(e["event"] == "rejected_by_reviewer" for e in doc["audit"])


def test_upload_requires_valid_key_and_pdf(api, db_urls):
    assert api.post("/documents", files={"file": ("a.pdf", b"%PDF-1.4", "application/pdf")}).status_code == 401
    r = api.post("/documents", files={"file": ("a.txt", b"hello", "text/plain")}, headers=key(db_urls, "acme/uploader"))
    assert r.status_code == 415


def test_shadow_mode_sends_nothing(api, worker, ap_http, db_urls):
    worker.deps.shadow_mode = True
    p = next(p for p in demo_pdfs(db_urls) if "-clean-ok-001" in p.name)
    doc = upload(api, db_urls, p)
    worker.drain()
    d = _doc(api, db_urls, doc["id"])
    assert d["status"] == "shadow_approved"
    assert any(e["event"] == "shadow_would_execute" for e in d["audit"])
    assert ap_http.get("/_state").json()["bills"] == []
