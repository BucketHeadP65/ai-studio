---
id: engram-adr-0029-decision-core-subsampled-calibration
aliases: ["engram-adr-0029-decision-core-subsampled-calibration"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0029 — Decision-core subsampled calibration with a paired-bootstrap guard (Vibe RAG Phase 5)"
---

# ADR-0029 — Decision-core subsampled calibration with a paired-bootstrap guard (Vibe RAG Phase 5)

**Status:** Accepted (2026-07-03). Designed in the vibe-rag-phase5-coverage-scaling planning
session; to be built on `feat/vibe-rag-phase5-coverage-scaling` (Vibe RAG **Phase 5**). Extends
the [ADR-0018](0018-calibration-loop.md) calibration loop; sibling decisions in
[ADR-0027](0027-stratified-salted-hash-seed-selection.md) and
[ADR-0028](0028-immutable-dataset-versions-and-case-identity.md). Scope:
`calibration/calibration.py` (core/tail carving), `calibration/objective.py` (the accept guard),
and `CalibrationSettings`. The propose-only delivery boundary, the proposer seam, and the winner-
on-validation rule are unchanged.

**Context.** `run_eval_on_dataset` scores **every** case, and a calibration run does so up to
`max_rounds` times plus baselines — at thousands of cases the loop's cost is dominated by eval
rounds, not proposals. Subsampling is the obvious lever, but accept-iff-better compares a
candidate against an incumbent, and that comparison is only meaningful when both are measured on
the same cases; a fresh subsample per round would either double per-round cost (re-measuring the
incumbent) or silently compare across different case sets. Worse, the shipped acceptance rule is a
fixed scalar `min_gain` on the round mean — on a ~100-case subsample a 0.01 "gain" is inside
sampling noise, which is precisely the Goodhart failure the eval-loop design warns against
(difficulty explains 19–91 % of score variance; the design calls for variance-sized margins but
ships none).

**Decision.** Calibration rounds run on a **fixed decision core plus a rotating tail**, and
acceptance is guarded by a **paired bootstrap**:

- **The decision core.** One stratified subset of the train split — stratified over
  `(archetype × difficulty)`, drawn by the ADR-0027 salted-hash rule, size a `CalibrationSettings`
  knob — is carved once per calibration run. **All accept-iff-better decisions in the run are
  measured on this same core**: the incumbent is measured once and every comparison is
  apples-to-apples across rounds. The core's salt is **fresh per run** by default (recorded in the
  run artifact, overridable for reproduction), so successive calibration runs cannot slowly
  overfit one frozen subset. The name is deliberately **not** "golden core" — that term stays
  reserved for the eval-loop's approval-governance concept.
- **The rotating tail.** Each round additionally scores a fresh stratified sample of non-core
  train cases (fresh salt per round). The tail **never** contributes to acceptance; it exists so
  `diagnose()` sees new failure examples every round (the proposer doesn't fixate on the core's
  worst cases) and so a core-vs-tail metric divergence flags core overfitting in the report.
  Exploration signal and decision signal are separated by construction.
- **The paired-bootstrap guard.** A candidate is accepted iff its mean gain on the primary metric
  clears `min_gain` **and** the bootstrap confidence interval over the per-case
  (candidate − incumbent) deltas on the core stays above zero. Pairing per case removes the
  between-case variance that dominates the signal; the computation is pure arithmetic over
  per-case scores the report already carries — no new model calls, judge-free. An all-zero delta
  vector (no case moved) is a rejection: no evidence is not improvement. A **min-n-per-stratum
  floor** keeps the core honest: a stratum below the floor folds into its parent difficulty band
  and is flagged in the report, never silently trusted.
- **The winner rule survives unchanged.** The stratified validation split is carved from the full
  dataset as today (stratification extended from archetype-only to archetype × difficulty), stays
  untouched by core/tail sampling, is scored only on accept events, and still picks the run's
  winner — the overfitting guard of ADR-0018 is composed with, not replaced.

**Consequences.** (+) Per-round cost is bounded by `core + tail` size, not dataset size — the loop
scales to arbitrarily large datasets at a chosen, fixed round budget. (+) Acceptance is
statistically honest on the subsample: variance-sized margins land (closing the eval-loop §13
gap) at zero extra model cost. (+) Cross-round comparability and the patience/trajectory signals
are preserved because every decision reads the same core. (−) A fixed core is a smaller yardstick
than the full set — a genuinely better policy whose gains live outside the core can be rejected in
that run (mitigated by the fresh-salt-per-run rotation across runs and the tail's divergence
signal). (−) Two more sampling knobs (core size, tail size) plus bootstrap parameters join the
settings surface. (−) The fresh paired-subsample-per-round alternative (honest within a round,
noisy across rounds, 2× cost) and the bound-the-dataset alternative (fidelity stops growing with
the corpus) were rejected.
