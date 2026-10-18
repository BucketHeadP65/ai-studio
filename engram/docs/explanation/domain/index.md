---
id: engram-domain-index
aliases: ["engram-domain-index"]
type: explanation
app: engram
status: current
tags: [domain, index]
title: "engram: one domain, three views"
---

# engram: one domain, three views

engram is a **domain-general, Obsidian-style indexer for markdown knowledge bases**: it turns a
`KnowledgeBase` of Markdown notes (the single source of truth) into queryable Postgres
state, then serves **read-only hybrid retrieval** over that state to humans and AI
agents alike. This package narrates that **one domain three ways**, for three
audiences:

- a [Business view](business-view.md): the value and the guarantee, in plain words,
- a [Research view](research-view.md): engram as a research instrument over a literature knowledge base,
- a [Software Engineering view](engineering-view.md): the bounded contexts, CQRS split, and retrieval seams.

**Why three views.** Different readers, one truth. A stakeholder, a researcher, and
an engineer each need the story in their own language, but it has to be the *same*
story, not three approximations that quietly disagree. This index exists to prove
they don't: the [concept concordance](#concept-concordance) below puts each binding
concept side by side in all three framings, with a single canonical home for the
fact. Reading a row across proves one domain, not three. **Diátaxis: explanation.**

## Shared domain: one glossary, five contexts

All three views speak the **engram domain language verbatim**. The binding terms
(`KnowledgeBase`, `WikiNote`, `Page`, `Section`, `Claim`, `Edge`, `Dispute`,
`VocabularyIndex`, `Source (raw)`, `Mirror`, `Conversion`, `Index`, `Retrieval
ladder`, `RetrievalPolicy`, `Vibe RAG`) are defined **once** in the
[engram glossary](../../../CONTEXT.md) and used unchanged across every view. None of
them redefines a term. Each applies its own *lens* to the same entities. When in
doubt, the glossary wins.

The domain divides into **five bounded contexts**, the backbone all three views and the
concordance share, with data flowing left-to-right across them:

1. **Ingestion**: `zotero` (read-only library), `conversion` (PDF/HTML to Markdown), `mirror` (immutable `Sources/`), `sync` (Zotero to knowledge base reconcile).
2. **Knowledge**: the `wikinote` kernel (the shared note grammar: parser and renderer, schema, vocabulary, verify) + `index` (parse to resolve to validate to load to embed, incremental by default).
3. **Retrieval**: `serve`, the one read path: hybrid BM25 + vector + graph over a single shared toolset on CLI / HTTP / MCP.
4. **Evaluation**: `evals`: judge-free structural ground-truths + the Vibe RAG tuning loop + metrics / RCA.
5. **Lifecycle & interchange**: `init` (knowledge base scaffold + DB), `export` (OKF export directories), `plugin` (engram as a Claude Code plugin).

The structure of these contexts (the C4 container/component diagrams, the per-slice
domain-model class diagrams, and the one system ERD) lives in the
[arc42/C4 master](../../architecture/index.md). The views narrate. The master owns
structure.

## Reader's guide

| You are a… | Start with | It gives you |
|---|---|---|
| Non-technical stakeholder | [Business view](business-view.md) | the value, the cost, and the trust guarantee, no schema |
| Researcher / knowledge base author | [Research view](research-view.md) | engram as a research instrument: claims, edges, disputes, Vibe RAG |
| Engineer / reviewer | [Software Engineering view](engineering-view.md) + the [arc42/C4 master](../../architecture/index.md) | the bounded contexts, the CQRS split, the retrieval seams, and the structure beneath |

New to all three? Read the [Business view](business-view.md) first for the shape,
then follow your role.

## The core idea, for any reader

You keep your knowledge as an ordinary folder of Markdown, a `KnowledgeBase`, the single
source of truth. engram **indexes** that knowledge base into a database: it reads the notes,
resolves the links between them, checks them, and loads a searchable knowledge graph
where every stored statement (a `Claim`) keeps a verbatim, machine-checked citation
back to its `Source`. Documents enter as immutable `Mirror`s (PDFs and web pages
converted to clean text). The indexed Postgres state is a disposable, rebuildable
derivative. Humans and AI agents then query that state **read-only** (by meaning, by
keyword, and by how facts connect) and an evaluation loop grades the retrieval
against ground-truths read off the knowledge base's own structure.

