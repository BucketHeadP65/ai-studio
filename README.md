# AI Studio

AI Studio turns folders of markdown notes into knowledge bases that agents can search, and runs AI workflows over them
that you can read, check and trust. Its web app, **AI Factory Studio**, is where you chat with the agent, build and run
workflows, see what each run did, and browse your knowledge bases.

- **Knowledge bases.** A folder of Obsidian-style markdown notes is indexed into Postgres. Agents search it with BM25,
  vectors and a link graph, over a CLI, an MCP server and a local HTTP API. Each knowledge base declares its own page
  types, fields and vocabulary in a schema file, so the same code serves research notes, a repository's docs or a
  benchmark dataset.
- **Workflows as data.** A workflow is a typed file that an agent or a person writes. It is validated before it runs,
  built into a `pydantic_graph` graph, and run behind gates that a person, a judge step or the agent decides. Its trust
  level comes from the evidence its runs recorded, never from a field someone sets.
- **Run inspection.** Rules read the recorded conversation of each run and raise leads. An inspector model turns a
  lead into a finding, such as a wrong tool chosen or a call with the wrong arguments, that names the part of the setup
  it blames, or dismisses it.
- **Templates.** A knowledge base starts from a template that brings its schema, its guidance pages and its
  workflows. Two templates load public retrieval benchmarks, so retrieval can be scored against them.

## Screenshots

**The workflow library.** Every saved workflow with its trust level, its steps and gates, and what its runs cost.

![The workflow library in AI Factory Studio, a grid of workflow cards with trust levels, step counts, gates and run costs](docs/images/studio-library.png)

**A workflow's map.** The steps of one workflow in the order they run, with a fan-out over each item, an agent gate
and the path a rejection takes back.

![The map of the workflow Flag ideas from new digests, its steps drawn as a graph with one agent gate](docs/images/studio-workflow-map.png)

**A run.** What one run did: its outcome checked against its own facts, what it left behind, what it cost, the
timeline of its steps and the values each step produced.

![The page of one completed run of the workflow Link news digests, with outcome, cost, evidence, values and timeline](docs/images/studio-run.png)

**Inspection findings.** What the inspector found in recorded runs: the symptom, the run, the model, and the part of
the setup each finding blames.

![The Inspection page, a table of findings with symptom, run, model, date and blamed surfaces](docs/images/studio-findings.png)

**A knowledge base's graph.** The `multihop-rag` knowledge base, built from the MultiHop-RAG news benchmark, with one
digest selected and the stories and entities it links to.

![The link graph of a news knowledge base, one digest page selected and its neighbours listed beside the graph](docs/images/studio-graph.png)

## Run it

