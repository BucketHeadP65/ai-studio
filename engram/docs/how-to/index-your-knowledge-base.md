---
id: engram-howto-index-your-knowledge-base
aliases: ["engram-howto-index-your-knowledge-base"]
type: how-to
app: engram
status: current
tags: [index, init, search, getting-started]
title: "Index your knowledge base and query it"
---

# Index your knowledge base and query it

**Standard:** Diátaxis (how-to).

This guide takes a knowledge base from nothing to queryable, end to end: bring up the
database, initialise the knowledge base, index it into Postgres, and run your first
search. It is task-focused, for *why* engram works this way (the immutable
knowledge base, the disposable mirror, read-only retrieval) read the
[domain index](../explanation/domain/index.md). For per-command contracts and
flags see the [init](../reference/init.md), [index](../reference/indexing.md),
and [serve](../reference/serve.md) references.

Two project directories are involved. The database and the schema chain belong to
the **library**, so run `docker compose` and `alembic` from `engram/`. Every
`studio` command is the **application**, so run it from `studio/`. Each step below
says which.

## Before you start

- `uv` installed, with both projects synced: `uv sync` from `engram/` and from
  `studio/`.
- Docker running (for the local database).
- A knowledge base directory, a folder of Markdown notes. If you are starting from a
  bare folder, step 2 scaffolds the required layout for you.

## Step 1: Bring up the database and apply the schema

*(from `engram/`)*

The default local database is the **local Postgres** on host port **5434**,
Postgres with `pg_textsearch` BM25 and Apache AGE, matching the managed Postgres target (engram
ADR-0017). ParadeDB on port 5433 is opt-in and not needed for this guide.

```bash
# Start the local Postgres (the default service) on 127.0.0.1:5434
docker compose up -d

# Create the tables, indexes, api views/functions, and role grants
uv run alembic upgrade head
```

This is the repo's one alembic chain, and it is checkout-relative, the same chain
also migrates the workflow-store database by URL parameter, so nothing else in the
repo owns a second one.

To use ParadeDB instead, start it explicitly and set the dialect:
`docker compose --profile paradedb up paradedb` (port 5433), then export
`APP_RETRIEVAL__DIALECT=pg_search` and `APP_RETRIEVAL__GRAPH_BACKEND=cte`.

## Step 2: Initialise the knowledge base from a schema

*(from `studio/`)*

engram indexes any knowledge base from its own `KnowledgeBaseSchema` (`knowledge-base-schema.json`),
which declares the page types, relations, vocab, and facets for your domain. The
quickest way to author one is the plugin's onboarding skill **`/knowledge-base:init`**,
which interviews you, co-designs the schema, and then calls the commands below, this
is the install-into-a-knowledge-base path. You can also hand-write `knowledge-base-schema.json` or build
one without an LLM via `uv run studio init spec --interactive`.

With a `knowledge-base-schema.json` in hand, run the one-shot initialiser. It chains
**scaffold, then db, then first index** (the index smoke skips embedding by default):

```bash
# Scaffold _meta/, migrate the DB, and run the first compile in one shot
uv run studio init all --spec knowledge-base-schema.json --knowledge-base .
```

Useful options:

- `--db-url URL`: point at a specific Postgres (asyncpg URL). Falls back to
  `APP_INFRASTRUCTURE__DATABASE__URL` or `application_settings.json`.
- `--collection NAME`: the knowledge base partition the smoke index loads (default
  `research`). Multiple collections can coexist in one database.

To run the stages separately:

```bash
uv run studio init scaffold --spec knowledge-base-schema.json --knowledge-base .  # _meta/ + templates only (no DB)
uv run studio init db --knowledge-base . [--db-url URL]                # alembic upgrade + load vocab
uv run studio init spec --interactive [--out knowledge-base-schema.json]  # non-LLM spec builder
```

`--knowledge-base` defaults to `.`, so pass it whenever you are not standing in the knowledge base
root, which, running from `studio/`, is most of the time.

## Step 3: Index the knowledge base into Postgres

*(from `studio/`)*

Indexing runs the five-stage pipeline (**parse to resolve to validate to load to
embed**), turning your notes into the rows that search and fetch read. It is
**incremental by default**: a content-hash manifest re-indexes only the changed
files (and the unchanged files whose link resolution flips), so re-running after
an edit is fast.

```bash
# Incremental compile including embeddings (default collection "research")
uv run studio index

# Skip the paid embedding stage for a fast dev/CI loop
uv run studio index --no-embed

# Force a full whole-knowledge-base rebuild (the guaranteed-correct escape hatch)
uv run studio index --full

# Preview the dirty set without writing anything (always exits 0)
uv run studio index --dry-run

# Compile a specific knowledge base partition
uv run studio index --collection research
```

The knowledge base root comes from `--knowledge-base` when given, else the nearest `knowledge-base-schema.json` above the
working directory.

A non-zero validation-error count makes `index` exit 1, an unclean knowledge base fails the
run. Rows still upsert, but fix the reported validation errors and re-index.

A knowledge base that declares itself private is read only through a login role of its own.
`index` creates that role and gives it a password derived from
`infrastructure.database.reader_secret`. Set the secret in `application_settings.json` before
indexing a private knowledge base. Without it no reader can read that knowledge base.

## Step 4: Verify it worked

*(from `studio/`)*

```bash
# Row counts per table, embed-model contract, and last-compile info
uv run studio status
```

`status` also reports the last run's mode (incremental or full) and a hint if the
next run will be forced to a full index (e.g. on a cold start or schema change).
Pass `--collection NAME` to inspect a specific partition.

## Step 5: Query the knowledge base

*(from `studio/`)*

Search and fetch from the command line:

```bash
# Hybrid (BM25 + vector) search; -k sets the number of results
uv run studio search "QUERY" -k 12

# Pre-filter to a facet, or run BM25-only (no embedding call needed)
uv run studio search "QUERY" --facet tasks=retrieval
uv run studio search "QUERY" --bm25-only

# Fetch one unit's full text by id
uv run studio fetch ID
```

`search` also accepts `--level` and `--collection`, and `--facet key=value` repeats to narrow
on several values or keys. The server caps `-k` at the `k_ceiling` of `kb.retrieval_tuning`, 50 at
most, keeps at most `rows_per_group` hits of one page, 3 by default, and at most `max_hits_per_document`
hits of one document (a source with the pages made from it, or a page made from no source), 1 by
default, so `-k` counts documents. The owner may set that cap for one knowledge base. A hit's other
matching units print indented under it, each with its snippet. See the
[serve reference](../reference/serve.md) for the full surface.

## Step 6: Serve it to tools (optional)

*(from `studio/`)*

Expose the same read-only retrieval to AI Factory Studio or an AI agent:

```bash
# Dashboard HTTP API for studio-client — local-only, binds 127.0.0.1:8100
uv run studio serve-http

# MCP stdio server (tools: describe_schema, run_sql, graph_expand, find_paths, search, fetch)
uv run studio mcp
```

Both are read-only by construction, they never index, embed, or mutate the
knowledge base. Their reader connection (`infrastructure.database.reader_url`) must log in as
the read-only `kb_reader` role. They refuse to read through a connection that can do more than
read the `api` schema. To index your edits, re-run `uv run studio index`
(step 3). The database is a disposable derivative, fully reconstructable from the
knowledge base.
