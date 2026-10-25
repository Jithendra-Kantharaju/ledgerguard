-- AP agent schema. Applied by scripts/init_db.py as the database owner.
-- The application connects as role ap_app, which owns nothing, so row-level
-- security is enforced on every tenant table (owners bypass RLS; ap_app cannot).

CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- Current tenant for this transaction, set with set_config('app.tenant_id', ..., true).
CREATE OR REPLACE FUNCTION app_tenant() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$;

CREATE TABLE IF NOT EXISTS tenants (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    slug        text UNIQUE NOT NULL,
    name        text NOT NULL,
    config_file text NOT NULL            -- e.g. customer_a.yaml
);

CREATE TABLE IF NOT EXISTS users (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id),
    email          text NOT NULL,
    role           text NOT NULL CHECK (role IN ('reviewer', 'approver', 'admin', 'uploader')),
    approval_limit numeric(14,2) NOT NULL DEFAULT 0,
    active         boolean NOT NULL DEFAULT true,
    UNIQUE (tenant_id, email)
);

-- API keys are looked up before the tenant is known, so this table is not tenant-scoped.
-- Only the SHA-256 of the key is stored.
CREATE TABLE IF NOT EXISTS api_keys (
    key_hash   text PRIMARY KEY,
    tenant_id  uuid NOT NULL REFERENCES tenants(id),
    user_id    uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS vendors (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      uuid NOT NULL REFERENCES tenants(id),
    external_id    text NOT NULL,
    name           text NOT NULL,
    aliases        jsonb NOT NULL DEFAULT '[]',
    name_embedding vector(256),
    UNIQUE (tenant_id, external_id)
);
CREATE INDEX IF NOT EXISTS vendors_embedding_idx ON vendors USING hnsw (name_embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS purchase_orders (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id),
    external_id text NOT NULL,
    po_number   text NOT NULL,
    vendor_id   uuid NOT NULL REFERENCES vendors(id),
    currency    text NOT NULL,
    status      text NOT NULL,
    lines       jsonb NOT NULL,
    version     int NOT NULL DEFAULT 1,
    UNIQUE (tenant_id, po_number)
);

CREATE TABLE IF NOT EXISTS receipts (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id),
    external_id text NOT NULL,
    po_id       uuid NOT NULL REFERENCES purchase_orders(id),
    lines       jsonb NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id),
    sha256      text NOT NULL,
    filename    text NOT NULL,
    storage_key text NOT NULL,
    status      text NOT NULL,           -- queued, processing, awaiting_review, executing, executed, rejected, failed
    version     int NOT NULL DEFAULT 1,  -- bumped on every change a reviewer must have seen
    analysis    jsonb,
    decision    jsonb,
    invoice     jsonb,                   -- current invoice fields (extracted, or corrected by a reviewer)
    error       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, sha256)           -- uploading the same file twice returns the same document
);
CREATE INDEX IF NOT EXISTS documents_status_idx ON documents (tenant_id, status, created_at);

-- Reviewer corrections are stored next to, never on top of, the extracted values.
CREATE TABLE IF NOT EXISTS corrections (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id   uuid NOT NULL REFERENCES tenants(id),
    document_id uuid NOT NULL REFERENCES documents(id),
    user_id     uuid NOT NULL REFERENCES users(id),
    fields      jsonb NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS reviews (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id        uuid NOT NULL REFERENCES tenants(id),
    document_id      uuid NOT NULL REFERENCES documents(id),
    user_id          uuid NOT NULL REFERENCES users(id),
    action           text NOT NULL CHECK (action IN ('approve', 'reject', 'correct')),
    document_version int NOT NULL,
    comment          text,
    outcome          text,               -- filled by the worker: accepted, or why it was not
    created_at       timestamptz NOT NULL DEFAULT now()
);

-- One row per invoice that was sent to AP. The unique key is the duplicate-payment guard.
CREATE TABLE IF NOT EXISTS processed_invoices (
    tenant_id      uuid NOT NULL REFERENCES tenants(id),
    vendor_id      uuid NOT NULL REFERENCES vendors(id),
    invoice_number text NOT NULL,
    document_id    uuid,                 -- null for history imported from the AP system
    external_ref   text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, vendor_id, invoice_number)
);

-- Insert-only audit trail (ap_app has no UPDATE or DELETE on it).
CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    tenant_id   uuid NOT NULL REFERENCES tenants(id),
    document_id uuid,
    actor       text NOT NULL,           -- user id, "worker:<id>", "dispatcher", "api"
    event       text NOT NULL,
    data        jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS audit_doc_idx ON audit_log (document_id, id);

-- Work queue. Not tenant-scoped: workers serve every tenant and set app.tenant_id per job.
CREATE TABLE IF NOT EXISTS jobs (
    id           bigserial PRIMARY KEY,
    tenant_id    uuid NOT NULL REFERENCES tenants(id),
    document_id  uuid NOT NULL REFERENCES documents(id),
    kind         text NOT NULL CHECK (kind IN ('analyze', 'resume')),
    payload      jsonb NOT NULL DEFAULT '{}',
    status       text NOT NULL DEFAULT 'queued',   -- queued, running, done, failed
    attempts     int NOT NULL DEFAULT 0,
    max_attempts int NOT NULL DEFAULT 3,
    run_after    timestamptz NOT NULL DEFAULT now(),
    locked_by    text,
    lease_until  timestamptz,
    last_error   text,
    created_at   timestamptz NOT NULL DEFAULT now(),
    updated_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS jobs_ready_idx ON jobs (status, run_after) WHERE status = 'queued';

-- Transactional outbox: written in the same transaction as the approval, sent later.
CREATE TABLE IF NOT EXISTS outbox (
    id              bigserial PRIMARY KEY,
    tenant_id       uuid NOT NULL REFERENCES tenants(id),
    document_id     uuid NOT NULL REFERENCES documents(id),
    idempotency_key text UNIQUE NOT NULL,   -- approve:<document id>:v<version>
    action          text NOT NULL,
    payload         jsonb NOT NULL,
    status          text NOT NULL DEFAULT 'pending',  -- pending, confirmed, dead
    attempts        int NOT NULL DEFAULT 0,
    next_attempt_at timestamptz NOT NULL DEFAULT now(),
    ap_reference    text,
    last_error      text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS outbox_ready_idx ON outbox (status, next_attempt_at) WHERE status = 'pending';

-- Row-level security on every tenant table.
DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY['users', 'vendors', 'purchase_orders', 'receipts', 'documents', 'corrections',
                             'reviews', 'processed_invoices', 'audit_log']
    LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS tenant_isolation ON %I', t);
        EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = app_tenant()) '
                       'WITH CHECK (tenant_id = app_tenant())', t);
    END LOOP;
END $$;
