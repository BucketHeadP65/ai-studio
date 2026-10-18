-- 10-extensions.sql: the local Postgres initdb hook (engram, ADRs 0006/0007).
--
-- Creates the four Postgres extensions the wiki's pg_textsearch backend depends on,
-- so the local container offers the same surface as the managed Postgres target:
--   * pg_textsearch — BM25 lexical search access method (`<@>` / `to_bm25query`),
--                     the counterpart of ParadeDB's pg_search (ADR-0006).
--   * age           — Apache AGE, in-Postgres openCypher graph engine (ADR-0007).
--   * vector        — pgvector, the halfvec(1024) + HNSW semantic leg.
--   * ltree         — hierarchical label paths (section / locator paths).
--
-- Placement & lifecycle: the image/compose (sibling tasks 2.6 / Slice-B compose)
-- mount this file into /docker-entrypoint-initdb.d/, so the official Postgres
-- entrypoint runs it ONCE, at FIRST cluster init only, against the database
-- POSTGRES_DB names (`knowledge_bases` in docker-compose.yml). It never switches
-- database, so it follows whatever POSTGRES_DB is set to.
-- It does NOT re-run on subsequent starts of an already-initialised data volume.
-- Every statement is idempotent (IF NOT EXISTS) so a manual re-run is harmless.
--
-- IMPORTANT — Apache AGE additionally requires the server-level GUC
--   shared_preload_libraries = 'age'
-- to be set (a static parameter needing a restart). That is the server's job,
-- handled in the sibling Dockerfile (task 2.6), NOT here: an initdb hook cannot
-- set a shared_preload_libraries that takes effect for its own session. Without
-- it, the first openCypher call fails with
--   "ERROR: unhandled cypher(cstring) function call error on first cypher query".
-- The `LOAD 'age';` and `SET search_path` lines below are AGE's documented
-- per-SESSION setup; in this initdb session they make AGE usable immediately, but
-- they are session-scoped and do NOT persist — application sessions set their own
-- search_path (a managed host does the same, see retrieval-concepts.md).

-- BM25 lexical search (pg_textsearch, ADR-0006).
CREATE EXTENSION IF NOT EXISTS pg_textsearch;

-- pgvector: the halfvec(1024) + HNSW semantic-search leg.
CREATE EXTENSION IF NOT EXISTS vector;

-- Hierarchical label paths (section_path / loc_path).
CREATE EXTENSION IF NOT EXISTS ltree;

-- Apache AGE — last, after the simpler extensions. Requires the server-level
-- shared_preload_libraries='age' noted in the header (sibling Dockerfile, 2.6).
CREATE EXTENSION IF NOT EXISTS age;
LOAD 'age';
SET search_path = ag_catalog, "$user", public;