You need Docker, [uv](https://docs.astral.sh/uv/getting-started/installation/) and Node 20.9 or later. Only Postgres runs
in a container. The Python apps and the web app run from source.

### 1. Install

```bash
(cd engram && uv sync)            # the database migrations run from here
(cd studio && uv sync)
(cd harness && uv sync)
(cd studio-client && npm install)
```

Each Python project keeps its own environment in `<project>/.venv`. `studio` and `harness` install `engram`, `core` and
`connectors` from their folders as editable path dependencies.

### 2. Configure

```bash
cp engram/config/application_settings.example.json engram/config/application_settings.json
```

This one gitignored file holds every connection and secret for every app. Fill in these keys:

| Key | What it is for |
| --- | --- |
| `infrastructure.database.url` and `reader_url` | The owner and the read-only database URLs. The example already names the local database. Keep `?sslmode=disable` for a local Postgres. |
| `infrastructure.openrouter.api_key` | The chat agent and workflow steps run on three model tiers. Each tier defaults to `glm-5.3-flash`, which is reached through OpenRouter. |
| `infrastructure.gateway.api_key` | The [Pydantic AI Gateway](https://pydantic.dev/gateway) key, for gateway models such as `gpt-5.4`. You choose each tier's model on the Studio's Harness page. |
| `ingestion.embedding_deployment` and `infrastructure.embeddings` | The embedding model, and the connection that serves it: `azure_openai` (the default, with `infrastructure.azure_openai.endpoint` and an Entra ID login such as `az login`) or `gateway` (with the gateway key). Indexing and importing workflows embed text, and `studio init all` stops when this connection is not set. |
| `ingestion.chunk_planner_model` | Optional, empty by default. A chat deployment on `infrastructure.azure_openai.endpoint`, such as `gpt-6-luna`, that chooses where a heading section of a source over the maximum chunk size changes topic when indexing. Empty, or with no endpoint, indexing cuts that text by size alone. Setting it makes the next index a full one. |
| `infrastructure.logfire.read_token` | A Pydantic Logfire read token, optional. The harness reads spend with it. Without one the harness still starts, and its spend reports say Logfire is off. `write_token` is optional too and sends traces. |
| `knowledge_bases` | The registered knowledge bases by name, each `{"path": "/absolute/folder"}`. The Studio writes this for you when you create or register one. |

The `studio` app also reads any value from an environment variable: `APP_` and the key path joined by `__`, such as
`APP_INFRASTRUCTURE__DATABASE__URL`. The harness keeps its own settings in `harness/config/harness_settings.json`,
overridden the same way, for example `APP_RUN__RUN_ENGINE=temporal`.

### 3. Create the databases

```bash
cd engram
docker compose up -d
uv run alembic upgrade head
docker compose exec postgres createdb -U wiki engram_workflows
APP_INFRASTRUCTURE__DATABASE__URL='postgresql+asyncpg://wiki:wiki@localhost:5434/engram_workflows?sslmode=disable' \
  uv run alembic upgrade head
```

`docker compose up -d` starts Postgres 17 with pg_textsearch (BM25), Apache AGE, pgvector and ltree on port 5434. One
migration chain sets up both databases: `knowledge_bases` for the knowledge bases and `engram_workflows` for the
workflow library. The harness creates a third database, `aifactory_sessions`, for the conversation record when it
first starts.

### 4. Start the stack

| Service | Port | Start it with |
| --- | --- | --- |
| Postgres | 5434 | `cd engram && docker compose up -d` |
| Knowledge base API | 8100 | `cd studio && uv run studio serve-http` |
| Agent and workflow runtime | 8770 | `cd harness && uv run harness serve` |
| AI Factory Studio | 3031 | `cd studio-client && npm run dev` |

Start them in this order. The harness reads the registered knowledge bases from the knowledge base API and refuses to
start until that API answers. `studio serve-http` also runs `studio worker` as a child process, which refreshes
knowledge bases and runs their schedules.

On macOS, `scripts/stack/stack.sh install` runs all four as login services that restart when they exit, and
`scripts/stack/stack.sh status|restart|logs <service>` manages them. [scripts/stack/README.md](scripts/stack/README.md)
has the details. After changing backend code, run `scripts/stack/stack.sh restart harness studio-api`. The web app
reloads on save.

### 5. Open the Studio

Open <http://localhost:3031>. <http://127.0.0.1:3031> works too, because the knowledge base API accepts requests from
both origins. The sidebar holds Home, Chat, the workflow pages (Fleet, Library, Schedules), Projects, Knowledge
bases, Harness, Inspection and Settings.

### 6. Your first knowledge base

```bash
cd studio
uv run studio init --template repo-docs --knowledge-base /path/to/folder   # copy the template's schema and guidance pages
uv run studio init all --knowledge-base /path/to/folder                    # migrate, seed the vocabulary and index
```

The new knowledge base then shows on the Studio's Knowledge bases page as indexed. Open it and register it with its
folder, or add it to `knowledge_bases` yourself. The page's **New base** button does all of this from a template in one
step, and **Refresh now** on a knowledge base syncs its sources and indexes it again with embeddings.

Then import the template's workflows into the library, with the stack running:

```bash
cd harness
uv run harness import workflows                          # the steps the templates share
uv run harness import ../templates/repo-docs/workflows   # the template's own steps and workflows
```

## Packages

| Path | What it is | Runs |
| --- | --- | --- |
| [engram/](engram/README.md) | The knowledge base library: the note grammar, indexing into Postgres, retrieval and authoring tools, evals, init and export. It also holds the repository's one migration chain and a Claude Code plugin. | library |
| [studio/](studio/README.md) | The knowledge base app: the `studio` CLI, the knowledge base HTTP API and its background worker. | `uv run studio serve-http` on :8100 |
| [harness/](harness/README.md) | The agent and workflow runtime: chat sessions, the workflow library, runs, gates, schedules, run inspection, plugins and profiles, and an ACP entry. | `uv run harness serve` on :8770 |
| [studio-client/](studio-client/README.md) | **AI Factory Studio**, the web app. | `npm run dev` on :3031 |
| [core/](core/README.md) | The shared Python library: database, files, settings, messaging, observability and the AI stack, including the workflow engine. | library |
| [connectors/](connectors/README.md) | Source connectors, watch-list scanners and document converters: Zotero, YouTube, any URL, X, BEIR and MultiHop-RAG. | library |
| [templates/](templates/) | Knowledge base templates: a schema, guidance pages, workflows and sometimes evals. | data |
| [scripts/](scripts/) | The macOS stack scripts and the code and documentation checks. | tools |

## Templates

- [`research`](templates/research/README.md): research notes from source mirrors, digested into pages whose claims
  quote their sources, with workflows that digest new sources, flag ideas and settle one decision.
- [`repo-docs`](templates/repo-docs/README.md): a repository's docs as code (ADRs, architecture, reference and working
  documents), with workflows that fold note threads into analyses and propose ADRs.
- [`news-benchmark`](templates/news-benchmark/README.md): the MultiHop-RAG news benchmark held filter-first, with
  workflows that digest the articles, link their entities and relate stories.
- [`benchmark-dataset`](templates/benchmark-dataset/README.md): one BEIR dataset, such as `scifact` or `nfcorpus`,
  with its search scored against the dataset's relevance judgments.

## Development

Every Python project has the same gate, run from inside its folder:

```bash
uv sync && uv run ruff check && uv run pyright && uv run pytest
```

Unit tests never need Docker. The integration and e2e tests start Postgres with testcontainers and skip when Docker is
not running. In `studio-client/`, run `npm test`, `npm run typecheck`, `npm run lint` and `npm run test:e2e`.

## Documentation

[CONTEXT-MAP.md](CONTEXT-MAP.md) indexes every governed doc. [docs/README.md](docs/README.md) explains how the docs
are organized, and [docs/architecture/index.md](docs/architecture/index.md) is the architecture. Each package has its
own README, and most have a `docs/` folder. [CLAUDE.md](CLAUDE.md) holds the conventions that agents and reviewers
follow.

## License

AI Studio is released under the MIT License. See [LICENSE](LICENSE).
