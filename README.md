# AP Invoice Agent

An accounts-payable agent that reads supplier invoices, checks them against purchase orders
and goods receipts (three-way match), decides **approve / review / reject** with business
rules first and calibrated confidence second, pauses durably for a person when it should,
and sends approved invoices to the AP system **exactly once**, with read-back verification
and a full audit trail.

![architecture](docs/architecture.png)

## What is in here

| Area | Where | Notes |
|---|---|---|
| Parse | `app/pipeline/parse.py` | PyMuPDF text layer; Tesseract OCR for scans; Docling optional |
| Extract | `app/pipeline/extract.py` | Two independent passes. Offline rule extractors (no key), or Claude / OpenAI with strict JSON-schema output. Document text is treated as untrusted data |
| Validate, retrieve, match | `app/pipeline/validate.py`, `retrieve.py`, `match.py` | Arithmetic, currency, dates, duplicates, PO / vendor / receipt match with per-customer tolerances. A failure on an unverified reading (scan, or passes disagree) becomes *review*, not *reject* |
| Confidence + router | `app/pipeline/confidence.py`, `router.py` | Gates in order: hard rule → reject; amount/flags → review; calibrated confidence → review; else approve |
| Workflow | `app/worker/graph.py` | LangGraph with a Postgres checkpointer; `interrupt()` for human review; revalidation on approval; corrections rerun checks and never auto-execute |
| Queue | `app/worker/queue.py` | Postgres `FOR UPDATE SKIP LOCKED`, leases, retries with backoff, sweeper |
| Execution | `app/actions/dispatcher.py` | Transactional outbox, idempotency key `approve:<doc>:v<version>`, status query on timeout, read-back, dead-letter |
| API | `app/api/main.py` | FastAPI, API keys per user, roles (uploader / reviewer / approver / admin), approval limits |
| Data isolation | `app/db/schema.sql` | Row-level security on every tenant table; app connects as a non-owner role; audit log is insert-only |
| Mock AP system | `app/mock_ap/server.py` | Idempotent, with fault injection (outage, lost response after commit, wrong read-back) |
| Review UI | `ui/` | React + Vite: queue, PDF, fields (disagreements highlighted), reasons, approve / correct / reject, audit trail |
| Evaluation | `eval/`, `data/generate.py` | Synthetic data with known answers, frozen test set (checksums), baselines, calibration + threshold sweep |
| Customer config | `app/config/*.yaml` | Tolerances, limits, thresholds, AP status names. A new customer is a new YAML file |
| Tracing | `app/tracing.py` | OpenTelemetry → Langfuse (OTLP), with bank/tax-ID redaction |
| Docs | `docs/scoping.md`, `docs/runbook.md` | Customer scoping template and operations runbook |

Reference results from the frozen test split (425 invoices, offline extractors, Tesseract OCR)
are in [`docs/reference-results/test-report.md`](docs/reference-results/test-report.md):
0 wrong auto-approvals, 0 approvals with misread fields, 0 false rejections, all 225 seeded
errors stopped, 73% of payable invoices automated; a naive "PO exists → approve" baseline
auto-approves 256 bad invoices on the same data.

---

## Verify it locally — step by step

Do these in order. Each level adds one dependency, so when something fails you know what
caused it. Commands are for macOS / Linux; on Windows use `.venv\Scripts\python` instead of
`.venv/bin/python`, or use WSL.

### Level 0 — prerequisites

| Need | For | Check |
|---|---|---|
| Python 3.11 or 3.12 | everything | `python3 --version` |
| Docker (or a local Postgres 16 with pgvector) | levels 2–4 | `docker --version` |
| Tesseract (optional) | OCR of scanned invoices | `tesseract --version` (`brew install tesseract` / `apt install tesseract-ocr`) |
| Node 20+ (optional) | review UI | `node --version` |

### Level 1 — the logic, offline (no database, no API key) · ~5 minutes

```bash
unzip ap-agent.zip && cd ap-agent
python3 -m venv .venv
.venv/bin/python -m pip install -U pip
.venv/bin/python -m pip install -e ".[dev,ocr]"

# 1. Unit tests: parsing, extraction, rules, match, router gate order, redaction, mock AP idempotency
.venv/bin/python -m pytest tests/unit -q          # expect: 45 passed

# 2. Generate data with known answers (dev split + frozen test split)
make data        # or run the three commands under "data:" in the Makefile

# 3. Evaluate on the dev split, with the naive baseline for comparison
.venv/bin/python -m eval.run_eval --split data/generated/dev --baselines
```

