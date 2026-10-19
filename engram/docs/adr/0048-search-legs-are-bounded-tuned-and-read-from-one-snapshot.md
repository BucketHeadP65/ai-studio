---
id: engram-adr-0048-search-legs-are-bounded-tuned-and-read-from-one-snapshot
aliases: ["engram-adr-0048-search-legs-are-bounded-tuned-and-read-from-one-snapshot"]
type: adr
app: engram
status: accepted
tags: []
title: "0048: Search legs are bounded and tuned from one row, results follow rank policies, and a tool call reads one snapshot"
---

# 0048: Search legs are bounded and tuned from one row, results follow rank policies, and a tool call reads one snapshot

**Status:** Accepted (2026-10-11)

> **Amended by migration 0071 (2026-10-19):** decision 2's single row is now a default row, whose `collection` is null,
> and at most one row per knowledge base, keyed by its collection. A knowledge base's row may set only
> `max_hits_per_document` and `folded_snippet_chars` (the longest snippet a folded unit carries), and what it sets wins
> over the default row for that knowledge base's hits. Every other number still comes from the default row.
> `api.search_hits_per_document(collection)` answers one knowledge base's cap, and the default row's for a knowledge
> base the session cannot see. The serve reference (`engram/docs/reference/serve.md`, "Search legs are bounded and
> tuned") describes the table as it is now. The bounded legs, the rank policies and the snapshot, the substance of this
> ADR, are unchanged.

**Context.** `api.search` had no limit on any leg. Every hybrid search computed a distance and a rank for every embedded row
of every tier and handed all of them to the fusion. Its numbers were constants in code: the fusion constant 60, the `k`
ceiling 50, snippets of 480 characters, and no tier or leg had a weight. A long page's sections and claims could fill all
`k` rows, and a card whose title was the query could rank below them. The search tool dropped `meta.*` pages after `k` was
applied, so a search could answer fewer than `k` rows while saying it was complete. A search over several knowledge bases
merged hits by raw score, which is comparable only within one list, and the re-ranker overwrote `score` with a probability,
so a re-ranked base and a fused base mixed scales. Each gateway read opened its own transaction, and the re-ranker fetched
each hit in its own, so an index run landing between them mixed two versions of a knowledge base in one answer.

**Decision.**

1. Each leg stops at a budget of rows (migration 0059). A keyword leg keeps its best-scored rows. A semantic leg keeps its
   nearest rows by exact distance, ordered by `(embedding <=> query_vec) + 0`, which the planner cannot serve from the HNSW
   index, then ranks them by that distance.
2. Every number is read when the function runs from `kb.retrieval_tuning`, one row the function reads as its owner and no
   reader may read or write. Defaults and bounds are generated into the function beside each other: a budget of 1 to 200
   and never below `k`, a fusion constant of 1 to 1000, a `k` ceiling of at most 50, a snippet of 80 to 2000 characters, a
   weight of 0 to 10 per tier and leg, and `rows_per_group` of 1 to 50. The row's checks refuse a value that is not a number
   (`core.ai.retrieval.schema.SearchTuning`).
3. Each tier tests pool membership with a hashed lookup instead of a join, so a keyword leg reads its index in score order
   (migration 0060). On 8,783 sections the join had made hybrid search five times slower.
4. After the fusion two rules apply by rank, so the search tool and `run_sql` share them (migration 0061). A card whose
   title equals the query, ignoring case and surrounding spaces, goes first when no other candidate holds that title. Then
   each page, or each source for raw chunks, keeps its best `rows_per_group` hits, 3 by default.
5. `api.search` takes `include_meta`. The search tool passes false, so `meta.*` pages leave the pool before `k` is applied
   (migration 0058).
6. A hit keeps its fused `score` and gains `rerank_score` when a decision model re-ranks it. A search over several
   knowledge bases merges by `rerank_score` when every hit has one, else rank by rank (`engram.retrieval.capability`).
7. Every tool call reads inside `read_snapshot()`: one `REPEATABLE READ`, read-only transaction per engine and collection,
   shared by every read of the call. The re-ranker reads its hits and their texts first and calls `release_snapshot()`
   before the model call, so no transaction stays open through it.

**Consequences.**

- (+) A hybrid search does bounded work per leg, and its numbers change with one row update, no migration.
- (+) The agent can no longer read or change the tuning, and a value outside its bounds is held to them.
- (+) A search returns `k` rows when that many exist, and one page no longer crowds out the rest.
- (+) Scores keep their meaning across knowledge bases and through the re-ranker.
- (+) One answer reads one state of the knowledge base.
- (−) The semantic leg scans its pool exactly and never uses the HNSW index. At current scale the largest tier holds about
  9,000 embedded rows. The exact scan gives the true nearest rows with no recall loss and needs no planner settings, and a
  whole hybrid search over the 8,783 sections took 254 ms after migration 0060. When a pool grows into the tens of
  thousands, an index path with iterative scan, sorted again by exact distance, is the next step.
- (−) A pinned title or a group cap can move a hit a caller expected higher. Both are tuning values, so a knowledge base
  can loosen them.
- **Reversible?** Yes. Each migration's downgrade restores the previous function, and 0059's drops the tuning row.

**Related.** [ADR-0035](0035-retrieval-sql-from-descriptors.md) (search generated from descriptors),
[ADR-0036](0036-behavioral-parity-generated-sql.md) (parity across dialects),
[ADR-0047](0047-the-agents-sql-is-checked-before-it-runs.md) (the model check on the semantic leg). Code:
`core/src/core/ai/retrieval/search_builder.py`, `core/src/core/ai/retrieval/schema.py`,
`engram/src/engram/retrieval/knowledge_base_schema.py`, `engram/src/engram/retrieval/rerank.py`,
`engram/src/engram/retrieval/infrastructure/gateway.py` (`read_snapshot`, `release_snapshot`),
`engram/alembic/versions/0058_search_include_meta.py` to `0061_search_policies.py`.
