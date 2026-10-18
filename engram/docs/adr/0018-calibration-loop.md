---
id: engram-adr-0018-calibration-loop
aliases: ["engram-adr-0018-calibration-loop"]
type: adr
app: engram
status: accepted
tags: []
title: "ADR-0018 — The retrieval calibration loop (Vibe RAG Phase 2)"
---

# ADR-0018 — The retrieval calibration loop (Vibe RAG Phase 2)

**Status:** Accepted (2026-06-24). Built on `feat/vibe-rag-calibration` (Vibe RAG **Phase 2**, the
controller half of the self-calibrating-retrieval program). Builds directly on
[ADR-0016](0016-structural-eval-generation.md) (Phase 1, structural ground-truth generation), runs
on the [ADR-0017](0017-local-postgres-primary-testbed.md) local Postgres bed by default, reuses the
[ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md) retrieval-strategy axis as a tuned knob,
measures over the [ADR-0006](0006-dual-dialect-bm25-seam.md) BM25 seam, and treats
[ADR-0015](0015-code-mode-on-the-mcp-retrieval-surface.md) code-mode as a deliberately excluded eval
axis. Scope: a new cluster under `features/evals` (`policy.py`, `diagnosis.py`, `objective.py`,
`proposer.py`, `calibration.py`, `mcp_calibration.py`, `delivery.py`), the `CalibrateRetrieval` bus
command with its handler and DI, the `engram evals calibrate` and `engram evals apply-policy` CLI
commands, `EvalsSettings.calibration` (`src/settings.py`), three opt-in MCP authoring tools, and a
per-tool description-override seam threaded through `features/serve/tools.py`. The read path, the
`api` SQL surface, the `wiki_reader` role, and the generation, scoring, experiment, and RCA paths are
unchanged. Structural-schema proposals stay out of scope (that is the separate Phase 4 advisor).

**Context.** Phase 1 ([ADR-0016](0016-structural-eval-generation.md)) made the accuracy signal
deterministic and judge-free: a generated case's gold source ids are read off the compiled corpus
structure before any model runs, so `recall@k` against those ids is correct by construction rather
than by a model's opinion. That is exactly what a control loop needs to be well-posed. Read through
the Vibe RAG control-loop framing, the generated ground-truths are the **setpoint**, the agent's
retrieval policy is the **controlled variable**, cost (tokens, latency) and accuracy are the **error
signal**, and the existing `features/evals/{runner,experiments,rca}` plus `RetrievalMetrics` are the
**measurement**. Phase 1 built the setpoint and the measurement. What was missing was the
**controller**, the half that reads the error signal, proposes a revised policy, evaluates it, and
converges on a better one. Two things had also drifted that the controller needed to clean up first.
The agent-under-test's fallback instruction string was duplicated in two places (`cli_evals.py` and
`features/evals/http.py`), and there was no single object naming the whole edit surface a calibration
run is allowed to move. Without one object, three loose levers would mean three search spaces and no
single thing to optimize, persist, and apply.

**Options considered.**

