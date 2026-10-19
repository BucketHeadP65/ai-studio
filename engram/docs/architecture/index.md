<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir engram/docs/architecture — do not edit here; edit the master and re-run the script. -->

# Architecture doc — section index

**Master:** [architecture.md](architecture.md) — the single source of
truth. Edit it, then run `python3 scripts/docs/split_doc.py --dir engram/docs/architecture` to regenerate `sections/` and this index.

**Purpose of this index:** when a question touches specific points of
the design, load ONLY the matching `sections/` chunk(s) below instead
of the whole document. Match against the *Load when* keywords; token
counts say what each load costs.

## Orientation (from the master)

This is engram's **graduated arc42-lite / C4 architecture master** (badge
**arc42** + **C4**, an architecture description in the sense of **ISO/IEC/IEEE 42010**,
whose per-slice component + domain-model diagrams are its **IEEE-1016** software-design view).
It graduated out of the root master's section 04 per
[ADR-0005](../../../docs/architecture/architecture.md)'s graduation rule and the
layered-three-view pattern of
[ADR-0020](../adr/0020-domain-documentation-architecture.md): engram's structure
had outgrown a ~430-token section. This master owns **structure**, the C4 L2
container/component overview, the per-slice C4 L3 component diagrams, the
per-slice domain-model class diagrams, and the single system ERD. It is
**split-doc'd**: edit this master only, then run
`python3 scripts/docs/split_doc.py --dir engram/docs/architecture`. Readers and
agents should start from [index.md](index.md) and load only the sections whose
*load-when* keywords match the question.

Around this master sit its sibling layers (per ADR-0020): the three-view domain
package (Business / Research / Engineering, under `engram/docs/explanation/domain/`)
narrates the domain and links *down* here for structure. Each per-slice
`engram/docs/reference/<slice>.md` owns the *contract* (entry points, public API,
testing seams) and links *up* to the matching component diagram here. The
`vision.md` / `product-decisions.md` / ADRs own the *why* and are linked, never
restated. The entry point to every governed doc is
[CONTEXT-MAP.md](../../../CONTEXT-MAP.md). The binding vocabulary is
[engram/CONTEXT.md](../../CONTEXT.md).

Sections: 10 · whole document ≈ 21,097 tokens.

### 01 — Orientation & scope
[sections/01-orientation-scope.md](sections/01-orientation-scope.md) · ~585 tokens
- **TL;DR:** engram is a domain-general Obsidian-style markdown knowledge-base indexer (knowledge base to Postgres) plus a read-only retrieval server over CLI + HTTP + MCP, organized into five bounded contexts (Ingestion, Knowledge, Retrieval, Evaluation, and Lifecycle & interchange) over a cross-cutting CQRS / DB / DI core.
- **Load when:** orientation, scope, what is engram, bounded contexts, how to navigate, arc42

### 02 — Container & component overview (C4 L2)
[sections/02-container-component-overview-c4-l2.md](sections/02-container-component-overview-c4-l2.md) · ~893 tokens
- **TL;DR:** `engram/src/` holds exactly one package, the `engram` library. The application over it is `studio.knowledge_base`, a separate project whose three entry points (the Typer CLI, the FastAPI Studio API on :8100, the FastMCP stdio server) drive engram. Write-side intents (index, sync, export) flow through a CQRS bus to slice handlers, while the read path (search/fetch/graph) bypasses the bus and goes straight through query handlers + a read-only SQL gateway to Postgres. Azure OpenAI supplies embeddings.
- **Load when:** containers, components, C4 L2, entry points, CQRS bus, data flow, overview, how it fits together, package layout, engram library vs studio application, library, application

### 03 — Ingestion (bounded context)
[sections/03-ingestion-bounded-context.md](sections/03-ingestion-bounded-context.md) · ~4,055 tokens
- **TL;DR:** The source-to-`Sources/` path: engram owns the two ports (`Connector`, `DocumentConverter`), the entry-point registry that resolves a knowledge base's declared connector names, the guarded fetcher, the mirror store, the reconcile, the source queue and the scanners. The connector and converter implementations (Zotero, YouTube, the general URL connector, arXiv-HTML/Azure conversion) live in the sibling `connectors/` project and arrive by entry point.
- **Load when:** ingestion, connectors, zotero, conversion, mirror, sync, source queue, watch list, sources, pdf, arxiv, mirrors, paper import, entry point, registry, scanners, refresh

### 04 — Knowledge (bounded context)
[sections/04-knowledge-bounded-context.md](sections/04-knowledge-bounded-context.md) · ~2,272 tokens
- **TL;DR:** The wikinote kernel is the note grammar (a parser ⟷ renderer pair over one set of domain models, shared by the indexer and authoring), and indexing is the five-stage rebuild (parse to resolve to validate to load to embed) with a true-incremental path that indexes again only a provably minimal dirty set.
- **Load when:** knowledge, knowledge base, wikinote, note grammar, index, indexing, parse, resolve, validate, validation, embed, incremental, claims, pipeline

