---
id: engram-domain-engineering-view
aliases: ["engram-domain-engineering-view"]
type: explanation
app: engram
status: current
tags: [domain, architecture, cqrs, retrieval, bounded-context]
title: "engram: Software Engineering view (the domain as a system)"
---

# engram: Software Engineering view (the domain as a system)

> **Diátaxis: explanation.** This is the engineer's / new-contributor's view of the
> *whole* engram domain, the story of how the pieces fit, not the structure itself.
> **No structural diagrams live here**: the C4 container/component diagrams, the
> per-slice domain-model class diagrams, and the one system ERD all live in the
> **arc42/C4 master** ([architecture index](../../architecture/index.md)), and this
> view links *down* to them. Companion views tell the same story for non-technical
> stakeholders (the [Business view](business-view.md)) and for researchers (the
> [Research view](research-view.md)). A [concept concordance](index.md) proves all
> three agree. The binding vocabulary is defined once in the
> [engram glossary](../../../CONTEXT.md) and used here verbatim.

engram is a **domain-general, Obsidian-style indexer for markdown knowledge bases**: it turns a
[knowledge base](../../../CONTEXT.md) (a folder of Markdown notes, the single source of truth)
into queryable Postgres state, then serves **read-only hybrid retrieval** over that
state through one shared toolset exposed identically on the CLI, the AI Factory Studio HTTP
API, and an MCP server. The DB is a disposable derivative. The knowledge base is authoritative.

---

## The five bounded contexts and how data flows

The domain divides into **five bounded contexts** (the backbone all three views and the
concordance share). Data flows left-to-right across them, from raw paper to scored
retrieval answer:

1. **Ingestion**: `zotero` reads the local (read-only) Zotero library. `conversion`
   turns each attachment PDF/HTML into Markdown (the `HybridConverter`: arXiv-HTML
   first, Azure Document Intelligence OCR only as a fallback). `mirror` writes
   title-named **immutable** copies into `Sources/`. `sync` reconciles the whole set
   on `engram sync`. A [Mirror](../../../CONTEXT.md) is never edited. *(Research-profile
   only, a domain-general knowledge base may have no Zotero at all.)* See arc42 section 3
   ([sections/03-ingestion-bounded-context.md](../../architecture/sections/03-ingestion-bounded-context.md))
   and ADRs [0002](../../adr/0002-disposable-mirror-zotero-source-of-truth.md),
   [0003](../../adr/0003-hybrid-html-first-conversion.md). Reference
   [sync.md](../../reference/sync.md).

2. **Knowledge**: an author writes typed **WikiNotes** (frontmatter + H2 Sections +,
   for evidence-bearing types, **Claims**). `index` then runs the five-stage
   pipeline (**parse to resolve to validate to load to embed**), projecting each WikiNote
   into exactly one **Page** (plus its **Sections** and **Claims**) and materializing
   typed **Edges**, all in one transaction. The indexed Page (not the note) carries
   the `card_text` and `embedding`. See arc42 section 4
   ([sections/04-knowledge-bounded-context.md](../../architecture/sections/04-knowledge-bounded-context.md))
   and reference [the indexing reference](../../reference/indexing.md).

3. **Retrieval**: the single `features/serve` slice is the **only** read path. It
   serves hybrid retrieval (BM25 + vector + graph) through the **shared retrieval
   toolset** over the read-only `api` surface. See arc42 section 5
   ([sections/05-retrieval-bounded-context.md](../../architecture/sections/05-retrieval-bounded-context.md))
   and reference [serve.md](../../reference/serve.md).

4. **Evaluation**: `evals` generates judge-free structural ground-truths from the
   knowledge base's own shape, runs the **Vibe RAG tuning loop** that tunes a unified
   `RetrievalPolicy` against them (propose-only), scores runs with recall@k /
   precision@k / MRR, and does Logfire-driven RCA. See arc42 section 6
   ([sections/06-evaluation-bounded-context.md](../../architecture/sections/06-evaluation-bounded-context.md))
   and reference [evals.md](../../reference/evals.md).

5. **Lifecycle & interchange**: `init` scaffolds `_meta/` and migrates the DB from a
   `KnowledgeBaseSchema`. `export` projects a collection into a read-only **OKF export directory**.
   The `plugin` packages engram as a Claude Code plugin you install into any knowledge base. See
   arc42 section 7
   ([sections/07-lifecycle-interchange-bounded-context.md](../../architecture/sections/07-lifecycle-interchange-bounded-context.md))
   and references [init.md](../../reference/init.md),
   [export.md](../../reference/export.md), [plugin.md](../../reference/plugin.md).

The end-to-end flow: **Zotero to Conversion to Mirror to (authored) WikiNotes to Index to
Pages / Sections / Claims / Edges in Postgres to Retrieval (serve) to Evals.** Every
index-state row also carries a `collection` so multiple knowledge bases coexist in one DB.

---

## The wikinote kernel: the shared grammar

