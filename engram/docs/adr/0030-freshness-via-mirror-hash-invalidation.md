---
id: engram-adr-0030-freshness-via-mirror-hash-invalidation
aliases: ["engram-adr-0030-freshness-via-mirror-hash-invalidation"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0030 — Freshness via mirror-hash invalidation (Vibe RAG Phase 6)"
---

# ADR-0030 — Freshness via mirror-hash invalidation (Vibe RAG Phase 6)

**Status:** Accepted (2026-07-04). Designed in the vibe-rag-phase6-freshness planning session; to
be built on `feat/vibe-rag-phase6-freshness` (Vibe RAG **Phase 6**, the roadmap's last row). Builds
the consumer of the `AnchorHash` seam [ADR-0016](0016-structural-eval-generation.md) baked into
generated cases; sibling decision in [ADR-0031](0031-staleness-aware-dataset-maintenance.md) (what
maintenance does with the verdicts). Scope: a new `features/evals/generation/freshness.py` (the pure
verdict core plus a colocated SELECT-only corpus reader `SqlFreshnessReader` and the report
serializers, the way `coverage.py` colocates `StratumInventoryReader`) and `mirror.py` (the shared
mirror recipe), the seed builders' body hydration, a `mirror_hash` on `QaCaseMeta`, the read-only
`engram evals freshness` verb (a `CheckFreshness` command routed to a `CheckFreshnessHandler` in
`handlers.py`, mirroring `ComputeCoverage`), and a recorded `dataset_staleness` on the calibration
and verify-advisory reports. No new DB tables or columns; datasets stay file artifacts.

**Context.** The corpus drifts under a golden dataset: pages are edited, renamed, deleted. A case
whose sources moved keeps scoring calibrations as if its ground truth were still true — the baseline
is silently poisoned. The shipped seam is weaker than the roadmap line suggests. First, anchors are
**page-level and card-text-level**: `AnchorHash.body_hash` is `content_hash(card_text)` — the
`"{kind}: {title} — {summary}"` header plus a facet line — so section and claim edits (the very text
pinpoint quotes are verified against) never move an anchor, while a summary tweak does. Claims have
no hash column at all. Second, **support quotes are dropped at the gate** (`accept()` verifies then
discards them), so shipped cases can never be re-verified quote-by-quote. Third, **`PapersChanged`
is an incomplete signal**: both compile paths emit only `kind == "paper"` ids, but gold ids span
every page kind, and an event consumer misses compiles that run while evals is cold. Fourth, a
latent defect discovered during planning: **non-pinpoint seeds have carried empty `source_bodies`
since Phase 1** — the drafter saw no passages and the faithfulness gate (which never passes on an
empty mirror) rejected every contrast / survey / trace / synthesis draft, so real datasets only ever
contained pinpoint and legacy cases; unit fakes always fabricated bodies, hiding the hole.

**Decision.** Staleness is a judge-free snapshot comparison, precise where the case's era allows it:

- **Detection is a read-only sweep, not an event consumer.** `engram evals freshness` (a
  `CheckFreshness` command through the bus, mirroring `ComputeCoverage`) loads a dataset version —
  the `.current` pointer by default, an explicit version for historical baselines — and diffs its
  baked anchors + gold ids against the current `wiki.*` rows. No `PapersChanged` or
  `compile_manifest` dependency: correct regardless of when compiles happened. It writes a
  `FreshnessReport` artifact (per-case verdicts, per-class counts, a **staleness fraction**, and
  per-page attribution) under `evals/freshness/`, CLI + artifact only. Folding into `evals
  coverage` (muddles representativeness with validity) and a compile-time subscriber (incomplete
  signal, cold-evals hole) were rejected.
- **The seam upgrade: hydrate the mirror, then hash it.** The four non-pinpoint seed builders
  hydrate `source_bodies` from their gold pages' card texts (the existing `_page_bodies` helper),
  fixing the empty-mirror defect. `accept()` then stamps each kept case with `mirror_hash =
  content_hash` of the exact joined mirror its quotes were verified against, a new optional
  `QaCaseMeta` field. One shared assembly home (`generation/mirror.py`) is used by both the
  generation path and the sweep-time recompute, so the two recipes cannot drift; the sweep rebuilds
  a case's mirror from `(archetype, gold_ids, seed_key)` alone — every seed-key format is parseable
  and pinpoint's embeds its claim id. Per-unit anchors (per-type recompute recipes, no stored claim
  hash to compare against) and persisting support quotes (bulkier artifacts, still unavailable for
  shipped cases) were rejected.
- **Verdicts are graded and era-authoritative.** Three case eras exist: **legacy** (positional
  names, no anchors), **page-anchored** (Phase 5), **mirror-anchored** (Phase 6+). The verdict
  ladder is `gold_gone` (a gold page left the corpus) > `mirror_stale` (mirror hash mismatch;
  mirror era only) > `page_drifted` (an anchored card hash moved) > `fresh`, with `unanchored`
  reported for legacy cases. Each case is judged by the strongest signal its era supports —
  `page_drifted` with an intact mirror means the drafting basis did not move and is report-only.
  Freshness therefore measures **drafting-basis drift**, not arbitrary content drift, and says so.
- **Consumers warn and record, never gate.** `calibrate` and `verify-advisory` run the cheap check
  when loading a dataset version and record `dataset_staleness` (the stale fraction) in their
  reports, printing a warning when nonzero. No threshold gate and no `--force`: the propose-only
  discipline leaves the decision with the human, and verify-advisory legitimately re-runs stale
  baselines for apples-to-apples diffs. A refusal gate was rejected.

**Consequences.** (+) Staleness detection is complete and timing-independent — it cannot miss a
compile, a non-paper gold page, or a cold-evals window. (+) The empty-mirror defect is fixed: four
of five archetypes generate real cases for the first time, and every future case carries a precise,
judge-free invalidation signal. (+) A conclusion computed on drifted ground truth carries the number
with it. (−) Shipped cases stay page-level forever — their quotes are unrecoverable and their
verdicts conservatively coarse (summary tweaks read as drift); precision arrives only as datasets
roll into the mirror era via top-up. (−) The mirror recipe is a parity liability like the SQL
difficulty predicates — mitigated the same way, by a single shared home. (−) Non-pinpoint mirrors
are card texts, so section-body edits still move nothing even in the mirror era; that is the honest
boundary of "drafting-basis drift" (the quotes came from card texts), not a detection hole.
