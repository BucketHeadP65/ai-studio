---
id: engram-domain-research-view
aliases: ["engram-domain-research-view"]
type: explanation
app: engram
status: current
tags: [domain, research, vibe-rag, claims, retrieval]
title: "The engram domain: Research (R&D) view"
---

# The engram domain: Research (R&D) view

> **Badge:** Diátaxis (explanation). **One domain, three views.** This is the
> researcher's view of the *whole* engram domain. The same domain is told for
> non-technical stakeholders in the [Business view](business-view.md) and for engineers
> in the [Software Engineering view](engineering-view.md). A sibling [index](index.md)
> holds a concept concordance across all three. The binding terms
> (`KnowledgeBase`, `WikiNote`,
> `Page`, `Section`,
> `Claim`, `Edge`, `Dispute`, `VocabularyIndex`, `Source (raw)`, `Mirror`, `Retrieval
> ladder`, `RetrievalPolicy`, `Vibe RAG`) are defined once in the
> [engram glossary](../../../CONTEXT.md) and used here verbatim. This view **narrates
> and links down**. It carries **no diagrams** (those live in the architecture master).

This is the *why it matters for doing research* view of engram as a whole: a system that
turns a knowledge base of papers and notes into a **living knowledge graph over a body of
research literature**, queryable as evidence rather than re-read as text. The grand argument lives in
[vision.md](../vision.md). This view points you at the part of it that owns each idea.

---

## The thesis: index a knowledge base into queryable, provenance-pinned knowledge

