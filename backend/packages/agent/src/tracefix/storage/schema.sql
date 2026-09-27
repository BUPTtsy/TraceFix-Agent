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
 intent_hash text NOT NULL, status text NOT NULL, receipt jsonb
);
CREATE TABLE IF NOT EXISTS memory_items (
 id text PRIMARY KEY, scope_id text NOT NULL, layer text NOT NULL,
 kind text NOT NULL, logical_key text NOT NULL, visibility text NOT NULL,
 status text NOT NULL, revision integer NOT NULL, source_revision text NOT NULL,
 content text NOT NULL, content_hash text NOT NULL, embedding vector(1024),
 search tsvector GENERATED ALWAYS AS (to_tsvector('simple', content)) STORED
);
CREATE INDEX IF NOT EXISTS memory_scope ON memory_items(scope_id, layer, status, revision);
CREATE INDEX IF NOT EXISTS memory_lexical ON memory_items USING gin(search);
CREATE TABLE IF NOT EXISTS approvals (
 id text PRIMARY KEY, run_id text NOT NULL, scope_id text NOT NULL,
 action_hash text NOT NULL, patch_hash text NOT NULL, expires_at double precision NOT NULL,
 decision text, consumed boolean NOT NULL DEFAULT false
);