- **Tune retrieval config knobs only (the mechanical surface).** Rejected as the controlled
  variable. The high-value knobs (RRF weights, `hnsw.ef_search`, the tokenizer) are baked into a
  migration, so a loop cannot sweep and apply them without a code change, and the instruction
  surface (where the agent's retrieval doctrine lives) is where the biggest wins are. Config-only
  would calibrate the cheapest surface and ignore the richest.
- **Keep an LLM judge in the accuracy signal.** Rejected. Phase 1 already made gold ids structural,
  so the accuracy metric is judge-free and deterministic. Reintroducing a judge would put the loop's
  acceptance decision back at the mercy of a model's opinion, the exact inversion Phase 1 removed.
- **Auto-apply the winning policy (close the loop end to end).** Rejected for this phase. A
  calibration run is per-corpus and a policy edit touches shipped code and a shipped skill, so an
  automatic write is too much autonomy for a one-corpus signal. Delivery is propose-only with an
  explicit, never-committing apply step instead.
- **A unified policy plus a propose-only controller (chosen).**

**Decision.**

Six load-bearing choices carry the loop.

**1. One unified controlled variable, `RetrievalPolicy`.** `features/evals/policy.py` defines a
frozen `RetrievalPolicy` bundling the whole edit surface into one artifact: `instructions` (the
agent-under-test system prompt), `strategy` (a `features.evals.strategy` name validated against
`EVAL_STRATEGY_NAMES`, so an unknown name is a contract violation rejected at construction and
`code-mode` is refused as a no-op eval axis), and `tool_descriptions` (per-tool MCP description
overrides, where an empty mapping means use the shipped `serve/tools.py` descriptions verbatim).
`baseline_policy()` reads the shipped incumbent (the canonical `DEFAULT_INSTRUCTIONS`, the default
`full` strategy, and no overrides). One object means one search space, one objective, and one
convergence. The consolidation of `DEFAULT_INSTRUCTIONS` into this module also removed its
duplication across `cli_evals.py` and `features/evals/http.py`, which now import the one copy.

**2. Diagnosis is a pure read of the error signal.** `features/evals/diagnosis.py` exposes
`diagnose(report)`, a pure function over an eval report that returns the per-`(archetype,
difficulty)` bucket metrics, the worst-N failing cases (each carrying the question, the gold ids, the
cited ids, and a tool-call summary), and a cost summary (mean tokens, mean duration). It owns no
measurement of its own, it only folds a report into the signal the proposer reasons about and the
per-metric mapping the objective compares.

**3. The objective is accuracy subject to a cost guardrail.** `features/evals/objective.py` defines
`CalibrationObjective`, a tunable frozen config. `is_improvement(candidate, incumbent)` accepts a
candidate iff its gain on the primary accuracy metric (default mean `recall@5`) clears `min_gain`
**and** its cost stays within `cost_budget_pct` of the incumbent (when the cost metric is missing
from a run the guardrail is skipped, you cannot penalize a cost that was not measured). `should_stop`
names the convergence trigger that fired, the round cap (`max_rounds`), a cumulative-token cap
(`token_budget`), or a patience streak with no new best (`patience`). Every numeric knob carries a
`Field` bound, and the whole config is tunable through `CalibrationSettings`
(`EvalsSettings.calibration`).

**4. The loop hill-climbs with an overfitting guard.** `features/evals/calibration.py` defines
`CalibrationLoop.run`, which is pure orchestration over injected ports: baseline, then per round
diagnose the incumbent, ask the proposer for one targeted revision, evaluate the candidate, accept it
iff `is_improvement` holds on the **train** split, and iterate until `should_stop`. The overfitting
guard is a stratified train and validation split (stratified by archetype). The winner of the whole
run is the accepted incumbent (the baseline included) with the highest **validation** accuracy, so a
candidate that climbs the train split but overfits loses the run to a higher-validation incumbent.
Measurement enters through `PolicyEvaluator`, a tight one-method port (production wraps
`run_eval_on_dataset`, a test passes a network-free stand-in that scores from a per-policy recall
script).

**5. Two brains, one core.** The same shared core drives two proposers. The automated path is an
in-code `pydantic-ai` proposer (`features/evals/proposer.py`, reached through `engram evals
calibrate`) that proposes one targeted change per round. The interactive path is Claude Code itself
over MCP (`features/evals/mcp_calibration.py` registers `diagnose`, `evaluate_policy`, and
`submit_calibration` under the `engram mcp --authoring` opt-in, the same gate the Phase 1 eval
authoring tools use), so an operator can be the proposer by hand. Both surfaces run the identical
diagnose, evaluate, accept, and converge core.

**6. Propose-only delivery with an explicit apply.** The loop emits a winning-policy artifact (a
saved `RetrievalPolicy` YAML) and a report, and never mutates the live system. A separate explicit
command, `engram evals apply-policy` (`features/evals/delivery.py`), writes the three channels into
the **working tree only and never commits**, so the operator reviews the diff. The `instructions`
channel rewrites the consolidated `DEFAULT_INSTRUCTIONS` constant and replaces a sentinel-delimited
managed section of the `searching-the-wiki` skill markdown (both rewrites idempotent). The `strategy`
and `tool_descriptions` channels write a per-corpus `<vault>/_meta/active-policy.yaml` that the
serve and MCP layer reads through the description-override seam in `features/serve/tools.py`, so the
calibrated profile and tool descriptions take effect live without editing the global serve constants.

**Consequences.**

- (+) **Principled acceptance.** The accuracy signal is deterministic and judge-free (gold ids come
  from structure, [ADR-0016](0016-structural-eval-generation.md)), so a candidate is accepted on a
  measured gain over a fixed yardstick, not a model's verdict, and the cost guardrail keeps a win
  from being bought with unbounded tokens.
- (+) **One artifact, one search space.** Folding the three edit surfaces into one
  `RetrievalPolicy` means the loop optimizes, persists, and applies a single object, and the
  consolidation removed the `DEFAULT_INSTRUCTIONS` duplication as a side effect.
- (+) **The overfitting guard generalizes.** Promoting the highest-validation incumbent rather than
  the highest-train one keeps the winner from being a policy that merely memorized the train split,
  which matters because a structural dataset is combinatorial and a proposer can hill-climb hard.
- (+) **Consented delivery.** Delivery is propose-only with an explicit apply that writes the
  working tree and never runs git, so the calibrated policy reaches production only after a human
  reviews the diff and commits (the user-owns-commits discipline).
- (+) **Per-corpus, no cross-vault leak.** The `strategy` and `tool_descriptions` channels land in a
  per-corpus `<vault>/_meta/active-policy.yaml`, so one vault's calibration never silently changes
  another vault's serving behaviour.
- (−) **Structural-schema proposals stay human-gated.** The loop tunes retrieval policy, not the
  vault schema or the chunking and graph layering. Proposing those is the separate Phase 4
  structural advisor, explicitly out of scope here and human-gated by design.
- (−) **The end-to-end CLI smoke needs the local Postgres DB.** `engram evals calibrate` persists each
  round's run via `EvalRunRepository`, so the end-to-end smoke needs a database (the
  [ADR-0017](0017-local-postgres-primary-testbed.md) local Postgres bed by default). The pure
  unit tier (policy round-trip, diagnosis, objective predicates, loop convergence over fakes) runs
  without Docker.