What to look for in the report: **wrong auto-approvals = 0** and **approved with misread
fields = 0** for the pipeline, every row of "Seeded errors stopped" at n/n, and the naive
baseline approving many bad invoices. Without Tesseract, scanned invoices are unreadable and
all go to review (that is the correct, safe behaviour). Add `OCR_BACKEND=tesseract` in front
of the command to include OCR.

```bash
# 4. Fit confidence calibration on dev, then the final eval on the frozen test set
OCR_BACKEND=tesseract .venv/bin/python -m eval.calibrate --split data/generated/dev
OCR_BACKEND=tesseract .venv/bin/python -m eval.run_eval --split data/generated/test \
    --baselines --require-frozen --max-wrong-approvals 0
```

`eval/results/threshold_sweep.png` shows the automation-vs-error trade-off that set the
threshold. `--require-frozen` refuses to run if any test file changed since `make data`.

### Level 2 — the database guarantees (Postgres) · ~5 minutes

```bash
# Postgres 16 + pgvector in Docker
docker run -d --name apdb -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=apagent \
    -p 5432:5432 pgvector/pgvector:pg16

# Integration tests: create their own database (apagent_test), then test
TEST_ADMIN_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/postgres \
    .venv/bin/python -m pytest tests/integration -q          # expect: 22 passed
```

These prove the properties that matter in production:

- one tenant cannot read or write another tenant's rows, even with no `WHERE` clause (RLS)
- the app role cannot update or delete the audit log
- a reviewer cannot approve; a stale version is refused; an approval above the approver's limit is refused
- if the PO is closed after review started, approval does not execute (revalidation)
- a correction reruns every check and goes back to review, never straight to payment
- a lost response after the AP system saved the bill is recovered by a status query, with no duplicate
- an outage retries with backoff, then dead-letters and flags the document
- a wrong read-back is not marked executed
- two workers never get the same job; a crashed worker's job is requeued
- a review pause survives a worker restart (a new worker resumes from the Postgres checkpoint)
- shadow mode decides but sends nothing

### Level 3 — run the system and click through it · ~10 minutes

Four terminals, all in the project folder, Postgres from level 2 still running.

```bash
cp .env.example .env               # defaults point at localhost:5432

# terminal 1: schema + demo tenants, users, API keys, ERP data
.venv/bin/python -m scripts.init_db
.venv/bin/python -m scripts.seed   # prints the dev API keys, e.g. dev-acme-approver
.venv/bin/python -m uvicorn app.mock_ap.server:app --port 8001

# terminal 2
.venv/bin/python -m uvicorn app.api.main:app --port 8000

# terminal 3
.venv/bin/python -m app.worker.main

# terminal 4: upload the 19 demo invoices, then look at the results
.venv/bin/python -m scripts.upload_demo
curl -s -H "X-API-Key: dev-acme-approver" localhost:8000/stats
# expect roughly: {"awaiting_review": 6, "executed": 5, "rejected": 8}
curl -s -H "X-API-Key: dev-acme-approver" "localhost:8000/documents?status=awaiting_review"
```

Approve one of the "above auto-approve limit" invoices (copy its `id` and `version`):

```bash
DOC=<id>
# A reviewer cannot approve (403):
curl -s -X POST localhost:8000/documents/$DOC/review -H "X-API-Key: dev-acme-reviewer" \
     -H "Content-Type: application/json" -d '{"action":"approve","expected_version":1}'
# An approver can (202), and the worker revalidates before anything is sent:
curl -s -X POST localhost:8000/documents/$DOC/review -H "X-API-Key: dev-acme-approver" \
     -H "Content-Type: application/json" -d '{"action":"approve","expected_version":1}'
curl -s -H "X-API-Key: dev-acme-approver" localhost:8000/documents/$DOC
# status "executed", an outbox row "confirmed" with an AP bill id, and the audit trail:
# uploaded → analyzed → review_submitted → approved_by_reviewer → execution_queued → executed
curl -s localhost:8001/_state      # the bills the mock AP system holds
# Tenant isolation over HTTP: Borealis cannot see Acme's document (404)
curl -s -H "X-API-Key: dev-borealis-approver" localhost:8000/documents/$DOC
```