Plain RAG re-reads raw papers on every query and never accumulates judgment. engram
applies the "index, don't re-retrieve" move (vision.md
[section 1](../vision.md#1-why-this-exists-the-grand-vision)): read the knowledge base once, carefully,
and distil what was learned into a structured graph, then let queries run against
it. For a researcher that graph is made of **Claims**, each one atomic,
block-anchored, and **provenance-pinned**: it carries a `loc::` (where in the source)
and a verbatim `quote::` machine-verified against an immutable `Mirror` of the original
`Source (raw)`. A claim whose quote is not literally in the source never enters the
knowledge layer. That is the anti-hallucination tether the whole research story rests on
(vision.md [section 6](../vision.md#6-claims-the-atomic-unit-of-knowledge)).

Three properties make this a *research instrument* and not just a search index:

- **Evidence compounds.** Every new paper updates the `entity` Pages of the techniques,
  datasets, and metrics it touches, one evidence bullet per claim. The unit of growth
  is "the knowledge base got smarter about LightRAG", never "another chunk got appended". Ask "how
  strong a baseline is naive RAG really?" and the answer is one page citing a dozen
  papers.
- **Typed Edges make relationships first-class.** A claim's evidential relations
  (`supports`, `refutes`, `outperforms`, `comparable_to`, `replicates`) and a paper's
  conceptual relations (`builds_on`, `evaluates`, `proposes`) index into typed `Edge`
  rows, the serving graph a multi-hop query walks. Comparative edges are invalid without
  dataset + metric qualifiers: "X beats Y" is not knowledge until it is "…on UltraDomain
  win-rate, judged by GPT-4o-mini, in hybrid mode". Null results (`comparable_to` with
  `kind:: negative-result`) are first-class, because the research goal is to *prune*
  strategies, and a tie is exactly the evidence that lets an agent advise against
  something.
- **Disagreement is data.** When two claims share subject + object + dataset + metric but
  point in opposite directions, the merge scan flags both. Resolution is never deletion:
  it is a `comparison` Page documenting the *conditions* under which each side holds
  (vision.md [section 8](../vision.md#8-disputes-disagreement-as-data)).

A controlled vocabulary keeps all of this queryable. The `VocabularyIndex` is the knowledge base's
indexed controlled vocabulary along four axes (`entity_types`, `relations`, `facets`,
and `claim_kinds`) so a facet `WHERE` clause means one thing across the whole knowledge base.
(Datasets, tasks, and metrics themselves are `entity` Pages, not registry axes. The
registries govern the *types and relations*, not the named things.) New vocabulary
arrives by registering rows, never by redesigning structure (vision.md
[section 7](../vision.md#7-vocabulary-closed-values-open-growth)).

> **Be precise about what is traversable.** A `Dispute` is a **claim status**, not a
> typed `Edge`. A disputed claim carries `status:: disputed` and a `disputed_by`
> link, stored as the claim's status plus resolved `disputed_by_ids`, **not** as a
> `kb.edges` row (CONTEXT.md, `Dispute`). So you cannot *traverse* a dispute the way
> you traverse `outperforms`. You query disputed pairs as a status (the served
> `api.contradictions` view surfaces both quotes). The closest traversable edge of
> disagreement is the `refutes` evidential relation. Treat "disagreement as data"
> (vision.md section 8) as a lifecycle-and-comparison-page mechanic, not a graph hop.

## WikiNote (authored) vs Page (indexed)

The researcher writes **WikiNotes** (one parsed `Wiki/**/*.md` file each): typed
frontmatter, canonical H2 `Section`s, and (for evidence-bearing types like a `paper`)
Claims. The `KnowledgeBase` of Markdown is the single source of truth. `Index` projects
each WikiNote into exactly one **Page** (a `kb.pages` row) plus its `Section`s and
`Claim`s, the queryable projection the read-only `api` surface serves. The indexed
Postgres state is a disposable derivative: rebuildable from Markdown at any time, and
kept in lock-step with the knowledge base by incremental reindexing. You author judgment. The
indexer makes it retrievable. The full pipeline (parse to resolve to validate to load to
embed) is owned by vision.md [section 11](../vision.md#11-indexing-from-knowledge-base-to-postgres),
with the contract in the indexing reference doc.

The freshness guarantee that makes the knowledge base a trustworthy yardstick (that an edit
costs proportional to the edit, not the knowledge base, yet stays bit-for-bit identical to a full
index) is the subject of the incremental-ingestion package (its
[research view](../incremental-ingestion/research-view.md), decision of record
[ADR-0019](../../adr/0019-true-incremental-ingestion.md)). That package is the
per-feature exemplar this whole-domain view sits above.

## Retrieval as evidence-finding

A query agent does not re-read papers. It walks the **Retrieval ladder**: L0 orient to L1
card scan to L1.5 hybrid search to L2 section read to L3 computed views / graph to L4 raw-span
verification (vision.md [section 12](../vision.md#12-retrieval-how-agents-query-the-indexed-knowledge-base)).
The ladder is a protocol an agent *may* use, not a tree it must descend: collapsed hybrid
search (BM25 fused with vector ranking by reciprocal-rank fusion) can land directly on a
claim row. For quantitative contrasts the agent reads a `comparison` Page's matrix as
data (each `Comparison cell` cites the backing claims) rather than improvising a verdict
from prose. Before *asserting* a number, the agent drops to L4 and confirms the verbatim
quote against the raw chunk of the mirror. Every answer ships with a citation a human can
open in Obsidian and an agent can re-verify mechanically. The retrieval surface itself is
domain-general and contract-frozen. The research framing is just the questions a
literature knowledge base invites.

## The eval loop and Vibe RAG as a research instrument

The hardest research question engram answers about *itself* is "is the retrieval any
good?", and it answers it without a circular LLM judge. **Vibe RAG** is engram's
self-tuning-retrieval program: it reads its evaluation ground-truths **off the
structure of the indexed knowledge base**, so the gold source ids are known by construction
before any model runs. A single claim is a *pinpoint* lookup. A comparison matrix is a
*contrast*. An edge fan-in is a *survey*. A multi-hop path is a *trace*. An out-fanning
page is a *synthesis*. Because the knowledge base's own structure seeds the eval cases, the
accuracy signal is judge-free and structural. See [vibe-rag.md](../vibe-rag.md) for the
program and its phase roadmap (0-7, all shipped), and vision.md
[section 15](../vision.md#15-roadmap-the-vibe-rag-program) for where it sits in the vision.

On top of those ground-truths runs the **tuning loop**: it tunes a unified
`RetrievalPolicy` (instructions + strategy + tool descriptions) and accepts a candidate
only when it improves a primary accuracy metric within a cost guardrail *and* generalizes
on a held-out validation split. It emits a winning policy and a report. Applying a winner
is a separate step: the human path writes the working tree and never
commits, and the loop's one machine path (`tune --apply-runtime`, ADR-0032) may
write only the knowledge base's `_meta/active-policy.yaml`, behind four guardrails anchored on a
frozen split. The loop's mechanics (the phases, the gates, the
metrics) live in [eval-loop.md](../eval-loop.md). The program-level framing lives in
vibe-rag.md. For a researcher this closes the scientific loop: a retrieval change is
*measured against the knowledge base's own structure*, not vibed.

## Where to read next

- **The grand vision**: the index-vs-retrieve thesis, the three layers, claims,
  registries, disputes, the roadmap: [vision.md](../vision.md).
- **The decision log**: every `D-n` choice with its as-built status and its engineering
  ADR: [product-decisions.md](../product-decisions.md).
- **The eval loop and the program**: [eval-loop.md](../eval-loop.md) (one cycle's
  mechanics) and [vibe-rag.md](../vibe-rag.md) (the self-tuning program).
- **Interchange**: projecting a collection into an OKF v0.1 export directory, see [okf.md](../okf.md).
- **The literature behind the design**: the research briefs:
  [Postgres search](../research/01-postgres-search.md),
  [hierarchical retrieval](../research/02-hierarchical-retrieval.md),
  [scholarly schemas](../research/03-scholarly-schemas.md),
  [agent SQL discovery](../research/04-agent-sql-discovery.md), and
  [the evaluation loop](../research/05-evaluation-loop.md).
- **The per-feature exemplar**: incremental ingestion told three ways:
  [its index](../incremental-ingestion/index.md).

---

*Companion views (this initiative): a [Business view](business-view.md) and a
[Software Engineering view](engineering-view.md) narrate the same domain for other
readers, and a domain [index](index.md) holds the concept concordance. Glossary:
[CONTEXT.md](../../../CONTEXT.md).*
