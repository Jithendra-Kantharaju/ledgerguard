"""Upload every PDF in a folder to the running API.

Usage: python -m scripts.upload_demo [--dir data/generated/demo_acme/cases] [--key dev-acme-uploader]
"""

import argparse
from pathlib import Path

import httpx

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--dir", default="data/generated/demo_acme/cases")
    p.add_argument("--key", default="dev-acme-uploader")
    p.add_argument("--api", default="http://localhost:8000")
    a = p.parse_args()
    files = sorted(Path(a.dir).glob("*.pdf"))
    with httpx.Client(base_url=a.api, headers={"X-API-Key": a.key}, timeout=30) as c:
        for f in files:
            r = c.post("/documents", files={"file": (f.name, f.read_bytes(), "application/pdf")})
            r.raise_for_status()
            print(f"{f.name:45s} -> {r.json()['id']}  {r.json()['status']}")
    print(f"uploaded {len(files)} files; run the worker, then GET /documents")