### 05 — Retrieval (bounded context)
[sections/05-retrieval-bounded-context.md](sections/05-retrieval-bounded-context.md) · ~2,499 tokens
- **TL;DR:** The serve slice is the read-only query surface, one shared retrieval toolset (search/fetch/graph_expand/find_paths) over dialect (BM25) and graph seams, hitting only the `api` schema through the read-only `kb_reader` role (a private knowledge base through its own login role), one snapshot per tool call, exposed identically over the CLI, the Studio API, and the MCP server.
- **Load when:** retrieval, serve, search, fetch, graph, mcp, http api, dialect seam, graph backend, kb_reader, read path, read tools, SQL tools, private knowledge base, statement guard, tuning, snapshot, graph reach

### 06 — Evaluation (bounded context)
[sections/06-evaluation-bounded-context.md](sections/06-evaluation-bounded-context.md) · ~3,859 tokens
- **TL;DR:** The evals slice generates judge-free retrieval ground-truths from the knowledge base's own structure, maintains them as immutable freshness-swept dataset versions with a frozen yardstick, runs a tuning loop that adjusts a unified RetrievalPolicy against them (human-gated everywhere except one machine rung that may write the per-knowledge-base `_meta/active-policy.yaml` behind four guardrails), diagnoses failures with deterministic detectors plus a gated LLM reflector, proposes gated structural-schema recommendations, scores runs with recall@k/precision@k/MRR over a Logfire-instrumented harness, and grades an answer's support, contradiction handling and abstention from the immutable evidence one blind, tool-guarded trial retained.
- **Load when:** evaluation, evals, tuning, retrieval policy, gold dataset, recall, precision, mrr, rca, structural seed, coverage, freshness, staleness, frozen split, freeze, runtime policy, rollback, reflection, recommend, evaluation run, tuning run, dataset, recommendation, quote check, freshness check, outcome evaluation, answer grading, answer criterion, delivered evidence, blind trial, abstention, counterevidence, answer support, trial trace

### 07 — Lifecycle & interchange (bounded context)
[sections/07-lifecycle-interchange-bounded-context.md](sections/07-lifecycle-interchange-bounded-context.md) · ~2,735 tokens
- **TL;DR:** The cradle-to-interchange slices, init scaffolds `_meta/` and migrates the DB from a `KnowledgeBaseSchema`, export projects a collection into a read-only OKF v0.1 export directory on disk, and the plugin packages engram as a Claude Code plugin you install into any knowledge base.
- **Load when:** lifecycle, interchange, init, scaffold, bootstrap, export, okf, plugin, claude code plugin, onboarding

### 08 — Cross-cutting concepts
[sections/08-cross-cutting-concepts.md](sections/08-cross-cutting-concepts.md) · ~1,440 tokens
- **TL;DR:** The backbone every context rides on, the in-process CQRS bus (frozen Command/Event, one handler per command, events to the outbox for a worker to deliver), the DB foundation (`WikiBase` + custom types), the wireup composition root, pydantic-settings configuration, the observability facade, and the Typer CLI seam.
- **Load when:** cross-cutting, cqrs bus, unit of work, db base, wikibase, wireup, composition root, settings, observability, cli, dependency injection

### 09 — System data model (ERD)
[sections/09-system-data-model-erd.md](sections/09-system-data-model-erd.md) · ~2,218 tokens
- **TL;DR:** The single system ERD, 18 of the 21 `kb` tables across five clusters (Sources, Knowledge, Ops, Indexing-private, Evals), every knowledge-base-owned table keyed by composite `(collection, id)` with containment FKs (same-collection, CASCADE) and citation FKs (own target-collection column, null-paired CHECK, no cascade), as one `erDiagram`. The agent-visible `api` schema is a read-only projection over these base tables, none of which are granted to `kb_reader`.
- **Load when:** erd, schema, tables, data model, entity relationship, database tables, identity, composite key, containment, citation, facets, tenancy

### 10 — Quality, risks, tech debt
[sections/10-quality-risks-tech-debt.md](sections/10-quality-risks-tech-debt.md) · ~541 tokens
- **TL;DR:** engram-specific risks: the hand-written SQL `api` surface drifts silently from the ORM, the dual-dialect BM25 seam and AGE-vs-CTE graph backend each risk parity drift, the bus's declarative route table needs one entry per new command (pinned by the composition-root tests), and integration/e2e coverage is Docker-gated.
- **Load when:** risks, tech debt, known issues, caveats, coverage, drift, parity, quality
