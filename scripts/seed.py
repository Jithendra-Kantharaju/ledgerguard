"""Seed two demo tenants with users, API keys and synthetic ERP data (vendors, POs, receipts).

Usage: python -m scripts.seed [--random-keys]

Without --random-keys the API keys are fixed, well-known development keys. Never use
them outside your laptop.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from datetime import date
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from app.config import load_config
from app.db.repo import load_master
from app.settings import ROOT, get_settings
from data.generate import build_split

TENANTS = [
    {"slug": "acme", "name": "Acme Distribution", "config": "customer_a.yaml", "seed": 7},
    {"slug": "borealis", "name": "Borealis Manufacturing", "config": "customer_b.yaml", "seed": 8},
]
USERS = [("approver", "50000"), ("reviewer", "0"), ("uploader", "0")]
DEMO_DIR = ROOT / "data" / "generated"


def ensure_demo_data(slug: str, config_file: str, seed: int, demo_dir: Path = DEMO_DIR) -> Path:
    split = f"demo_{slug}"
    master = demo_dir / split / "master.json"
    if not master.exists():
        # Demo invoices are dated relative to today so the age checks pass whenever you run this.
        build_split(demo_dir, split, seed, n_clean=8, n_scanned=2, n_per_error=1,
                    config=load_config(ROOT / "app" / "config" / config_file), reference_date=date.today())
    return master


def seed_all(admin_url: str, random_keys: bool = False, demo_dir: Path = DEMO_DIR, quiet: bool = False) -> dict:
    """Returns {"acme/approver": key, ...} and {"acme": tenant_id, ...} under "_tenants"."""
    keys: dict = {"_tenants": {}}
    with psycopg.connect(admin_url, row_factory=dict_row) as conn:
        for t in TENANTS:
            row = conn.execute(
                "INSERT INTO tenants (slug, name, config_file) VALUES (%s, %s, %s) ON CONFLICT (slug) "
                "DO UPDATE SET name = EXCLUDED.name, config_file = EXCLUDED.config_file RETURNING id",
                (t["slug"], t["name"], t["config"])).fetchone()
            tid = str(row["id"])
            keys["_tenants"][t["slug"]] = tid
            conn.execute("SELECT set_config('app.tenant_id', %s, true)", (tid,))
            for role, limit in USERS:
                email = f"{role}@{t['slug']}.example"
                u = conn.execute(
                    "INSERT INTO users (tenant_id, email, role, approval_limit) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (tenant_id, email) DO UPDATE SET role = EXCLUDED.role RETURNING id",
                    (tid, email, role, limit)).fetchone()
                key = secrets.token_urlsafe(24) if random_keys else f"dev-{t['slug']}-{role}"
                conn.execute("INSERT INTO api_keys (key_hash, tenant_id, user_id) VALUES (%s, %s, %s) "
                             "ON CONFLICT (key_hash) DO NOTHING",
                             (hashlib.sha256(key.encode()).hexdigest(), tid, u["id"]))
                keys[f"{t['slug']}/{role}"] = key
            master = json.loads(ensure_demo_data(t["slug"], t["config"], t["seed"], demo_dir).read_text())
            counts = load_master(conn, tid, master)
            if not quiet:
                print(f"tenant {t['slug']}: {counts}")
        conn.commit()
    return keys


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--random-keys", action="store_true")
    a = p.parse_args()
    keys = seed_all(get_settings().admin_database_url, a.random_keys)
    print("API keys (development only):" if not a.random_keys else "API keys (store these now; only hashes are kept):")
    for who, key in keys.items():
        if who != "_tenants":
            print(f"  {who:20s} {key}")
    print(f"Demo invoices to upload: {DEMO_DIR}/demo_acme/cases/*.pdf")


if __name__ == "__main__":
    main()
