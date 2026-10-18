---
id: engram-adr-0027-stratified-salted-hash-seed-selection
aliases: ["engram-adr-0027-stratified-salted-hash-seed-selection"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0027 — Stratified salted-hash seed selection (Vibe RAG Phase 5)"
---

# ADR-0027 — Stratified salted-hash seed selection (Vibe RAG Phase 5)

**Status:** Accepted (2026-07-03). Designed in the vibe-rag-phase5-coverage-scaling planning
session; to be built on `feat/vibe-rag-phase5-coverage-scaling` (Vibe RAG **Phase 5**,
coverage-based scaling). Extends the [ADR-0016](0016-structural-eval-generation.md) structural
generation pipeline; sibling decisions in [ADR-0028](0028-immutable-dataset-versions-and-case-identity.md)
(dataset identity) and [ADR-0029](0029-decision-core-subsampled-calibration.md) (calibration
subsampling). Scope: `SeedRepository`'s seed queries, a new coverage/allocation pair of pure
modules, the `engram evals coverage` verb, and `GenerationSettings`. The read path, the `api` SQL
surface, and scoring are unchanged. No new DB tables.

**Context.** The Phase-1 seeders assumed a small corpus and quietly encoded two defects that scale
would amplify rather than create. First, **biased truncation**: `pinpoint_seeds` takes the first N
claims by `ORDER BY id` (an alphabetical bias), and the survey/trace/synthesis seeders keep the
first N groups in edge-discovery order — the same low-sorting pages seed every dataset, and corpus
regions that sort late are never sampled at any size. Second, **eager materialization**:
`_collection_edges` loads *every* edge of the collection into Python before grouping, and
`_trace_paths` enumerates every 2-hop path — O(edges) memory and O(paths) time, a wall at thousands
of documents. "Coverage-driven seed selection" must therefore both define what coverage *is*
(judge-free, from corpus structure alone — the locked Vibe RAG discipline) and move selection to
where the combinatorial space never materializes.

**Decision.** Coverage is a stratification over corpus structure, and selection is a deterministic
stratified sample pushed into SQL:

- **The stratum is `(archetype × derived difficulty band × anchor kind)`.** All three are already
  structural: archetype is the seeder's shape, difficulty derives from the seed's shape via the
  existing `derive_difficulty` thresholds, and the **anchor kind** rule makes the multi-kind gold
  set single-valued (pinpoint → the claim's page, contrast → the comparison page, survey → the hub,
  trace → the path start, synthesis → the fanning page). Facet values (`tasks` / `domains` /
  `stages` / …) are **report-only** dimensions — visible in the coverage report, never
  quota-enforced. Graph communities and embedding clusters were considered and rejected for v1
  (an algorithmic dependency, non-determinism risk, and semantic redundancy with kind + facets).
- **Quotas allocate hierarchically over feasible cells.** `target_distribution` keeps owning the
  archetype level unchanged (back-compat); within an archetype, the quota spreads **uniformly
  across feasible (difficulty × anchor-kind) cells, capped by each cell's seed pool**, with
  undersubscribed remainder redistributed to cells with headroom. Several cells are structurally
  impossible (pinpoint is single-source, trace is fixed at two hops) — the allocator computes over
  *feasible* cells only, and the coverage report's **feasibility manifest** lists the impossible
  ones so a zero is never misread as a gap. Uniform-across-feasible was chosen over
  proportional-to-inventory deliberately: mirroring corpus density reproduces the old bias with
  extra steps, whereas coverage means thin regions are sampled relatively *more*.
- **Sampling is a salted-hash draw in SQL.** Within a stratum, rows/groups are ordered by
  `md5(stable_key ‖ salt)` with the per-stratum `LIMIT` — deterministic and reproducible from
  `(corpus, salt, budget)`, no low-id bias, and executed inside Postgres so enumeration never
  materializes in Python (survey/synthesis become `GROUP BY … HAVING count ≥ threshold` with hashed
  ordering; trace becomes a **staged draw** — sample first-hop edges by hash, extend only those,
  cap per-node degree — because a naive 2-hop self-join with `ORDER BY md5(...)` would still
  enumerate every path inside the database). Difficulty-band predicates mirror
  `derive_difficulty`'s integer thresholds as bound parameters; a property test asserts SQL-band ==
  `derive_difficulty(shape)` parity on every drawn seed.
- **`engram evals coverage` is the human surface.** Read-only, CLI + artifact only: without a
  dataset it reports the **stratum inventory** (what the corpus offers per stratum); with a dataset
  it audits actual coverage against inventory — gaps, over-representation, the feasibility
  manifest, the dataset version chain, and a single trendable **coverage-debt** scalar (weighted
  distance from the target distribution + share of feasible strata at zero).

**Consequences.** (+) Seed selection cost is bounded by the quota, not the corpus — the
combinatorial space is sampled, never enumerated. (+) Datasets stop over-representing low-sorting
ids and early-discovered hubs at *any* corpus size. (+) A draw is reproducible bit-for-bit from the
recorded salt, and salted-hash order is stable per key, which [ADR-0028](0028-immutable-dataset-versions-and-case-identity.md)
exploits for minimal-churn retirement. (+) Coverage stays judge-free — measured from structure,
never LLM-scored — extending the ADR-0016 inversion from scoring to sampling. (−) The seed queries
get meaningfully more complex (grouped, hashed, staged), and the SQL difficulty predicates must be
kept in parity with `derive_difficulty` (mitigated by the property test). (−) Uniform-across-
feasible under-weights dense strata relative to their real query traffic; if that ever matters, the
rejected per-cell weights option is the escape hatch. (−) Adaptive, failure-driven quotas (sample
more where calibration fails) were explicitly rejected: although judge-free, they couple the
generation loop to the calibration state — a soft Goodhart channel.
