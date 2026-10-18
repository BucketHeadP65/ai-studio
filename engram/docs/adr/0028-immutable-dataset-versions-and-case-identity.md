---
id: engram-adr-0028-immutable-dataset-versions-and-case-identity
aliases: ["engram-adr-0028-immutable-dataset-versions-and-case-identity"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0028 — Immutable dataset versions and seed-derived case identity (Vibe RAG Phase 5)"
---

# ADR-0028 — Immutable dataset versions and seed-derived case identity (Vibe RAG Phase 5)

**Status:** Accepted (2026-07-03). Designed in the vibe-rag-phase5-coverage-scaling planning
session; to be built on `feat/vibe-rag-phase5-coverage-scaling` (Vibe RAG **Phase 5**). Sibling
decisions in [ADR-0027](0027-stratified-salted-hash-seed-selection.md) (seed selection) and
[ADR-0029](0029-decision-core-subsampled-calibration.md) (calibration subsampling); protects the
retained-dataset joins of [ADR-0026](0026-structural-advisor.md) `verify-advisory` and consumes
the [ADR-0019](0019-true-incremental-ingestion.md) changed-set. Scope: `datasets.py` (stamp,
versioned save/load, pointer), the `GenerateEvalDataset` command's top-up mode, and case naming in
the generation pipeline. No new DB tables; datasets stay file artifacts in `evals/datasets/`.

**Context.** At thousands of documents the corpus changes continuously (Phase 3 makes recompiles
cheap and emits a precise changed-set), so the golden dataset must be *maintained*, not
regenerated. But two shipped conventions make maintenance destructive. First, **case names are
positional**: `_case_name` is `{paper_id}-{index}` where `index` is the case's position in the
dataset — any insert or retirement shifts indices, silently renaming cases. Everything that joins
on case names — `verify-advisory`'s retained-baseline diff, `wiki.eval_case_results` run history,
calibration comparisons — quietly breaks. Second, **datasets are single mutable files**: an
advisory that snapshotted its baseline against `research.yaml` cannot re-run "the same dataset"
after a top-up rewrote it; retired cases become indistinguishable from gold-pages-gone drops, and
the apples-to-apples delta `verify-advisory` depends on degrades without warning.

**Decision.** Dataset maintenance is **coverage-gap top-up with deterministic retirement**, and
its artifacts are **immutable versions with seed-derived case identity**:

- **Stable case identity.** A case is named `{seed_key}#{draft_ordinal}` (e.g.
  `survey:builds_on:page.x#2`) — the seed's already-stable, already-unique key plus the draft's
  ordinal within that seed. Names survive inserts, retirement, and version chains; the `#`
  separator cannot collide with the seed key's own `:` / `>` characters, and names stay legible
  (a hash-based name was rejected as opaque). Pre-coverage datasets keep their legacy positional
  names — old run history is unbroken; the new scheme applies from the first coverage-stamped
  version.
- **Immutable versions with a stamped lineage.** A dataset is a sibling-file chain —
  `<name>.v{N}.yaml` — and each version carries a **dataset stamp**: the sampling salt, the corpus
  fingerprint, the case budget, a coverage summary, `parent_version`, and the **retirement
  ledger**. A one-line `<name>.current` pointer file names the live version. Top-up never mutates
  an existing file, so a calibration or advisory that recorded which version it ran on can always
  re-run exactly that baseline. Timestamped-plus-manifest and content-hash naming were rejected
  (lineage drifting outside the artifact; unreadable filenames).
- **Top-up generates only the gap and retires deterministically.** `engram evals gen-dataset
  --top-up <parent>` recomputes the stratum inventory, drafts cases only for under-covered strata
  (narrowing the seed draw to the Phase-3 changed-set pages where they are the gap), and
  re-applies the salted-hash selection under the new inventory: cases whose seeds are no longer
  selected **retire** into the ledger. Because hash order is stable per key, retirement is
  consistent-hashing-like — only cases displaced by higher-ranked newcomers churn, the rest of the
  dataset is untouched. `--dry-run` prints the per-stratum generate/retire plan without writing.
  Append-only-with-weights (unbounded growth, weighted metrics everywhere) and full regen (name
  churn on every corpus change) were rejected.

**Consequences.** (+) `verify-advisory` re-runs precisely its baseline dataset regardless of later
top-ups; retired-by-top-up and gold-gone are distinguishable by ledger. (+) Dataset cost tracks
corpus *growth*, not corpus *size* — only gaps are drafted. (+) The whole maintenance operation is
reproducible from `(corpus, salt, budget, parent)`. (−) Version chains accumulate files; pruning
is deliberately out of scope (a user-owned filesystem operation, not a verb). (−) Cross-boundary
history: `wiki.eval_case_results` rows referencing legacy positional names never join against
new-scheme names — accepted, documented, and confined to the one-time scheme change. (−) The
`.current` pointer is one more thing to keep honest; top-up owns updating it and `evals coverage`
prints the chain so drift is visible.