Try failure handling by hand: `curl -X POST localhost:8001/_faults -H "Content-Type: application/json" -d '{"lose_response_after_commit":1}'`,
then approve another document and look for `execution_recovered_by_status_query` in its audit trail.

Review UI (optional): `cd ui && npm install && npm run dev`, open http://localhost:5173 and
paste `dev-acme-approver` as the key. API docs: http://localhost:8000/docs.

### Level 4 — with a real model (API key) · minutes, a few cents

Only after levels 1–3 pass. Start small so cost stays visible.

```bash
export ANTHROPIC_API_KEY=sk-ant-...
# 20 dev invoices, Claude as the primary extractor, the rule extractor as the second pass
.venv/bin/python -m eval.run_eval --split data/generated/dev --extractor anthropic \
    --secondary rules_alt --limit 20 --out eval/results/dev-claude
```

Check that field accuracy is close to 100% on digital invoices, injection cases still go to
review, and look at the token counts in the report. Then compare models
(`--extractor anthropic:claude-sonnet-5-5`), and optionally the LLM-as-judge baseline
(`--llm-judge --limit 20`), which shows what happens when the model makes the decision.

To use the model in the running system, set in `.env` and restart the worker:

```
EXTRACTOR=anthropic
SECONDARY_EXTRACTOR=anthropic:claude-sonnet-5-5   # two independent model passes for agreement
ANTHROPIC_API_KEY=sk-ant-...
```

Re-run `eval.calibrate` after changing extractors: the confidence model is specific to them.

### Level 5 — the whole stack in Docker, then deploy

```bash
docker compose up --build
# API :8000, UI :5173, mock AP :8001, MinIO console :9001 (minioadmin / minioadmin)
.venv/bin/python -m scripts.upload_demo     # from the host, against the containers
```


## Application Demo

### Dashboard
![LedgerGuard Dashboard](docs/images/ledgerguard-dashboard.png)

### Human-in-the-Loop Review
![Invoice Review](docs/images/ledgerguard-review.png)

### Verified AP Execution
![AP Execution](docs/images/ledgerguard-execution.png)

## Local Verification Results

- 70 automated tests passed
- 19 synthetic invoices processed
- 5 mock AP bills successfully created
- 8 invoices rejected
- 6 invoices routed for human review
- 0 dead-lettered actions

These results use synthetic invoices and a mock AP API.
Real LLM extraction and QuickBooks sandbox validation
are separate verification stages.


Before deploying anywhere real:

1. `python -m scripts.seed --random-keys` (or create users yourself) — the `dev-*` keys are public.
2. Set a real `APP_DB_PASSWORD` when running `init_db`, and put secrets in a secret manager, not `.env`.
3. Replace the mock AP system with an adapter for the customer's AP API (`app/actions/ap_client.py`
   is the seam: `create_bill`, `find_by_key`, `get_bill`). Keep `SHADOW_MODE=true` for the first weeks.
4. Calibrate on the customer's own labelled invoices, then rerun the frozen-test eval.
5. Point `OTEL_EXPORTER_OTLP_ENDPOINT` at Langfuse (or any OTLP backend) and read `docs/runbook.md`.

---

## Honest limitations

- **Docker files were not run** in the environment this was built in (no Docker daemon there).
  Everything else was run: 67 tests against Postgres 16 + pgvector, the API, worker and mock AP
  as real processes, the UI build, and the evals above. If `docker compose` misbehaves, levels 1–3
  do not depend on it.
- **The data is synthetic.** Three invoice layouts, generated scans. Real invoices are messier;
  the rule extractors are there so everything runs offline, and they will not read arbitrary
  real invoices. Use an LLM extractor for real documents and measure it on real, labelled ones.
- **The calibration set is small** (144 dev invoices, few misreads). The threshold it picks is
  fragile; with the customer's threshold at 0.80 every scanned invoice currently goes to review.
- **Two passes over the same OCR text are not independent.** When OCR misreads a digit, both
  extractors agree on the wrong value. For scans, a second, different reading (another OCR
  engine or a vision model) is the real fix.
- **No real AP connector.** The mock behaves like real AP APIs in the ways that matter (idempotency
  keys, lost responses, read-back), but QuickBooks / NetSuite / SAP adapters are not included.
- **No SSO.** API keys per user; swap `current_user` in `app/api/main.py` for OIDC in a real deployment.