The note grammar is not owned by either authoring or indexing. It is a **kernel**
both depend on. `src/wikinote/` encodes the page types, frontmatter fields, canonical
H2s, claims grammar, and vocabulary once, as a **parser and renderer** pair
over one set of domain models. The parser reads a `.md` file into those models. The
renderer writes a validated `.md` back from an authoring payload (`engram note build`).
Indexing reuses the same parser. Authoring tools reuse the same renderer and the same
verify checks. The knowledge base's controlled vocabulary is the **vocabulary index**, and the
indexing pipeline's validate stage checks every note's entity type, relation key, and facet value
against it. This shared kernel is why the authored WikiNote and its indexed Page
projection can never drift in their grammar. See arc42 section 4.

---

## The CQRS split: writes on the bus, reads straight to the gateway

engram runs a deliberate **command/query asymmetry**. Write-side intents
(`IndexKnowledgeBase`, `SyncMirrors`, `ExportOkf`, and the evals commands) are frozen
pydantic `Command`s dispatched through the in-process **CQRS bus** (`src/cqrs/bus.py`)
to exactly one handler each, with events fanning out FIFO **after** the handler's unit
of work commits.

The **read/query path does *not* touch the bus.** `search`, `fetch`, `graph_expand`,
and `find_paths` go through dedicated query handlers (`engram.retrieval.queries`)
and a read-only SQL gateway (`engram.retrieval.infrastructure.gateway`) straight to the `api` schema.
No command, no event, no unit of work. This asymmetry is intentional: a query has
nothing to commit and nothing to fan out, so routing it through the write machinery
would buy only ceremony. See arc42 section 2
([sections/02-container-component-overview-c4-l2.md](../../architecture/sections/02-container-component-overview-c4-l2.md))
and section 8 ([sections/08-cross-cutting-concepts.md](../../architecture/sections/08-cross-cutting-concepts.md)),
governed by [ADR-0002](../../adr/0002-disposable-mirror-zotero-source-of-truth.md)
(the root CQRS chain).

---

## The retrieval seams and the read-only surface

Retrieval is built on two pluggable seams, both chosen via settings:

- **Dialect seam (BM25)**: a `SearchDialect` Strategy Registry emits backend-specific
  lexical SQL so one codebase serves both the local Postgres (`pg_textsearch`, the
  default local/CI test bed) and ParadeDB (`pg_search`, opt-in). Only the lexical
  grammar differs. The vector legs, RRF fusion, and the `api.search` signature are
  identical on both. ADRs [0006](../../adr/0006-dual-dialect-bm25-seam.md),
  [0017](../../adr/0017-local-postgres-primary-testbed.md).
- **Graph backend**: multi-hop graph walks run over either the edge relation or
  Apache AGE, selected the same way. Both walk over the set of reached nodes, one hop
  at a time, and differ only in how they read one hop.
  [ADR-0007](../../adr/0007-age-multihop-dual-graph-backend.md),
  [ADR-0049](../../adr/0049-graph-walks-keep-the-set-of-reached-nodes.md).

Every query runs under the read-only **`kb_reader`** Postgres role against the **`api`
surface** only (views + functions, every object COMMENTed). The base `kb.*` tables are
never granted to the reader, and the application refuses to start when the reader
connection can do more. A private knowledge base is read only through a login role of
its own ([ADR-0046](../../adr/0046-private-knowledge-base-read-through-its-own-login-role.md)),
and every `run_sql` statement is checked before it runs
([ADR-0047](../../adr/0047-the-agents-sql-is-checked-before-it-runs.md)). There is
**one sanctioned exception**: `StatusHandler` reads `kb.*` row counts and the embed-meta
row through the admin gateway over the admin engine (`engram.retrieval.queries`),
because those counts are not exposed through `api`. Every other read honours the
reader-role boundary. See arc42 section 5.

---

## Incremental index, in one paragraph

`index` is incremental by default. Each run re-hashes every file and diffs against a
persisted per-collection **index manifest** to get the raw `{added, removed,
content-changed}` partition. A **content_hash** ("did anything in this file change?")
gates whether a file is re-parsed. An **identity_hash** (over the normalized
resolution-key set) gates the **inbound closure** (the unchanged pages that resolve
links to an affected name, found via the persisted link-reference graph). The minimal
**dirty set** (`content-changed ∪ added ∪ inbound-closure ∪ mirror-citers`) is the only
thing indexed again. A global **index fingerprint** mismatch (schema / vocabulary /
embed-model / indexer change) automatically falls back to a `--full` index, which CI always
runs. The output is provably bit-for-bit identical to a full index. The full SDD is
the per-feature exemplar package [incremental-ingestion](../incremental-ingestion/index.md).
The decision of record is
[ADR-0019](../../adr/0019-true-incremental-ingestion.md).

---

*Companion views: a [Business view](business-view.md) and a [Research view](research-view.md)
of this same domain, plus an [index](index.md) carrying the concept concordance. Structure
lives in the [arc42/C4 master](../../architecture/index.md). The why lives in
[vision.md](../vision.md), [product-decisions.md](../product-decisions.md), and the ADRs.
Glossary: [CONTEXT.md](../../../CONTEXT.md).*