- **Reversible?** Yes, additively. The `features/evals` calibration cluster, the two CLI commands,
  `EvalsSettings.calibration`, the three MCP tools, and the description-override seam are removable
  independently, and reverting leaves the generation, scoring, experiment, and RCA paths and the read
  surface intact.

**Related.** [ADR-0016](0016-structural-eval-generation.md) (Phase 1, the structural ground-truth
generation this loop calibrates against, the setpoint and measurement),
[ADR-0017](0017-local-postgres-primary-testbed.md) (the local Postgres bed the end-to-end smoke runs
on), [ADR-0012](0012-retrieval-strategy-and-code-mode-axes.md) (the retrieval-strategy axis the policy
tunes as one of its three knobs), [ADR-0006](0006-dual-dialect-bm25-seam.md) (the BM25 seam the
measured retrieval runs over), [ADR-0015](0015-code-mode-on-the-mcp-retrieval-surface.md) (code-mode,
the MCP retrieval profile deliberately excluded from `EVAL_STRATEGY_NAMES`), and the eval loop's
graduated-`act` design ([eval-loop.md](../explanation/eval-loop.md) §9), of which this loop is the
first built controller. Code: `features/evals/policy.py` (`RetrievalPolicy`, `baseline_policy`,
`save_policy` / `load_policy`, the canonical `DEFAULT_INSTRUCTIONS`), `features/evals/diagnosis.py`
(`diagnose`), `features/evals/objective.py` (`CalibrationObjective`, `is_improvement`, `should_stop`),
`features/evals/proposer.py` (`PolicyProposer`, `PydanticAiProposer`), `features/evals/calibration.py`
(`CalibrationLoop`, `PolicyEvaluator`, `CalibrationResult`), `features/evals/mcp_calibration.py` (the
`diagnose` / `evaluate_policy` / `submit_calibration` authoring tools), `features/evals/delivery.py`
(`apply_policy`), `src/settings.py` (`CalibrationSettings` / `EvalsSettings.calibration`),
`src/cli_evals.py` (`evals calibrate` / `evals apply-policy`), `features/serve/tools.py` (the
description-override seam). Patterns:
[CQRS message bus](../../../docs/explanation/design-patterns.md#cqrs-message-bus) (the
`CalibrateRetrieval` command).
