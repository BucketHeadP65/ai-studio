# studio, the knowledge base app

`studio` is the application over the [`engram`](../engram/README.md) library: the `studio` command line and the
knowledge base HTTP API that AI Factory Studio reads on port 8100. It is a Python 3.13 project with the package
`studio` and the console script `studio`.

Nothing in it names a knowledge base's page types, statuses, folders or fields. Every command and route takes them from
the knowledge base's own schema or from the request.

## Run it

```bash
uv sync
uv run studio --help
uv run studio serve-http             # the API on 127.0.0.1:8100, with `studio worker` as a child process
uv run studio serve-http --no-worker # the API alone, for a deployment that runs the worker itself
```

The settings come from `../engram/config/application_settings.json`, copied from the committed
`application_settings.example.json`. An environment variable overrides any value: `APP_` and the key path joined by
`__`, such as `APP_INFRASTRUCTURE__DATABASE__URL`. The root [README](../README.md#2-configure) lists the keys to fill in.

A command that acts on one knowledge base takes `--knowledge-base <folder>`, or uses the working directory when it
holds a `knowledge-base-schema.json`.

## Commands

| Command | What it does |
| --- | --- |
| `init` | Starts a knowledge base from a template (`--template`, `--slug`), or with `scaffold`, `db`, `all` and `spec` writes its `_meta/` files, migrates the database, seeds the vocabulary, indexes it, or builds a schema from prompts. |
| `index` | Indexes the knowledge base into Postgres: parse, resolve, validate, load and embed. Only changed files by default, `--full` for all of them, `--no-embed` to skip embedding. |
| `sync` | Brings the knowledge base's `Sources/` mirrors in line with its connectors. |
| `search`, `fetch` | Searches with BM25 and vectors (`--bm25-only` for BM25 alone, `--facet key=value` to narrow), and fetches one unit's full text by id. |
| `status` | Prints the row count of each table, the embedding metadata and how far the index is behind the files. |
| `mcp` | Serves the retrieval tools over MCP on stdio. `--authoring` and `--doc-authoring` add the write tools. |
| `serve-http`, `worker` | Serves the HTTP API, and runs the background worker that refreshes knowledge bases, runs their schedules and delivers events. |
| `source-queue`, `watch-list` | Adds, lists and retries URLs on the source queue, and adds, lists and removes the entries the watch list scans for new sources. |
| `decisions`, `decide` | Lists the pages waiting for a decision, and moves one page to a new status. |
| `note`, `ingest` | Builds and validates notes from the grammar, and writes one page straight into Postgres. |
| `vocab`, `summaries` | Proposes and approves new vocabulary values and index-time summaries. |
| `evals` | Retrieval evaluation: run, score, tune, compare, import relevance judgments and more. |
| `export`, `feedback` | Exports the knowledge base as an OKF v0.2 directory, and prints what agents reported about the retrieval tools. |

## The HTTP API

`studio serve-http` binds to 127.0.0.1 and allows browser requests from `http://localhost:3031` and
`http://127.0.0.1:3031` only. Its routes:

- `GET /api/knowledge-bases` lists every registered or indexed knowledge base, and `POST` creates one from a template
  or a schema.
- `GET /api/knowledge-bases/{name}` and `.../schema`, `.../decisions` and `.../refresh-runs` read one of them.
- `PUT` and `DELETE .../registration` register or unregister a folder, and `PUT` and `DELETE .../schedule` set or clear
  its refresh schedule.
- `POST .../refresh`, `.../source-queue`, `.../watch-list` and `.../notes` refresh it, queue a URL, watch a feed or
  keep a run's value as a note. `PATCH .../pages/{page_id}` decides one page.
- `GET /api/graph?knowledge_base=<name>` is one knowledge base's link graph, `GET /api/templates` the templates,
  `GET /api/jobs/{job_id}` one queued job, and `GET /api/events` the change notifications.

The registered knowledge bases are the `knowledge_bases` section of the settings file. The harness reads them over
this API, so the harness refuses to start until it answers.

## Develop

```bash
uv run ruff check && uv run pyright && uv run pytest
```

`studio` depends on `engram`, `engram-connectors` (with the `conversion`, `source-queue`, `x`, `youtube` and `zotero`
extras) and `core`, all from their folders in this repository. A change in any of them must keep this project green.
Composition uses wireup: each slice lists what it registers in `src/studio/knowledge_base/wiring/`, and every route
group is a controller class in `src/studio/knowledge_base/http/controllers/`.

## Docs

- [`CONTEXT.md`](CONTEXT.md), the glossary of knowledge base management.
- [`docs/reference/knowledge-base-management.md`](docs/reference/knowledge-base-management.md), the registry, the
  routes, the Knowledge bases page and the terminal.
- [`docs/adr/`](docs/adr/), this app's decisions.
