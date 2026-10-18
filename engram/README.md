# engram

engram is the knowledge base library. It indexes a folder of Obsidian-style markdown notes into Postgres and serves
retrieval and writing tools to agents. The folder is the source of truth, and the database is derived from it and can
be rebuilt at any time.

engram is domain-general. Every knowledge base declares its page types, frontmatter fields, facets, relations and
vocabulary in its own `knowledge-base-schema.json`, and there is no default schema: a folder without one is refused
with a pointer to `studio init --template <slug>` and the templates in [`../templates/`](../templates/).

The project ships one Python package, `engram`, and no command line. The `studio` CLI and the knowledge base HTTP API
over it live in [`../studio/`](../studio/README.md). engram depends on [`../core/`](../core/README.md) and never imports
`studio`.

## What is inside

| Subpackage | What it does |
| --- | --- |
| `kb` | The schema and the note grammar: parsing, rendering and validating a page against its page type. |
| `indexing` | Parses, resolves links, validates, loads and embeds a knowledge base. An index run only reads the files changed since the last one unless asked for a full run. Source mirrors are cut at their headings into chunks sized by `ingestion.chunk_target_tokens` and `ingestion.chunk_max_tokens`. |
| `retrieval` | The read tools: hybrid search over BM25 and vectors with facets and a date range, fetch by id, graph walks, read-only SQL over the `api` schema and optional re-ranking of search hits. |
| `authoring` | The write tools a host can mount: `write_note`, `check_note`, `read_page`, `decide_page`, `resolve_entities`, `request_source` and more. Every write is validated against the knowledge base's own grammar and indexed again. |
| `sources` | The ports for connectors, document converters and watch-list scanners, the source queue (`_meta/source-queue.yaml`) and the watch list (`_meta/watch-list.yaml`). The implementations live in [`../connectors/`](../connectors/README.md). |
| `evals` | Retrieval evaluation with pydantic-evals: datasets, scoring, policy tuning and paired comparisons. `studio evals` runs it. |
| `init`, `export` | Scaffolding a knowledge base from its schema or a template, and exporting it as a portable OKF v0.2 directory. |
| `agent_memory` | A store of beliefs an agent can recall and add to. |
| `testing` | The test toolkit other projects import (fakes, factories, knowledge base builders and a containerized Postgres pytest plugin), installed with the `engram[testing]` extra. |

[`plugin/`](plugin/README.md) is a plugin for Claude Code and Codex that serves a knowledge base's retrieval tools over
`studio mcp`.
[`alembic/`](alembic/) is the repository's one migration chain.

## Use it from Python

Outside code imports only the names `engram` exports:

```python
from engram import embedded_knowledge_base, embedded_read_tools, embedded_authoring, load_knowledge_base_schema
from engram.init.contracts import scaffold, list_templates
```

Indexing, syncing, searching, evals and the MCP server are `studio` commands. Run `uv run studio --help` from
`../studio`.

## Set up a database

```bash
docker compose up -d          # Postgres 17 with pg_textsearch, Apache AGE, pgvector and ltree on localhost:5434
uv sync
uv run alembic upgrade head   # schemas, the read-only api views and functions, roles and indexes
```

The migrations read the database URL from `infrastructure.database.url` in `config/application_settings.json`, or from
`APP_INFRASTRUCTURE__DATABASE__URL`, which wins. The same chain migrates the workflow library database
`engram_workflows` when that variable points at it.

ParadeDB is the other supported database: `docker compose --profile paradedb up paradedb` starts it on port 5433, and
`APP_RETRIEVAL__DIALECT=pg_search` with `APP_RETRIEVAL__GRAPH_BACKEND=cte` selects it at run time.

## The read-only role

Agents read through the `kb_reader` role, which may query the `api` schema only. The migrations set its password from
the password in `infrastructure.database.reader_url` (or `APP_INFRASTRUCTURE__DATABASE__READER_URL`), so the role always
matches the URL the apps connect with. To change it, change that URL and run `uv run alembic upgrade head` again.

A knowledge base marked private gets a reader role of its own. Its password is derived from
`infrastructure.database.reader_secret`, and with no secret set a private knowledge base reads as empty.

## Embeddings

An index run embeds pages, sections, claims and raw chunks with the model `ingestion.embedding_deployment` names, on
the connection `infrastructure.embeddings` names: `azure_openai` with an Entra ID token (for example after `az login`),
or `gateway` with the Pydantic AI Gateway key. Text whose hash has not changed is not embedded again. `studio index
--no-embed` skips embedding, and `studio search --bm25-only` searches without embedding the query.

## Tests

```bash
uv run ruff check
uv run pyright
uv run pytest
```

The unit tests need no Docker. The integration tests run the migrations against a Postgres started by testcontainers
and skip when Docker is not running. `KNOWLEDGE_BASE_TEST_DIALECT=pg_search` runs them on ParadeDB instead. The
end-to-end flows over the CLI and HTTP live in `../studio/tests`.

## Docs

- [`CONTEXT.md`](CONTEXT.md), the glossary.
- [`docs/reference/`](docs/reference/), one page per area: indexing, sync, init, export, evals, conversion, the plugin
  and more.
- [`docs/architecture/index.md`](docs/architecture/index.md), the architecture.
