---
id: engram-adr-0034-reflective-diagnosis
aliases: ["engram-adr-0034-reflective-diagnosis"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0034 — Reflective diagnosis: LLM-assisted, never scoring (Vibe RAG Phase 7)"
---

# ADR-0034 — Reflective diagnosis: LLM-assisted, never scoring (Vibe RAG Phase 7)

**Status:** Accepted (2026-07-05). Designed in the vibe-rag-phase7-closeout planning session; to be
built on `feat/vibe-rag-phase7-closeout` (Vibe RAG **Phase 7**). Builds the eval-loop design's
reflective-diagnosis rung ([eval-loop.md](../explanation/eval-loop.md) §8) onto the
[ADR-0018](0018-calibration-loop.md) reading half, in the shape the
[ADR-0026](0026-structural-advisor.md) advisor established (deterministic detectors → LLM drafts →
pure gate). Scope: additive error-type/routing fields on `rca/diagnosis.py`'s `Diagnosis`, a new
`rca/reflect.py` (the reflection agent, its gate, and the change-manifest models), the advisory
section in the proposer prompt, and prediction scoring in the calibration report. Scoring,
acceptance, and the objective are untouched.

**Context.** The eval-loop design's Phase 4 names a reflective diagnostic agent that clusters
failures difficulty-stratified, routes each to S1/S2/S3, and emits ranked findings that are each a
falsifiable change-manifest. What shipped is simpler: `diagnose()` folds a report into
`(archetype × difficulty)` buckets, the worst-N cases by recall, and a cost summary; `rca.py` is
the Logfire-brief complement. Two constraints frame the upgrade. First, the locked judge-free
discipline bans LLM *scoring* — but is silent on LLM-assisted *diagnosis*, and the ADR-0026 advisor
already resolved that fork for S3: detectors compute every number, an LLM drafts, a pure gate
re-validates, and nothing the LLM says becomes a score. Second, answer-layer correctness has no
judge-free signal, so the two-layer disagreement of the original design is unmeasurable without a
judge; the taxonomy an honest diagnosis can compute is retrieval-layer only.

**Options considered.**

- **Structural-only diagnosis.** Rejected as the end state (its detectors ship as this decision's
  first half): templated manifests ("bucket X weak → try strategy Y") carry no case-specific
  reasoning, and §8's reflective rung would remain unbuilt.
- **Findings feed the human report only.** Rejected: zero hidden-judge risk, but the reflective
  output then feeds nothing — diagnosis improves reading while proposing stays blind to it.
- **Ungated LLM reflection.** Rejected: an unvalidated narrative could reference invented buckets
  or smuggle judgment-shaped claims into the loop; the advisor's gate discipline exists precisely
  to prevent this.
- **Detectors + gated LLM reflection, advisory feed, scored predictions (chosen).**

**Decision.**

- **Deterministic detectors own every number.** `diagnose()` gains a per-case **error-type**
  classification computed from cited-vs-gold ids, gold rank within the citation order, precision,
  and tool-call counts — `gold_missed` (gold absent), `gold_buried` (present but low-ranked),
  `over_fetch` (low precision / high tool calls), `abandoned` (no real retrieval effort) — each
  routed to a primary surface (S1 instructions / S2 config / S3 schema) via the eval-loop §8 table
  and aggregated per `(archetype × difficulty)` bucket. The fields are additive on `Diagnosis`;
  existing consumers are untouched. The answer layer stays unmeasured, and the taxonomy says so.
- **A gated LLM reflection drafts findings.** `rca/reflect.py` runs an injected pydantic-ai model
  (the proposer's seam idiom) over the detector output plus the worst trajectories and drafts
  **ranked findings**, each carrying a **change-manifest**: the proposed change, its predicted
  per-bucket metric delta, and the buckets it puts at risk. A pure gate re-validates every draft —
  referenced buckets and cases must exist in the diagnosis, surfaces must be in-set, predictions
  must name real metrics — and rejects what fails; the detector output always survives alone, so
  reflection is degradable (no model, no findings, same run).
- **Findings advise; measurements decide; predictions get graded.** Gated findings render as an
  advisory section in the proposer prompt — the proposer may ignore them — and the acceptance path
  is untouched (the objective reads measured metrics only). Each manifest's predictions are checked
  against the next round's measured per-bucket deltas, and the hit/miss record lands in the
  calibration report: the LLM's opinions are scored, never scoring.

**Consequences.** (+) The §8 rung ships without breaching judge-free scoring, and its diagnostic
claims accumulate a measured track record instead of trust. (+) The detectors alone upgrade the
structural signal for every consumer, LLM present or not. (−) A persuasive-but-wrong narrative can
still steer *which* candidates get proposed within a run — bounded by structural acceptance, the
patience cap, and the prediction scoreboard that exposes a chronically-wrong reflector. (−) One
more model call per round, inside the existing `token_budget`. **Reversible?** Yes: dropping
`reflect.py` and the prompt section leaves the enriched detectors as a pure improvement.
