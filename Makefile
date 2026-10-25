# Common tasks. Run `make help`.
PY ?= .venv/bin/python
PGADMIN ?= postgresql://postgres:postgres@localhost:5432/postgres

.PHONY: help install lint test-unit test-integration data eval eval-test calibrate db seed api worker mock-ap ui up down

help:
	@grep -E '^[a-z-]+:.*?## ' $(MAKEFILE_LIST) | awk -F':.*?## ' '{printf "  %-18s %s\n", $$1, $$2}'

install: ## create .venv and install everything (needs python 3.11+)
	python3 -m venv .venv && $(PY) -m pip install -U pip && $(PY) -m pip install -e ".[dev,ocr]"

lint: ## ruff
	$(PY) -m ruff check app eval data scripts tests

test-unit: ## unit tests (no database, no network, no API key)
	$(PY) -m pytest tests/unit -q

test-integration: ## integration tests (needs Postgres with pgvector; see PGADMIN)
	TEST_ADMIN_DATABASE_URL=$(PGADMIN) $(PY) -m pytest tests/integration -q

data: ## generate dev and frozen test splits
	$(PY) -m data.generate --out data/generated --split dev --seed 2 --n-clean 60 --n-scanned 30 --n-per-error 6
	$(PY) -m data.generate --out data/generated --split test --seed 1
	$(PY) -m eval.freeze data/generated/test

calibrate: ## fit confidence model + threshold on the dev split
	$(PY) -m eval.calibrate --split data/generated/dev

eval: ## quick eval on the dev split with the naive baseline
	$(PY) -m eval.run_eval --split data/generated/dev --baselines

eval-test: ## final eval on the frozen test split (fails on any unsafe auto-approval)
	$(PY) -m eval.run_eval --split data/generated/test --baselines --require-frozen --max-wrong-approvals 0

db: ## create schema, RLS, app role (uses ADMIN_DATABASE_URL)
	$(PY) -m scripts.init_db

seed: ## demo tenants, users, API keys, ERP data
	$(PY) -m scripts.seed

api: ## run the API on :8000
	$(PY) -m uvicorn app.api.main:app --reload --port 8000

worker: ## run the worker
	$(PY) -m app.worker.main

mock-ap: ## run the mock AP system on :8001
	$(PY) -m uvicorn app.mock_ap.server:app --port 8001

ui: ## run the review UI on :5173
	cd ui && npm install && npm run dev

up: ## whole stack in Docker
	docker compose up --build

down:
	docker compose down -v
