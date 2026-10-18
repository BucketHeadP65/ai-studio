---
id: engram-adr-0033-frozen-split
aliases: ["engram-adr-0033-frozen-split"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0033: Frozen-split governance (Vibe RAG Phase 7)"
---

# ADR-0033: Frozen-split governance (Vibe RAG Phase 7)

**Status:** Accepted (2026-07-05). Designed in the vibe-rag-phase7-closeout planning session; to be
built on `feat/vibe-rag-phase7-closeout` (Vibe RAG **Phase 7**). Mechanizes the eval-loop design's
Iron Law (the frozen held-out set, [eval-loop.md](../explanation/eval-loop.md) §4/§13) over the
[ADR-0028](0028-immutable-dataset-versions-and-case-identity.md) immutable versions; the final gate
of the [ADR-0032](0032-runtime-policy-rung.md) machine rung; carve statistics per
[ADR-0027](0027-stratified-salted-hash-seed-selection.md), staleness per
[ADR-0030](0030-freshness-via-mirror-hash-invalidation.md). Scope: the `studio evals freeze` verb, a
versioned frozen-split sidecar under `evals/datasets/`, frozen-case exclusion plus an end-of-run
frozen scoring pass in `calibration/calibration.py`, and a `frozen_fraction` settings knob. No new
DB tables; sidecars are file artifacts.

**Context.** The Iron Law — "if a change can't beat the frozen held-out set by a measured margin,
it didn't happen" — has no mechanized referent. Every split that exists today is carved *by the
loop itself, per run*: the train/validation split (ADR-0018), the decision core and rotating tail
(ADR-0029). They guard within-run overfitting, but nothing survives across runs as a yardstick the
loop never optimizes against, and successive winners compared on freshly-sampled splits are not
strictly comparable. Propose-only could live with that — a human read the report. The ADR-0032
machine rung cannot: an auto-apply gated only by loop-sampled splits is graded by a yardstick the
optimization process influences, exactly the Goodhart channel the eval-loop design warns against.
The tension the design left open is frozen-vs-fresh: the corpus drifts (ADR-0030 measures it), so a
frozen yardstick monotonically stales.

**Options considered.**

- **Freeze a whole dataset version as the yardstick.** Rejected: runs train on the `.current`
  living version whose cases overlap the frozen one, so train and yardstick share cases unless the
  frozen version is retired from top-up. The overlap quietly violates never-optimize-against.
- **The full golden-core two-track promotion** (per-case approval ledger, candidate frontier,
  critique battery — eval-loop §5). Rejected for this phase as the heaviest path; it remains the
  roadmap's fuller shape and this sidecar is forward-compatible with it (an approval ledger could later
  subsume the carve).
- **Auto-roll stale frozen cases via top-up.** Rejected: a self-rolling yardstick is no longer
  frozen — the loop regains influence over what grades it.
- **A version-pinned sidecar carve that a human freezes again (chosen).**

**Decision.**

- **Freeze is a human verb that carves a sidecar.** `studio evals freeze --dataset <name>` (an
  explicit version overridable) draws a stratified subset of the named immutable version —
  `(archetype × difficulty)` strata, the ADR-0027 salted-hash rule, `frozen_fraction` (default
  `0.2`) with a min-n-per-stratum floor — into `<name>.frozen.v{M}.json`, with a
  `<name>.frozen.current` pointer (ADR-0028's own idiom). The sidecar records the pinned dataset
  version, the frozen case names, the carve salt, a freeze stamp, the staleness fraction at freeze
  time, and `parent_frozen_version` lineage.
- **Calibrate excludes and then grades.** Frozen case names are excluded from the train pool
  before the train/val split, the decision core, and the rotating tail are carved — the loop never
  trains on, proposes against, or accepts on frozen cases. At run end the baseline and the winner
  are each scored once on the frozen split (paired per case). The frozen delta is **reported on
  every run** and **gates only `--apply-runtime`**: the machine rung fires only when the winner
  beats the incumbent on the frozen split under the paired-bootstrap guard.
- **Frozen means frozen; staleness blocks the machine and summons the human.** The freshness sweep
  reports the frozen split's stale fraction like any dataset's; no auto-roll, no top-up touch.
  `--apply-runtime` refuses above `apply_staleness_max` and prints the instruction to freeze again. Human
  runs warn only (ADR-0030's discipline). Freezing again (a new carve from a fresh version) is a
  deliberate human act that resets the yardstick's era, recorded in the sidecar lineage. A dataset
  with no frozen sidecar simply cannot arm `--apply-runtime`.

**Consequences.** (+) The Iron Law is enforced at the cost of two extra scoring passes over ~20 %
of the dataset per calibration run, zero new model classes. (+) Winners across runs within one
frozen era are compared on an identical yardstick. (−) The frozen carve shrinks the usable train
pool by `frozen_fraction`. (−) A stale frozen split blocks the machine rung until a human freezes again
— deliberate: the alternative is grading on drifted ground truth. (−) One more pointer file to keep
honest. The freeze verb owns updating it and the coverage/freshness reports print the frozen chain.
**Reversible?** Yes: deleting the sidecars and the exclusion returns to per-run splits only.
