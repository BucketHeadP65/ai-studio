---
id: engram-adr-0032-runtime-policy-rung
aliases: ["engram-adr-0032-runtime-policy-rung"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0032 — The runtime policy rung: auto-apply scoped to the per-vault runtime surface (Vibe RAG Phase 7)"
---

# ADR-0032 — The runtime policy rung: auto-apply scoped to the per-vault runtime surface (Vibe RAG Phase 7)

**Status:** Accepted (2026-07-05). Designed in the vibe-rag-phase7-closeout planning session; to be
built on `feat/vibe-rag-phase7-closeout` (Vibe RAG **Phase 7**, the closeout phase). Deliberately
relitigates the propose-only delivery boundary of [ADR-0018](0018-calibration-loop.md); gated by the
[ADR-0033](0033-frozen-split.md) frozen split and the [ADR-0030](0030-freshness-via-mirror-hash-invalidation.md)
staleness signal; acceptance statistics per [ADR-0029](0029-decision-core-subsampled-calibration.md).
Scope: an `instructions` key on the per-vault `_meta/active-policy.yaml` (and its three consumers),
the `--apply-runtime` flag on `engram evals calibrate`, an append-only `_meta/policy-history/`
snapshot dir, the `engram evals rollback-policy` verb, and the machine staleness gate in
`CalibrationSettings`. No new DB tables; the read path's SQL surface and the `wiki_reader` role are
unchanged. Structural-advisor schema proposals stay human-gated regardless (ADR-0026), as do the
shipped-code channels and every S2b baked knob.

**Context.** Every phase since ADR-0018 re-affirmed propose-only: a calibration run is per-corpus,
and a policy edit touched shipped code (the `DEFAULT_INSTRUCTIONS` constant, the `searching-the-wiki`
skill's managed block), so an automatic write was too much autonomy for a one-corpus signal. Two of
that judgment's premises have since changed. Acceptance is now statistically honest — the ADR-0029
paired-bootstrap guard sizes the margin to variance instead of trusting a raw `min_gain` — and
ground-truth drift is now detectable — the ADR-0030 sweep gives every dataset version a judge-free
staleness number. The third premise was never uniform: the shipped `apply-policy` already writes two
blast radii — shipped code (cross-corpus) versus the per-vault `_meta/active-policy.yaml` that the
serve/MCP layer reads live (one corpus, one file, reversible by restoring it). What remained missing
for a bounded rung was an instructions channel that does not touch shipped code, a rollback, and an
external yardstick (the eval-loop Iron Law names a frozen held-out set the loop never optimizes
against; no shipped mechanism enforced it).

**Options considered.**

- **The full eval-loop §9 ladder (S2a config knobs + S2b proposed migrations).** Rejected. The
  baked knobs (RRF weights, `ef_search`, tokenizer) still require code changes the loop cannot make;
  the runtime config knobs (`search_k`, facet defaults) are not in the calibration search space, so
  widening `RetrievalPolicy` to include them would grow the search space and the blast radius in the
  same step that first grants autonomy — two risks compounded.
- **Propose-only forever.** Argued, not assumed away — and rejected this time. With honest
  acceptance, a staleness gate, a bounded per-vault surface, and one-command rollback, withholding
  the rung leaves the Iron Law unmechanized and keeps the loop's throughput human-bound; the
  shipped-code argument that justified the boundary does not apply to a vault-local file.
- **Autonomy for dataset top-up only.** Rejected. Dataset maintenance is already a consented verb
  with immutable versions; the controlled variable of the program is the policy, and a rung that
  never touches the controlled variable closes nothing.
- **A standing `auto_apply` settings knob.** Rejected. Consent becomes ambient; a flag on each
  invocation keeps the human deciding per run, without a scheduler this costs nothing.
- **The runtime-scoped rung with per-invocation consent (chosen).**

**Decision.**

- **The whole `RetrievalPolicy` becomes runtime-per-vault.** `_meta/active-policy.yaml` gains an
  optional `instructions` key beside `strategy` and `tool_descriptions`. Its consumers: the
  calibrate incumbent (`baseline_policy` becomes vault-aware, so successive runs hill-climb from the
  last applied winner), `evals run`'s default instructions (variant-stamped, explicit
  `--instructions` still overrides), and the FastMCP server's `instructions` field at `engram mcp`
  start. The shipped `DEFAULT_INSTRUCTIONS` is the fallback; the skill's managed block and the
  constant rewrite stay the human-gated shipped channel (`apply-policy`, unchanged).
- **Auto-apply is `calibrate --apply-runtime`, off by default.** When set, a run whose winner
  clears all four guardrails writes the winning policy's three channels to the vault file — never
  shipped code, never a commit. The four guardrails: **accept-iff-better** on the decision core with
  the paired-bootstrap guard (ADR-0029), the **cost guardrail** (ADR-0018), the **machine staleness
  gate**, and the **frozen-split beat** (the winner must beat the incumbent on the ADR-0033 frozen
  split, paired per case). Any guardrail failing leaves the run propose-only with the reason in the
  report.
- **The machine staleness gate is a deliberate asymmetry over ADR-0030's warn-never-gate.** Human
  paths keep warn-only; `--apply-runtime` *refuses* when the dataset's or the frozen split's stale
  fraction exceeds `apply_staleness_max` (a `CalibrationSettings` knob, default `0.0`) and prints
  the top-up / freeze command instead. A human may knowingly run stale. The machine must not.
- **Rollback is one command over an append-only history.** Every apply — human or machine —
  snapshots the outgoing live file to `_meta/policy-history/active-policy.v{N}.yaml` before
  overwriting `_meta/active-policy.yaml`; `engram evals rollback-policy` restores the latest
  snapshot's content as a new apply (itself snapshotted), so rollback-of-rollback works and the
  serve path's filename never changes.

**Consequences.** (+) The control loop closes end-to-end on a bounded, reversible, per-corpus
surface, with consent per invocation and the Iron Law enforced by ADR-0033. (+) A calibrated
doctrine now reaches live MCP clients (server instructions) without touching shipped code. (−) Two
sources of truth for the effective policy (shipped defaults vs vault override) — mitigated by the
existing serve banner's policy marker and `status` surfacing the override. (−) The MCP server's
instructions now vary per vault — deliberate: per-corpus calibration is the program's premise. (−)
The history dir grows one file per apply; pruning stays a user-owned filesystem operation
(ADR-0028's idiom). **Reversible?** Yes, additively: dropping the flag, the verb, and the vault
file's `instructions` key returns exactly to ADR-0018's propose-only posture.