```mermaid
flowchart TD
    src([Sources: PDFs / web pages]) --> mirror[Convert to immutable<br/>**Mirror**s in Sources/]
    mirror --> knowledge_base([**KnowledgeBase**<br/>Markdown notes you author])
    knowledge base --> indexing([studio **index**])
    indexing --> db[(Queryable<br/>**Postgres** state:<br/>Pages, Claims, Edges)]
    db --> retrieval[/Read-only **retrieval**<br/>CLI, the Studio, AI agents/]
    db -.checked by.-> evals[**Evals**: ground-truths<br/>from the knowledge_base's own structure]
    evals -.tunes.-> retrieval
```

This is the accessible shape of the whole domain. The
[arc42/C4 master](../../architecture/index.md) gives the precise container,
component, and ERD structure, and the [Software Engineering view](engineering-view.md)
narrates the data flow across the five contexts.

## Concept concordance

The centrepiece. Each row is one binding concept from the
[glossary](../../../CONTEXT.md). The three middle columns phrase that **same** concept
in each view's language, and the last column names its single canonical home. Reading
a row across proves the three views describe one domain, not three.

| Concept | Research (R&D) framing | Business framing | Software-Engineering framing | Canonical home |
|---|---|---|---|---|
| **Knowledge base** | The body of papers and notes you author and curate, the knowledge base, queryable as evidence rather than re-read as text. | The asset of record. Everything downstream is a disposable, rebuildable derivative of it. | A folder of Markdown (`Wiki/**/*.md` notes + `Sources/**/*.md` mirrors) at `knowledge_base_root`. The DB is a derived projection. | [glossary `KnowledgeBase`](../../../CONTEXT.md) |
| **WikiNote (authored) vs Page (indexed)** | What you write (a typed `Wiki/**/*.md` note, frontmatter + Sections + Claims) vs what the indexer projects it into for querying. | The thing you edit vs the fast, rebuildable copy of it the tools read. Editing the note, never the copy, keeps one source of truth. | One parsed `.md` file (`WikiNote`) indexes into **exactly one** `kb.pages` row (`Page`) plus its Sections and Claims. `card_text` + `embedding` live on the Page, never the note. | [arc42 section 4](../../architecture/sections/04-knowledge-bounded-context.md), [glossary `WikiNote`/`Page`](../../../CONTEXT.md) |
| **Section** | A canonical H2/H3 part of a note, the L2 "read this section" unit when a card scan isn't enough. | A labelled part of a document, so an answer can point at *where* in a note it came from. | An H2 or H3 leaf section's `kb.sections` row (`page_id`, `heading`, ltree `path`, `ord`, `body`, `embedding`). The ladder's L2 read unit. | [arc42 section 4](../../architecture/sections/04-knowledge-bounded-context.md), [glossary `Section`](../../../CONTEXT.md) |
| **Claim** | The atomic unit of knowledge: one block-anchored, provenance-pinned statement carrying `loc::` + a verbatim `quote::` machine-verified against the mirror. | A single stored fact that always carries its source, the receipt behind every answer. | A top-level `## Findings` bullet ending in `^cN` with `key:: value` fields (`loc::` required, `quote::` required for evidence kinds). The grounding tether. | [vision.md section 6](../vision.md), [glossary `Claim block`](../../../CONTEXT.md) |
| **Edge** | Typed, first-class relationships (`supports`, `refutes`, `outperforms`, `builds_on`, …), the serving graph a multi-hop query walks. Comparative edges need dataset+metric qualifiers. | How facts connect, stored as data, so you can ask "what builds on what" and trace it, not guess it. | A resolved `kb.edges` row (`src_id` to `rel` to `dst_id`, jsonb `qualifiers`, nullable `provenance_claim`). One per registered frontmatter / Claim relation, walked by the CTE or AGE backend. | [arc42 section 4](../../architecture/sections/04-knowledge-bounded-context.md), [glossary `Edge`](../../../CONTEXT.md) |
| **Dispute** | Disagreement as **data**, not deletion: two claims sharing subject+object+dataset+metric in opposite directions both get flagged. It is a claim **status**, not a graph hop, the closest *traversable* edge of disagreement is `refutes`. | A flagged contradiction surfaced instead of silently blended away, you see the conflict, you don't inherit a smoothed-over guess. | A claim-level lifecycle mechanic: `status:: disputed` + resolved `disputed_by_ids` on the claim, **NOT** a `kb.edges` row. Surfaced via `api.contradictions`. | [vision.md section 8](../vision.md), [glossary `Dispute`](../../../CONTEXT.md) |
| **Vocabulary** | The indexed controlled vocabulary along four axes (`entity_types`, `relations`, `facets`, `claim_types`) so a facet `WHERE` clause means one thing across the knowledge base. **Datasets/tasks/metrics are `entity` Pages, not axes.** | The closed-but-extensible vocabulary that keeps the knowledge consistent: new terms are *registered*, never bolted on by redesign. | `VocabularyIndex` (`src/engram/kb/domain/vocabulary.py`) parsed from `_meta/vocabulary/*.md`. The validate stage checks every note's type/relation/facet against it, upserted into `kb.vocab` for the `api` surface. | [arc42 section 4](../../architecture/sections/04-knowledge-bounded-context.md), [glossary `VocabularyIndex`](../../../CONTEXT.md) |
| **Source (raw) / Mirror** | The provenance root: a Zotero item's fulltext, chunked into L4 verification spans (`Source raw`), with its converted body the immutable `Mirror` a claim's quote is checked against. | The original document, kept untouched, so every citation can be opened and the quote re-checked against it. | A `kb.sources_raw` row (PK `zotero_key`) + `kb.raw_chunks` (L4 spans). The `Mirror` is the immutable `Sources/` `.md` copy (`zotero_key` + `source_hash`), never edited. | [arc42 section 3](../../architecture/sections/03-ingestion-bounded-context.md), [glossary `Source (raw)`/`Mirror`](../../../CONTEXT.md) |
| **Conversion** | Turning a paper into checkable text: arXiv-HTML first (the author's exact prose, free), Azure Document Intelligence OCR only as a fallback. | Messy PDFs and web pages turned into tidy, readable text engram can work with. | `HybridConverter` (`features/conversion/hybrid.py`): the arXiv-HTML adapter first, Azure-DI OCR only on a `ConversionError`. Other exceptions propagate. | [arc42 section 3](../../architecture/sections/03-ingestion-bounded-context.md), [ADR-0003](../../adr/0003-hybrid-html-first-conversion.md) |
| **Index (+ incremental)** | The button that makes your latest edits queryable, and that costs proportional to the edit, not the knowledge base, yet stays identical to a full index. | The build step that converts the asset into a queryable product. Its speed sets how tight the author loop feels, with a guaranteed-correct full index as insurance. | `parse → resolve → validate → load (one txn) → embed`. Incremental by default via the `index_manifest` + `content_hash`/`identity_hash` dirty set, with a `index_fingerprint` mismatch forcing `--full`. | [arc42 section 4](../../architecture/sections/04-knowledge-bounded-context.md), [incremental-ingestion](../incremental-ingestion/index.md), the indexing reference at `../../reference/indexing.md` |
| **Retrieval ladder (card to section to claim)** | How an agent finds evidence without re-reading papers: L0 orient to L1 card scan to L1.5 hybrid search to L2 section to L3 views/graph to L4 raw-span verification. A *protocol*, not a tree to descend. | How the knowledge base answers questions (by meaning, by keyword, and by how facts connect) always with a citation you can open. | The retrieval contract served by `features/serve`. Collapsed hybrid search (BM25 ⊕ vector by RRF) can land directly on a claim. L4 re-verifies the verbatim quote. | [arc42 section 5](../../architecture/sections/05-retrieval-bounded-context.md), [reference/serve.md](../../reference/serve.md) |
| **api surface / kb_reader** | The frozen, read-only contract a query agent sees (never the raw tables) so retrieval can't mutate the knowledge base. | The guarantee that querying is always read-only: tools and AI assistants read the knowledge, they never change it. | The `api` schema (views + functions, every object COMMENTed) is the only thing granted to the `kb_reader` role (`read_only`, `statement_timeout=10s`). Base `kb.*` tables are never granted. A private knowledge base is read only through a login role of its own, a member of `kb_reader` ([ADR-0046](../../adr/0046-private-knowledge-base-read-through-its-own-login-role.md)). | [arc42 section 5](../../architecture/sections/05-retrieval-bounded-context.md), [reference/serve.md](../../reference/serve.md) |
| **Dialect seam (BM25)** | The lexical-search abstraction that keeps results comparable across backends, only the BM25 grammar differs. The vector and fusion legs are identical. | Portability without lock-in: the same retrieval runs on the default local engine or an alternative, with the same answers. | A `SearchDialect` Strategy Registry emitting backend-specific BM25 SQL: local Postgres (`pg_textsearch`, the default local/CI test bed) and ParadeDB (`pg_search`, opt-in). The `api.search` signature is identical on both. | [arc42 section 5](../../architecture/sections/05-retrieval-bounded-context.md), [ADR-0006](../../adr/0006-dual-dialect-bm25-seam.md), [ADR-0017](../../adr/0017-local-postgres-primary-testbed.md) |
| **Graph backend (CTE / AGE)** | The mechanism behind a multi-hop traversal (a `trace` over typed edges), selectable without changing what a query means. | The engine that walks "what connects to what", swappable so the system can scale or move to a managed host. | A multi-hop walk over the relational `cte` backend or Apache AGE, selected via settings (`graph_backend`). Both walk the same `kb.edges` graph one hop at a time over the set of nodes already reached, and `api.graph_reach` says how far a walk can go. | [arc42 section 5](../../architecture/sections/05-retrieval-bounded-context.md), [ADR-0007](../../adr/0007-age-multihop-dual-graph-backend.md), [ADR-0049](../../adr/0049-graph-walks-keep-the-set-of-reached-nodes.md) |
| **RetrievalPolicy** | The single tunable object (instructions + strategy + tool descriptions) the tuning loop optimizes against the structural ground-truths. | The dial the self-tuning loop turns to make answers better, applied only by an explicit human step. | The unified policy the `evals` tuning controller tunes. `engram evals tune` proposes a winning policy, `apply-policy` writes the working tree (never commits). | [arc42 section 6](../../architecture/sections/06-evaluation-bounded-context.md), [eval-loop.md](../eval-loop.md) |
| **Vibe RAG tuning loop** | Answering "is the retrieval any good?" without a circular LLM judge: ground-truths read off the knowledge base's own structure, then a propose-only loop that accepts a candidate only on a within-cost accuracy gain that generalizes on a held-out split. | The self-checking that lets engram grade and improve its own answers against questions whose correct answers are known in advance. | The `evals`-slice controller (judge-free structural seeds to `RetrievalPolicy` tuning, propose-only). CLI and MCP drive one shared core (Vibe RAG Phase 2). | [arc42 section 6](../../architecture/sections/06-evaluation-bounded-context.md), [vibe-rag.md](../vibe-rag.md), ADR-0018 (`../../adr/0018-calibration-loop.md`) |
| **The five bounded contexts** | The knowledge base lifecycle: bring papers in (Ingestion) to distil them into a knowledge graph (Knowledge) to query it as evidence (Retrieval) to measure the retrieval (Evaluation) to scaffold and share it (Lifecycle). | The end-to-end journey from raw documents to a trustworthy, shareable knowledge base your tools can use. | `Ingestion → Knowledge → Retrieval → Evaluation → Lifecycle & interchange`. Data flows left-to-right, every index-state row carrying a `collection` so knowledge bases coexist. | [arc42 section 2](../../architecture/sections/02-container-component-overview-c4-l2.md), [Software Engineering view](engineering-view.md) |

The honest edge cases the concordance preserves: a `Dispute` is a **claim status**,
not a `kb.edges` row (the traversable disagreement edge is `refutes`). A `WikiNote`
(authored) and a `Page` (indexed `kb.pages` row) stay distinct. And the
`VocabularyIndex` axes are **entity-types / relations / facets / claim-kinds**, not
datasets / tasks / metrics (those are `entity` Pages governed *by* the vocabulary, not
axes of it).

## See also

- [Business view](business-view.md), [Research view](research-view.md), [Software Engineering view](engineering-view.md): the three views of this package.
- [vision.md](../vision.md): the grand "index, don't re-retrieve" argument the whole domain rests on.
- [vibe-rag.md](../vibe-rag.md): the self-tuning-retrieval program. The tuning loop is its Phase 2.
- [incremental-ingestion](../incremental-ingestion/index.md): the per-feature exemplar of this three-views-plus-concordance pattern.
- [ADR-0020: Domain documentation architecture](../../adr/0020-domain-documentation-architecture.md): the decision of record for this layered entry point.
- [engram glossary](../../../CONTEXT.md): the shared vocabulary all three views use verbatim.
