CREATE EXTENSION IF NOT EXISTS vector;
CREATE TABLE IF NOT EXISTS projects (
 id text PRIMARY KEY, parent_id text, root_binding text NOT NULL,
 memory_revision integer NOT NULL, access_epoch integer NOT NULL, config jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
 id text PRIMARY KEY, scope_id text NOT NULL, state jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
 run_id text NOT NULL, seq bigint NOT NULL, payload jsonb NOT NULL,
 PRIMARY KEY (run_id, seq)
);
CREATE TABLE IF NOT EXISTS operations (
 id text PRIMARY KEY, run_id text NOT NULL, scope_id text NOT NULL,
 intent_hash text NOT NULL, status text NOT NULL, receipt jsonb,
 resources jsonb, owner_token text, epoch text, resolution jsonb
);
ALTER TABLE operations ADD COLUMN IF NOT EXISTS resources jsonb;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS owner_token text;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS epoch text;
ALTER TABLE operations ADD COLUMN IF NOT EXISTS resolution jsonb;
UPDATE operations SET status='UNKNOWN', resources='["*"]'::jsonb
 WHERE status='STARTED' AND owner_token IS NULL;
UPDATE operations SET resources='["*"]'::jsonb
 WHERE status <> 'DONE' AND (resources IS NULL OR resources='[]'::jsonb);
CREATE INDEX IF NOT EXISTS operations_pending_scope ON operations(scope_id)
 WHERE status <> 'DONE';
CREATE TABLE IF NOT EXISTS memory_items (
 id text PRIMARY KEY, scope_id text NOT NULL, layer text NOT NULL,
 kind text NOT NULL, logical_key text NOT NULL, visibility text NOT NULL,
 status text NOT NULL, revision integer NOT NULL, source_revision text NOT NULL,
 content text NOT NULL, content_hash text NOT NULL, embedding vector(1024),
 search tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
);
CREATE INDEX IF NOT EXISTS memory_scope ON memory_items(scope_id, layer, status, revision);
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS source_run_id text;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS expires_at double precision;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS applicability jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS revoked_reason text;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS revoked_at double precision;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS evidence_refs jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS patch_hash text;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS environment_digest text;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS test_spec_hash text;
ALTER TABLE memory_items ADD COLUMN IF NOT EXISTS verification_refs jsonb NOT NULL DEFAULT '[]'::jsonb;
CREATE INDEX IF NOT EXISTS memory_lexical ON memory_items USING gin(search);
CREATE TABLE IF NOT EXISTS approvals (
 id text PRIMARY KEY, run_id text NOT NULL, scope_id text NOT NULL,
 action_hash text NOT NULL, patch_hash text, expires_at double precision NOT NULL,
 decision text, consumed boolean NOT NULL DEFAULT false
);
ALTER TABLE approvals ALTER COLUMN patch_hash DROP NOT NULL;
CREATE TABLE IF NOT EXISTS harness_jobs (
 id text PRIMARY KEY, job_id text NOT NULL UNIQUE, scope_id text NOT NULL,
 revision integer NOT NULL CHECK(revision >= 0),
 status text NOT NULL CHECK(status IN ('CREATED','RUNNING','STOPPING','STOPPED','COMPLETED','BLOCKED','FAILED')),
 state jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS harness_work_items (
 id text PRIMARY KEY, job_id text NOT NULL REFERENCES harness_jobs(id), scope_id text NOT NULL,
 revision integer NOT NULL CHECK(revision >= 0),
 status text NOT NULL CHECK(status IN ('PENDING','READY','LEASED','CANDIDATE','VERIFIED','BLOCKED')),
 lease_owner text, lease_epoch bigint NOT NULL DEFAULT 0, state jsonb NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS harness_one_claimed_work_item ON harness_work_items(job_id)
 WHERE status IN ('LEASED','CANDIDATE');
CREATE TABLE IF NOT EXISTS harness_sessions (
 id text PRIMARY KEY, job_id text NOT NULL REFERENCES harness_jobs(id), scope_id text NOT NULL,
 revision integer NOT NULL CHECK(revision >= 0),
 status text NOT NULL CHECK(status IN ('CREATED','RUNNING','STOPPING','STOPPED','COMPLETED','BLOCKED','FAILED')),
 kind text NOT NULL CHECK(kind IN ('initializer','coder')), harness_task_id text REFERENCES harness_work_items(id),
 state jsonb NOT NULL,
 CHECK((kind='initializer' AND harness_task_id IS NULL) OR (kind='coder' AND harness_task_id IS NOT NULL))
);
CREATE UNIQUE INDEX IF NOT EXISTS harness_one_active_session ON harness_sessions(job_id)
 WHERE status NOT IN ('COMPLETED','FAILED');
CREATE TABLE IF NOT EXISTS sdk_executions (
 id text PRIMARY KEY, job_id text NOT NULL REFERENCES harness_jobs(id), scope_id text NOT NULL,
 revision integer NOT NULL CHECK(revision >= 0),
 status text NOT NULL CHECK(status IN ('PREPARED','STARTING','RUNNING','SUCCEEDED','INTERRUPTING','STOPPED','FAILED','UNKNOWN')),
 session_id text NOT NULL REFERENCES harness_sessions(id), node_kind text NOT NULL,
 logical_invocation_id text NOT NULL, owner_epoch bigint NOT NULL, state jsonb NOT NULL,
 UNIQUE(session_id,node_kind,logical_invocation_id)
);
CREATE TABLE IF NOT EXISTS harness_outbox (
 job_id text NOT NULL REFERENCES harness_jobs(id), scope_id text NOT NULL, seq bigint NOT NULL,
 event_id text NOT NULL UNIQUE, owner_epoch bigint NOT NULL, payload jsonb NOT NULL,
 delivered boolean NOT NULL DEFAULT false, PRIMARY KEY(job_id,seq)
);
CREATE TABLE IF NOT EXISTS harness_projection_offsets (
 consumer_id text NOT NULL, job_id text NOT NULL REFERENCES harness_jobs(id),
 last_seq bigint NOT NULL, state jsonb NOT NULL, PRIMARY KEY(consumer_id,job_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS sdk_one_execution_per_invocation ON sdk_executions
 (session_id, (state->>'invocation_no'));

