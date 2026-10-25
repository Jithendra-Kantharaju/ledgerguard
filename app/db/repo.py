"""SQL implementation of the pipeline's Repository, plus data loading used by the seed script.

Every method runs on a connection inside a tenant transaction (app.db.core.tx(tenant_id)),
so row-level security decides what is visible.
"""

from __future__ import annotations

import psycopg
from psycopg.types.json import Jsonb

from app.pipeline.retrieve import embed_name
from app.schemas import POLine, PurchaseOrder, Receipt, ReceiptLine, Vendor


def vec_literal(v: list[float]) -> str:
    return "[" + ",".join(f"{x:.6f}" for x in v) + "]"


def _vendor(row: dict) -> Vendor:
    return Vendor(id=str(row["id"]), name=row["name"], aliases=row["aliases"] or [])


def _po(row: dict) -> PurchaseOrder:
    return PurchaseOrder(id=str(row["id"]), po_number=row["po_number"], vendor_id=str(row["vendor_id"]),
                         currency=row["currency"], status=row["status"],
                         lines=[POLine(**ln) for ln in row["lines"]], version=row["version"])


class SqlRepository:
    def __init__(self, conn: psycopg.Connection):
        self.conn = conn

    def get_po_by_number(self, po_number: str) -> PurchaseOrder | None:
        row = self.conn.execute("SELECT * FROM purchase_orders WHERE po_number = %s", (po_number,)).fetchone()
        return _po(row) if row else None

    def get_vendor(self, vendor_id: str) -> Vendor | None:
        row = self.conn.execute("SELECT id, name, aliases FROM vendors WHERE id = %s", (vendor_id,)).fetchone()
        return _vendor(row) if row else None

    def search_vendors(self, name: str, limit: int = 5) -> list[Vendor]:
        rows = self.conn.execute(
            "SELECT id, name, aliases FROM vendors ORDER BY name_embedding <=> %s::vector LIMIT %s",
            (vec_literal(embed_name(name)), limit)).fetchall()
        return [_vendor(r) for r in rows]

    def receipts_for_po(self, po_id: str) -> list[Receipt]:
        rows = self.conn.execute("SELECT id, po_id, lines FROM receipts WHERE po_id = %s", (po_id,)).fetchall()
        return [Receipt(id=str(r["id"]), po_id=str(r["po_id"]), lines=[ReceiptLine(**ln) for ln in r["lines"]])
                for r in rows]

    def find_processed_invoice(self, vendor_id: str, invoice_number: str,
                               exclude_document_id: str | None = None) -> str | None:
        row = self.conn.execute(
            "SELECT coalesce(document_id::text, external_ref, 'history') AS ref, document_id FROM processed_invoices "
            "WHERE vendor_id = %s AND invoice_number = %s", (vendor_id, invoice_number)).fetchone()
        if not row:
            return None
        if exclude_document_id and row["document_id"] and str(row["document_id"]) == str(exclude_document_id):
            return None
        return row["ref"]


def load_master(conn: psycopg.Connection, tenant_id: str, master: dict) -> dict[str, int]:
    """Load vendors, POs, receipts and paid-invoice history from a generated master.json.

    Idempotent: rows are keyed by their external id and skipped if present.
    """
    vendor_ids: dict[str, str] = {}
    for v in master["vendors"]:
        row = conn.execute(
            "INSERT INTO vendors (tenant_id, external_id, name, aliases, name_embedding) "
            "VALUES (%s, %s, %s, %s, %s::vector) ON CONFLICT (tenant_id, external_id) "
            "DO UPDATE SET name = EXCLUDED.name RETURNING id",
            (tenant_id, v["id"], v["name"], Jsonb(v.get("aliases", [])), vec_literal(embed_name(v["name"])))
        ).fetchone()
        vendor_ids[v["id"]] = str(row["id"])
    po_ids: dict[str, str] = {}
    for p in master["purchase_orders"]:
        row = conn.execute(
            "INSERT INTO purchase_orders (tenant_id, external_id, po_number, vendor_id, currency, status, lines) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, po_number) "
            "DO UPDATE SET status = EXCLUDED.status RETURNING id",
            (tenant_id, p["id"], p["po_number"], vendor_ids[p["vendor_id"]], p["currency"], p["status"],
             Jsonb(p["lines"]))).fetchone()
        po_ids[p["id"]] = str(row["id"])
    n_receipts = 0
    for r in master["receipts"]:
        exists = conn.execute("SELECT 1 FROM receipts WHERE tenant_id = %s AND external_id = %s",
                              (tenant_id, r["id"])).fetchone()
        if not exists and r["po_id"] in po_ids:
            conn.execute("INSERT INTO receipts (tenant_id, external_id, po_id, lines) VALUES (%s, %s, %s, %s)",
                         (tenant_id, r["id"], po_ids[r["po_id"]], Jsonb(r["lines"])))
            n_receipts += 1
    for h in master.get("processed_invoices", []):
        conn.execute(
            "INSERT INTO processed_invoices (tenant_id, vendor_id, invoice_number, external_ref) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (tenant_id, vendor_ids[h["vendor_id"]], h["invoice_number"], h["document_id"]))
    return {"vendors": len(vendor_ids), "purchase_orders": len(po_ids), "receipts": n_receipts,
            "history": len(master.get("processed_invoices", []))}
