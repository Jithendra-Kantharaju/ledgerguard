"""Look up the purchase order, receipts and vendor for an invoice.

Tenant isolation is the repository's job: the SQL repository runs inside a
transaction with app.tenant_id set, and Postgres row-level security filters
every query, so this module cannot see another tenant's records even by mistake.
"""

from __future__ import annotations

import difflib
import hashlib
import math
from typing import Protocol

from app.pipeline.normalize import norm_text
from app.schemas import Invoice, PurchaseOrder, Receipt, Retrieval, Vendor

_CORP_SUFFIXES = {"llc", "inc", "co", "ltd", "corp", "corporation", "company", "gmbh", "plc", "limited", "sa", "bv"}
EMBED_DIM = 256


def canonical_name(name: str) -> str:
    return " ".join(w for w in norm_text(name).split() if w not in _CORP_SUFFIXES)


def name_similarity(a: str | None, vendor: Vendor) -> float:
    if not a:
        return 0.0
    ca = canonical_name(a)
    return max(difflib.SequenceMatcher(None, ca, canonical_name(n)).ratio() for n in [vendor.name, *vendor.aliases])


def embed_name(name: str) -> list[float]:
    """Deterministic character-trigram hashing embedding, so vendor search works offline.

    Swap for a model embedding (and a wider vector column) if you need semantic matching.
    """
    s = f"  {canonical_name(name)}  "
    vec = [0.0] * EMBED_DIM
    for i in range(len(s) - 2):
        h = int.from_bytes(hashlib.blake2b(s[i:i + 3].encode(), digest_size=4).digest(), "big")
        vec[h % EMBED_DIM] += 1.0
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class Repository(Protocol):
    def get_po_by_number(self, po_number: str) -> PurchaseOrder | None: ...
    def get_vendor(self, vendor_id: str) -> Vendor | None: ...
    def search_vendors(self, name: str, limit: int = 5) -> list[Vendor]: ...
    def receipts_for_po(self, po_id: str) -> list[Receipt]: ...
    def find_processed_invoice(self, vendor_id: str, invoice_number: str,
                               exclude_document_id: str | None = None) -> str | None: ...


def retrieve(invoice: Invoice, repo: Repository, document_id: str | None = None) -> Retrieval:
    r = Retrieval()
    if invoice.vendor_name:
        candidates = repo.search_vendors(invoice.vendor_name)
        scored = sorted(((name_similarity(invoice.vendor_name, v), v) for v in candidates),
                        key=lambda x: x[0], reverse=True)
        if scored:
            r.vendor_similarity, r.vendor = scored[0]
    if invoice.po_number:
        r.po = repo.get_po_by_number(invoice.po_number.strip())
        if r.po:
            r.po_vendor = repo.get_vendor(r.po.vendor_id)
            r.receipts = repo.receipts_for_po(r.po.id)
    if invoice.invoice_number:
        vendor_ids = {v.id for v in (r.vendor, r.po_vendor) if v}
        for vid in vendor_ids:
            dup = repo.find_processed_invoice(vid, invoice.invoice_number.strip(), document_id)
            if dup:
                r.duplicate_of = dup
                break
    return r


class InMemoryRepository:
    """Used by the eval harness and unit tests; same interface as the SQL repository."""

    def __init__(self, vendors: list[Vendor], pos: list[PurchaseOrder], receipts: list[Receipt],
                 processed: list[dict] | None = None):
        self.vendors = {v.id: v for v in vendors}
        self.pos = {p.po_number: p for p in pos}
        self.receipts = receipts
        self.processed = {(p["vendor_id"], p["invoice_number"]): p["document_id"] for p in (processed or [])}

    @classmethod
    def from_master(cls, master: dict) -> InMemoryRepository:
        return cls(
            vendors=[Vendor(id=v["id"], name=v["name"], aliases=v.get("aliases", [])) for v in master["vendors"]],
            pos=[PurchaseOrder(**p) for p in master["purchase_orders"]],
            receipts=[Receipt(**r) for r in master["receipts"]],
            processed=master.get("processed_invoices", []),
        )

    def get_po_by_number(self, po_number: str) -> PurchaseOrder | None:
        return self.pos.get(po_number)

    def get_vendor(self, vendor_id: str) -> Vendor | None:
        return self.vendors.get(vendor_id)

    def search_vendors(self, name: str, limit: int = 5) -> list[Vendor]:
        return sorted(self.vendors.values(), key=lambda v: name_similarity(name, v), reverse=True)[:limit]

    def receipts_for_po(self, po_id: str) -> list[Receipt]:
        return [r for r in self.receipts if r.po_id == po_id]

    def find_processed_invoice(self, vendor_id: str, invoice_number: str,
                               exclude_document_id: str | None = None) -> str | None:
        doc = self.processed.get((vendor_id, invoice_number))
        return doc if doc and doc != exclude_document_id else None
