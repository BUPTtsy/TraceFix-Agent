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
 action_hash text NOT NULL, patch_hash text NOT NULL, expires_at double precision NOT NULL,
 decision text, consumed boolean NOT NULL DEFAULT false
);

