<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir docs/architecture — do not edit here; edit the master and re-run the script. -->

## 2. Containers & data flow
> **TL;DR:** Two Python apps (studio on :8100, harness on :8770) + one TS app (studio-client) over three libraries (engram, core, engram-connectors), with `templates/` as data beside them. Two Postgres databases of the same shape on one server, the knowledge base DB (the disposable local Postgres on :5434 by default, ParadeDB on :5433 opt-in) and the workflow library `engram_workflows`, migrated by one alembic chain.
<!-- load-when: containers, deployment, databases, local Postgres, ParadeDB, ports, data flow, C4 L2, connectors, templates -->

```mermaid
C4Container
    title Containers, AI Factory Studio
    Person(owner, "Architect / owner")
    System_Boundary(se, "AI Factory Studio") {
        Container(studioclient, "studio-client", "Next.js 16 / React 19, :3031", "UI: search, ask, graph, workflow studio, ops, evals")
        Container(studiocorpus, "studio.corpus", "Typer CLI + FastAPI :8100 + MCP", "Knowledge base sync to index to serve pipeline")
        Container(studioagent, "harness", "FastAPI :8770 + ACP", "Coding agent, validates, indexes and runs workflows behind gates")
        Container(engram, "engram", "Python lib, uv path dep", "Note grammar, indexing pipeline, retrieval plugins, evals, sources ports, init/export")
        Container(connectors, "engram-connectors", "Python lib, uv path dep + extras", "Zotero / YouTube / URL-intake connectors and their converters, found by entry point")
        Container(core, "core", "Python lib, uv path dep", "db, files, markdown, cqrs, ai: model factories, retrieval, disclosure, workflows engine")
        Container(templates, "templates/", "Data, one folder per knowledge base", "knowledge-base-schema.json, _meta/ guidance, workflows/, the template a knowledge base starts from")
        ContainerDb(corpusdb, "knowledge base DB", "local Postgres pg17, :5434 (ParadeDB :5433 opt-in)", "disposable indexed knowledge base + api schema, kb_reader role and one login role per private knowledge base")
        ContainerDb(storedb, "engram_workflows", "same server, same memory schema", "workflows as an indexed collection")
    }
    System_Ext(corpus, "Obsidian knowledge base + Zotero")
    System_Ext(azure, "Azure OpenAI / Document Intelligence")
    Rel(owner, studioclient, "localhost")
    Rel(studioclient, studiocorpus, "/api/* (127.0.0.1:8100, no auth)")
    Rel(studioclient, studioagent, "/api/* (127.0.0.1:8770, no auth), the studio station")
    Rel(studiocorpus, studioagent, "POST /api/runs/history/{run_id}/artifacts/resolve, reads a run's fixed value to keep as a note")
    Rel(studiocorpus, studioagent, "POST /api/schedules/refresh-runs, reports each ended refresh run through its outbox")
    Rel(studiocorpus, engram, "indexes and queries through the library plugins")
    Rel(studioagent, core, "core.ai.workflows engine + adapters")
    Rel(studiocorpus, corpusdb, "SQLAlchemy 2 async, serve path via corpus_reader")
    Rel(studioagent, storedb, "workflows as an engram collection")
    Rel(studiocorpus, corpus, "sync mirrors, index notes")
    Rel(studiocorpus, azure, "embeddings + PDF fallback")
    Rel(studiocorpus, providers, "resolves declared connector names by entry point")
    Rel(studioagent, providers, "classifies a pasted URL through the same registry")
    Rel(studiocorpus, templates, "studio init --template copies schema + guidance into a new knowledge base")
    Rel(studioagent, templates, "harness import loads a template's workflows into the library")
```

Load-bearing properties of this picture:

- **The knowledge base DB is disposable. The workflow library is not. One chain migrates both.**
  The knowledge base DB is a global derivative of the knowledge base, rebuilt by
  `studio index` ([ADR 0001](../../adr/0001-wiki-app-placement-and-core.md)). The
  **default** local bed is the disposable **local Postgres on host :5434**
  (`postgres:17` + `pg_textsearch` BM25 + Apache AGE + pgvector + ltree),
  `docker compose up` from `engram/` starts it, and it runs the `pg_textsearch`
  dialect + AGE graph the same way the managed Postgres target does, so the
  pipeline is developed and tested offline on that grammar (engram
  [ADR-0017](../../../engram/docs/adr/0017-local-postgres-primary-testbed.md)).
  **ParadeDB on :5433** stays a first-class opt-in, behind a `paradedb` compose
  profile (`APP_RETRIEVAL__DIALECT=pg_search` + `graph_backend=cte` at runtime).
  The **workflow library** is a second database, `engram_workflows`, on the same
  server and with the same `memory` schema: workflows live in it as an engram
  collection: the save path writes them through engram's validated ingest, so
  the library carries authored rows rather than being a pure rebuild of a markdown
  tree. Run state is a local run log beside it, and the agent's own timeline is the
  append-only session log (harness ADR 0001). Neither database has a chain of
  its own,
  engram's single chain migrates both by URL parameter
  ([ADR 0009](../../adr/0009-ai-factory-studio-consolidation.md), amended by [ADR 0010](../../adr/0010-harness-extraction.md)).
  Source: engram/docs/adr/0017-local-postgres-primary-testbed.md, engram/docker-compose.yml
- **The knowledge base app reads a run's value from the agent app, over one route it does not choose.**
  Keeping what a run produced as a note means writing a value the agent app holds, so
  `studio` posts the run's own reference to the agent's read-only resolver at
  `/api/runs/history/{run_id}/artifacts/resolve` and validates the answer against that
  reference. The base URL is `infrastructure.harness.base_url`, which defaults to the local
  harness, and no request ever supplies an address.
  Nothing imports a harness module and nothing opens its run store
  ([ADR 0019](../../adr/0019-knowledge-base-reads-a-run-value-over-http.md)).
  The studio also reports each ended refresh run to `POST /api/schedules/refresh-runs` through its outbox, which delivers again until the harness answers
  ([ADR 0020](../../adr/0020-refresh-runs-arm-schedules.md)). These are the only two edges between the two backends.
- **Both backends are localhost-only by design.** `studio serve-http` binds
  127.0.0.1:8100 (CORS open exactly to :3031). The agent app binds
  127.0.0.1:8770 (CORS open to exactly the two spellings of the studio's dev
  origin). Neither has auth. The localhost binding is the boundary.
- **Shared code flows through libraries, not imports across apps.**
  Python apps consume `core/` and `engram/` as editable uv path sources. The SPA
  keeps its app-agnostic code in `studio-client/src/shared/`, which imports
  nothing from the rest of the app. `engram` never imports `studio` or the
  engine. A package-boundary test enforces it. A change in a shared lib must
  keep **all** consumers' suites green.
- **Knowledge base vocabulary and host knowledge are both outside the code.** A knowledge base's
  page types, folders, field names, statuses and collection name live in its template
  (`templates/<slug>/knowledge-base-schema.json` plus its guidance pages and workflows), and
  every layer above the grammar takes them from the knowledge base's own schema or from the
  request. A connector's URL shapes, key derivation, item-type vocabulary and deep
  links live in `providers/`, discovered by entry point rather than imported, so
  `engram` indexes a knowledge base with no connector installed. Both rules are the root
  ownership clause and [ADR 0018](../../adr/0018-corpus-bundles-and-provider-packages.md),
  held by an import-boundary test per Python project.
