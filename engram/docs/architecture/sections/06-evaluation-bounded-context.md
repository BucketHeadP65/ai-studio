<!-- GENERATED from architecture.md by scripts/docs/split_doc.py --dir engram/docs/architecture — do not edit here; edit the master and re-run the script. -->

## 6. Evaluation (bounded context)
> **TL;DR:** The evals slice generates judge-free retrieval ground-truths from the knowledge base's own structure, maintains them as immutable freshness-swept dataset versions with a frozen yardstick, runs a tuning loop that adjusts a unified RetrievalPolicy against them (human-gated everywhere except one machine rung that may write the per-knowledge-base `_meta/active-policy.yaml` behind four guardrails), diagnoses failures with deterministic detectors plus a gated LLM reflector, proposes gated structural-schema recommendations, scores runs with recall@k/precision@k/MRR over a Logfire-instrumented harness, and grades an answer's support, contradiction handling and abstention from the immutable evidence one blind, tool-guarded trial retained.
<!-- load-when: evaluation, evals, tuning, retrieval policy, gold dataset, recall, precision, mrr, rca, structural seed, coverage, freshness, staleness, frozen split, freeze, runtime policy, rollback, reflection, recommend, evaluation run, tuning run, dataset, recommendation, quote check, freshness check, outcome evaluation, answer grading, answer criterion, delivered evidence, blind trial, abstention, counterevidence, answer support, trial trace -->

The **Evaluation** context is the single `engram/evals` slice, the
self-tuning-retrieval program (Vibe RAG, all eight phases 0-7 shipped. The
program story lives in [vibe-rag.md](../../explanation/vibe-rag.md)). It has five parts.
**Structural generation** seeds judge-free QA cases from the indexed knowledge base's
own structure (a claim, a survey fan-in, a multi-hop path, a synthesis fan-out,
four archetypes), so a case's gold source ids are known by
construction before any model runs
([ADR-0016](../../adr/0016-structural-eval-generation.md)). The **dataset lifecycle**
keeps those cases trustworthy at knowledge base scale: stratified salted-hash seed draws
against a coverage report ([ADR-0027](../../adr/0027-stratified-salted-hash-seed-selection.md)),
immutable dataset versions with stable case identity
([ADR-0028](../../adr/0028-immutable-dataset-versions-and-case-identity.md)), an
`evals freshness` check whose staleness fraction warns but never gates
([ADR-0030](../../adr/0030-freshness-via-mirror-hash-invalidation.md)), staleness-aware
top-up + a retirement record ([ADR-0031](../../adr/0031-staleness-aware-dataset-maintenance.md)),
and a **frozen split** (`evals freeze`) the loop never trains on
([ADR-0033](../../adr/0033-frozen-split.md)). The **tuning loop**
tunes a unified `RetrievalPolicy` (instructions + strategy + tool descriptions)
against those ground-truths, accepting a candidate only if it beats a primary
accuracy metric within a cost guardrail on a fixed decision core
([ADR-0029](../../adr/0029-decision-core-subsampled-calibration.md)) and generalizes
on a held-out split ([ADR-0018](../../adr/0018-calibration-loop.md)). Delivery is
human-gated with **one machine rung**: `studio evals tune` emits a
winning-policy file, `studio evals apply-policy` writes the working tree
without committing, and `tune --apply-runtime` may write exactly one
file (the per-knowledge-base `_meta/active-policy.yaml`) behind four guardrails,
every apply snapshotted to an append-only `_meta/policy-history/` with an
`evals rollback-policy` escape hatch
([ADR-0032](../../adr/0032-runtime-policy-rung.md)). **Diagnosis** is deterministic
detectors first (a per-case error-type taxonomy) with an optional gated LLM
**reflector** whose findings only recommend to the proposer and whose predictions are
graded against the next round ([ADR-0034](../../adr/0034-reflective-diagnosis.md)).
The **recommendation loop** proposes schema edits as simulated, gated,
human-applied recommendations ([ADR-0026](../../adr/0026-structural-advisor.md)). The
**harness** scores runs with `RetrievalMetrics` (recall@k / precision@k / MRR)
under Logfire instrumentation, with saved-SQL + Logfire-MCP **RCA**.
The contract lives in [evals.md](../../reference/evals.md).

### evals

`engram/evals` is the whole Evaluation context, the self-tuning-retrieval
program. It generates judge-free retrieval ground-truths from the indexed knowledge base's
own structure, hill-climbs a unified `RetrievalPolicy` against them (human-gated,
plus the one guardrailed ADR-0032 machine rung), and scores runs with `RetrievalMetrics`
(recall@k / precision@k / MRR) over a Logfire-instrumented harness. The slice is
organised into five sub-packages
(`generation/`, `tuning/`, `rca/`, `harness/`, `recommendation/`, plus `shared/` value
objects and the slice-level `commands.py` / `handlers.py` bus surface. Its wiring is
app-side, in `studio/src/studio/knowledge_base/wiring/evals.py`, and the `studio evals` CLI is its only
entry point, with no HTTP route) and gets **two** C4 L3 component sub-diagrams.

**Generation, the dataset lifecycle, and the tuning loop.** Generation seeds
cases whose gold ids are known by construction (`SeedQueries` reads `kb.*` over
the owner session, `archetypes` dispatches the four structural shapes, the
`CaseDrafter` writes only question/answer/quotes and `accept` quote-verifies them via
the WikiNote kernel's `quotes_in_mirror`, `CaseCurator` dedups + stratifies, and the
`GenerationPipeline` stamps each accepted case's stable seed-derived name). The
lifecycle keeps datasets honest at scale: `allocate` turns the coverage report's
stratum inventory into per-cell quotas (ADR-0027), `datasets` owns the immutable
`<name>.v{N}.yaml` version chain, its `.current` pointer, and the stamp sidecars
(ADR-0028), `freshness` recomputes every case's anchors + structural premises
against the live knowledge base (ADR-0030, report-only premises per D12), `topup` plans
staleness-aware replacements against the retirement record (ADR-0031), and `frozen`
carves the held-out sidecar `evals freeze` writes (ADR-0033). The
`TuningLoop` then hill-climbs on a fixed decision core with a rotating
diagnosis tail (ADR-0029), excluding the frozen names and scoring baseline
and winner once each on them at run end: `diagnose` folds an eval report into a
`Diagnosis`, the `PolicyProposer` emits one targeted revision (non-binding reflective
findings render as extra context when a reflector is bound), the
`TuningObjective` owns the accept / convergence / `frozen_beat` rules, and the
run promotes the highest-validation `RetrievalPolicy` as a YAML file
([ADR-0018](../../adr/0018-calibration-loop.md)). Writing that policy into a knowledge base is the
ADR-0032 machine rung, and the file is the Knowledge base's own: Evals keeps the four
guardrails and, once they pass, dispatches `ApplyRetrievalPolicy`, whose handler
snapshots the outgoing live policy into the append-only `_meta/policy-history/`, writes
`_meta/active-policy.yaml`, and restores it on `evals rollback-policy`. The Evals
handler never touches the folder itself. `mcp_tuning` exposes the same propose to evaluate to
submit primitives as MCP tools so a Claude Code client can be the controller brain
by hand.

```mermaid
C4Component
    title evals (C4 L3, 1/2), generation, the dataset lifecycle, and the tuning loop
    ContainerDb_Ext(pg, "Postgres, kb.*", "owner/primary session", "edges / claims / sections")
    Container_Ext(kernel, "wikinote kernel", "wikinote/verify.py", "quotes_in_mirror (quote check)")
    System_Ext(model, "pydantic-ai Model", "Azure, TestModel", "drafter / proposer brain")
    Container_Ext(kbmeta, "knowledge base _meta/", "active-policy.yaml + policy-history/", "the serve-read tail (section 5, F35)")

    Component_Boundary(gen, "engram/evals/generation, cases + the dataset lifecycle") {
        Component(seedrepo, "SeedQueries", "generation/infrastructure/seeds.py", "5 structural seeders to Seed (gold ids by construction), band_case SQL mirror")
        Component(archetypes, "ArchetypeSpec registry", "generation/service/archetypes.py", "resolve_archetype to seeder + drafting prompt")
        Component(drafter, "PydanticAiDrafter / accept", "generation/infrastructure/drafter.py + domain/drafting.py", "draft Q/A/quotes, accept = quote-verify gate")
        Component(curator, "CaseCurator", "generation/service/curate.py", "dedup (cosine) + stratify by archetype")
        Component(pipeline, "GenerationPipeline", "generation/service/pipeline.py", "seed to draft to accept to curate, stamps stable case names (ADR-0028)")
        Component(coverage, "coverage + allocate", "generation/domain/coverage.py + allocation.py + infrastructure/stratum_inventory.py", "StratumInventoryQueries to CoverageReport to per-cell quotas (ADR-0027)")
        Component(datasets, "datasets version chain", "generation/domain/datasets.py + infrastructure/files.py", "<name>.v{N}.yaml + .current pointer + stamps, require_slug / require_basename")
        Component(fresh, "freshness check", "generation/domain/freshness.py + infrastructure/freshness.py", "era verdicts + structural premises (D12), SqlFreshnessQueries (SELECT-only)")
        Component(topup, "top-up planner", "generation/domain/topup.py", "staleness-aware replacements vs the retirement record (ADR-0031)")
        Component(frozen, "frozen split", "generation/domain/frozen.py", "carve_frozen to <name>.frozen.v{M}.json sidecar (evals freeze, ADR-0033)")
    }

    Component_Boundary(tun, "engram/evals/tuning, the tuning loop") {
        Component(loop, "TuningLoop", "tuning/service.py", "decision core + rotating tail (ADR-0029), frozen names excluded, scored once at run end")
        Component(objective, "TuningObjective", "tuning/domain/objective.py", "accepts (paired bootstrap) / frozen_beat / should_stop")
        Component(proposer, "PydanticAiProposer", "tuning/infrastructure/proposer.py", "one targeted ProposedPolicy per round (+ non-binding findings section)")
        Component(policy, "RetrievalPolicy / active policy", "tuning/policy.py", "load_active_policy / effective_baseline_policy / save_policy")
        Component(strategy, "resolve_strategy", "tuning/strategy.py", "RetrievalStrategy to RetrievalProfile (tools + leg)")
        Component(mcptun, "register_tuning_tools", "tuning/infrastructure/mcp_tuning.py", "diagnose / evaluate_policy / submit_tuning MCP tools")
    }

    Component_Boundary(kbpol, "engram/kb, the knowledge base's own policy file") {
        Component(applyrt, "ApplyRetrievalPolicy", "kb/domain/retrieval_policy.py", "snapshot to apply to rollback (the ADR-0032 machine rung)")
    }

    Rel(seedrepo, pg, "SELECT edges/claims/cells (read-only)")
    Rel(archetypes, seedrepo, "SeedFn to *_seeds")
    Rel(pipeline, archetypes, "resolve_archetype")
    Rel(pipeline, seedrepo, "draw Seeds (stratified salted-hash order)")
    Rel(pipeline, coverage, "inventory to allocate quotas")
    Rel(pipeline, drafter, "draft per seed")
    Rel(drafter, model, "agent.run")
    Rel(drafter, kernel, "quotes_in_mirror")
    Rel(pipeline, curator, "dedup + stratify")
    Rel(pipeline, datasets, "build EvaluationDataset")
    Rel(fresh, pg, "recompute anchors + premises (SELECT-only)")
    Rel(topup, fresh, "verdicts to replacement plan")
    Rel(frozen, datasets, "carve a version's cases")

    Rel(loop, datasets, "split EvaluationDataset (frozen excluded)")
    Rel(loop, proposer, "propose(policy, diagnosis, findings)")
    Rel(proposer, model, "agent.run to ProposedPolicy")
    Rel(proposer, strategy, "EVAL_STRATEGY_NAMES (validate)")
    Rel(loop, objective, "accepts / frozen_beat / should_stop")
    Rel(loop, policy, "winning RetrievalPolicy to save_policy")
    Rel(loop, applyrt, "dispatch ApplyRetrievalPolicy once the 4 guardrails pass")
    Rel(applyrt, kbmeta, "load_policy / save_policy (boundary re-validation), append-only history")
    Rel(mcptun, loop, "propose/evaluate/submit primitives")
```

Source: `src/engram/evals/generation/contracts.py`, `src/engram/evals/generation/domain/{drafting,coverage,allocation,datasets,freshness,topup,frozen,carving}.py`, `src/engram/evals/generation/service/{archetypes,curate,pipeline}.py`, `src/engram/evals/generation/infrastructure/{seeds,stratum_inventory,freshness,files,drafter}.py`, `src/engram/evals/tuning/{contracts,internal_contracts,service,policy,strategy}.py`, `src/engram/evals/tuning/domain/{objective,proposal}.py`, `src/engram/evals/tuning/infrastructure/{proposer,mcp_tuning}.py`, `src/engram/kb/domain/verify.py`, `src/engram/kb/domain/retrieval_policy.py`, `src/engram/kb/infrastructure/retrieval_policy_files.py`

**Harness + diagnosis/reflection + the recommendation loop.** The harness binds the
**same** single-source retrieval toolset the MCP surface exposes
(`harness.build_agent` over `serve/tools.build_retrieval_toolset`,
[ADR-0011](../../adr/0011-single-source-retrieval-toolset.md)), so evaluating the
agent-under-test is evaluating the real retrieval surface. `runner.run_eval` is the
shared run flow both the CLI and the HTTP job delegate to, `evaluators.RetrievalMetrics`
scores recall@k / precision@k / MRR off the captured span tree, and
`experiments.expand_variants` runs the factorial axes. `delivery.apply_policy` is the
**human** shipped-code apply (it snapshots `policy-history` first, sharing the runtime
rung's rollback discipline). Diagnosis lives in `rca/`: `diagnose` folds a report into a
`Diagnosis` with a deterministic per-case **error-type taxonomy** routed to a primary
corrective surface, and `reflect` is the optional gated LLM layer, findings only recommend
to the proposer, and each finding's falsifiable predictions are graded against the next
round's measured deltas ([ADR-0034](../../adr/0034-reflective-diagnosis.md)). `rca.py` is
the programmatic saved-SQL + Logfire-MCP root-cause path over the traces. The
**recommendation loop** (`recommendation/`, [ADR-0026](../../adr/0026-structural-advisor.md)) turns
structural signals into simulated, evidence-gated, human-applied schema recommendations.
`instrumentation` ships every span to Logfire (the deep store). `generation/service/subscriber.py`
enqueues changed digest pages off the indexing `CitingPagesChanged` event for the gen-dataset worker.

```mermaid
C4Component
    title evals (C4 L3, 2/2), harness/metrics, diagnosis + reflection, and the recommendation loop
    Container_Ext(serve, "serve toolset", "serve/tools.py", "build_retrieval_toolset (the SAME toolset)")
    System_Ext(aoai, "Azure OpenAI", "core.ai", "chat model under test")
    System_Ext(logfire, "Pydantic Logfire", "spans + Query API + MCP", "the deep telemetry store")

    Component_Boundary(run, "engram/evals/harness, run + score + deliver") {
        Component(harness, "build_agent / make_task", "harness/agent.py", "AgentUnderTest = model by instructions by toolset to RetrievalAnswer")
        Component(runner, "run_eval / build_eval_model", "harness/service.py", "shared run flow, EvaluationRunConfig, average_metrics")
        Component(metrics, "RetrievalMetrics", "harness/domain/evaluators.py", "recall@k / precision@k / mrr + n_tool_calls (page-level)")
        Component(experiments, "expand_variants / run_experiment", "harness/domain/experiments.py", "factorial: hold one axis, vary the rest")
        Component(instr, "configure_logfire", "harness/infrastructure/instrumentation.py", "process-wide Logfire config, local span fallback")
        Component(delivery, "apply_policy (human)", "harness/infrastructure/policy_files.py", "shipped-code + skill block + active-policy, snapshots policy-history first")
    }

    Component_Boundary(rcab, "engram/evals/rca, diagnosis + reflection") {
        Component(diagnose, "diagnose / classify_case", "rca/domain/diagnosis.py", "EvaluationRunReport to Diagnosis, error-type taxonomy to S1/S2 routing")
        Component(reflect, "Reflector / gate_reflection", "rca/domain/reflection.py + rca/infrastructure/reflector.py", "gated LLM findings (recommend-only), grade_predictions next round (ADR-0034)")
        Component(rcasql, "build_rca_report / build_rca_prompt", "rca/service.py + rca/domain/rca_report.py", "saved-SQL over the Logfire Query API + MCP prompt")
    }

    Component_Boundary(rec, "engram/evals/recommendation, gated schema recommendations (ADR-0026)") {
        Component(detectors, "detectors + simulate", "recommendation/domain/detectors.py + service.py", "structural signals to counterfactual restructure simulation")
        Component(recprop, "PydanticAiRecommendationProposer", "recommendation/infrastructure/proposer.py", "DraftRecommendation over SqlKnowledgeBaseGateway reads")
        Component(recgate, "gate_recommendation / verify_recommendation", "recommendation/domain/gate.py + domain/verify.py", "evidence floor in, A/B verify out, human applies")
    }

    Component(subscriber, "QaGenerationSubscriber", "generation/service/subscriber.py", "CitingPagesChanged to enqueue digest page ids (gen-dataset worker)")

    Rel(runner, harness, "build_agent")
    Rel(harness, serve, "build_retrieval_toolset")
    Rel(runner, aoai, "create_azure_chat_model")
    Rel(harness, metrics, "dataset.evaluate scores")
    Rel(runner, instr, "configure_logfire (best-effort)")
    Rel(instr, logfire, "configure + instrument_pydantic_ai")
    Rel(experiments, runner, "VariantEvaluator")
    Rel(diagnose, reflect, "Diagnosis to gated findings")
    Rel(reflect, aoai, "reflection agent (optional, TestModel in tests)")
    Rel(rcasql, logfire, "query_json_rows / MCP")
    Rel(recprop, detectors, "signals + simulations in the prompt")
    Rel(recgate, recprop, "gate the DraftRecommendation")
```

Source: `src/engram/evals/harness/{agent,service,contracts,internal_contracts}.py`, `src/engram/evals/harness/domain/{evaluators,experiments,delivery}.py`, `src/engram/evals/harness/infrastructure/{instrumentation,policy_files}.py`, `src/engram/evals/rca/{service,contracts,internal_contracts}.py`, `src/engram/evals/rca/domain/{diagnosis,reflection,rca_report}.py`, `src/engram/evals/rca/infrastructure/{logfire_query,saved_queries,reflector}.py`, `src/engram/evals/recommendation/{service,contracts,internal_contracts}.py`, `src/engram/evals/recommendation/domain/{detectors,gate,verify}.py`, `src/engram/evals/recommendation/infrastructure/{proposer,gateway}.py`, `src/engram/evals/generation/service/subscriber.py`, `src/engram/retrieval/tools.py`

The domain model is one family of frozen value objects (`shared/domain.py`) plus the
tuning value objects (`tuning/policy.py` / `domain/objective.py` / `service.py`)
and the thin Postgres run summary (`shared/infrastructure/orm.py`). A `Seed` is the structural unit
whose `gold_ids` are known by construction (its `Archetype` fixing the retrieval shape
and `difficulty` derived from its structural counts). A drafted `AuthoredCase`
(question + `ExpectedQa` ground truth + `QaCaseMeta`, carrying its stable seed-derived
`case_name`) maps to the pydantic-evals `EvaluationCase`. Phases 5-7 add the lifecycle
value objects beside their owning modules, the `DatasetStamp` version sidecar
(`generation/domain/datasets.py`), the freshness `CaseFreshness` / `CasePremise`
(`generation/domain/freshness.py`), and the `FrozenSplit` (`generation/domain/frozen.py`).
**`RetrievalPolicy` is defined here** (its `strategy` field validated against
`EVAL_STRATEGY_NAMES`) and *applied in serve* (section 5, F35), the coupling is via the
`_meta/active-policy.yaml` file `save_policy` writes, **not** a typed import:
serve reads the YAML, so the context boundary is a file, not a code dependency.
`EvaluationRunORM` / `EvaluationCaseResultORM` are the section 9 ERD's Evals cluster. The deep telemetry
lives in Logfire, not Postgres.

```mermaid
classDiagram
    class Archetype {
        <<StrEnum>>
        PINPOINT
        SURVEY
        TRACE
        SYNTHESIS
    }
    class Seed {
        <<value object>>
        +Archetype archetype
        +tuple~str~ gold_ids
        +tuple~str~ source_bodies
        +str seed_key
        +int n_hops
        +int n_sections
        +tuple~AnchorHash~ anchors
        +n_sources() int
        +difficulty() QaDifficulty
    }
    class ExpectedQa {
        <<value object>>
        +str answer
        +tuple~str~ expected_source_ids
        +tuple~str~ expected_claim_anchors
    }
    class QaCaseMeta {
        <<value object>>
        +str anchor_page_id
        +QaDifficulty difficulty
        +Archetype archetype
        +tuple~AnchorHash~ anchors
    }
    class AuthoredCase {
        <<value object>>
        +QaInput question
        +ExpectedQa expected
        +QaCaseMeta meta
        +str case_name
    }
    class FrozenSplit {
        <<value object, frozen sidecar>>
        +str dataset_name
        +int dataset_version
        +tuple~str~ case_names
        +str salt
        +float staleness_at_freeze
    }
    class RetrievalAnswer {
        <<value object>>
        +str answer
        +list~str~ cited_source_ids
    }
    class RetrievalPolicy {
        <<value object, applied in serve>>
        +str instructions
        +str strategy
        +Mapping~str,str~ tool_descriptions
    }
    class ProposedPolicy {
        <<agent output>>
        +RetrievalPolicy policy
        +str rationale
    }
    class TuningObjective {
        +str primary_metric
        +str cost_metric
        +float cost_budget_pct
        +float min_gain
        +int max_rounds
        +int patience
        +float val_split
        +float apply_staleness_max
        +is_improvement() bool
        +accepts() AcceptDecision
        +frozen_beat() bool
        +should_stop() StopDecision
    }
    class TuningResult {
        +RetrievalPolicy winning_policy
        +RetrievalPolicy baseline_policy
        +tuple~TuningRound~ rounds
        +StopReason stop_reason
        +bool frozen_beat
        +bool applied_runtime
    }
    class RetrievalMetrics {
        <<Evaluator>>
        +tuple~int~ ks
        +evaluate(ctx) dict~str,float~
    }
    class EvaluationRunORM {
        +str id
        +str dataset
        +str model_name
        +dict variant
        +dict metrics
    }
    class EvaluationCaseResultORM {
        +int id
        +str run_id
        +str case_name
        +dict scores
        +int n_tool_calls
    }

    Seed --> Archetype : shape
    QaCaseMeta --> Archetype : labelled
    AuthoredCase *-- ExpectedQa
    AuthoredCase *-- QaCaseMeta
    Seed ..> AuthoredCase : accept() builds (gold_ids to expected_source_ids)
    RetrievalMetrics ..> RetrievalAnswer : scores cited_source_ids
    ProposedPolicy *-- RetrievalPolicy
    TuningResult *-- "winning" RetrievalPolicy
    TuningObjective ..> TuningResult : accept + stop rules
    FrozenSplit ..> TuningResult : frozen_* scored once at run end
    EvaluationRunORM ||--o{ EvaluationCaseResultORM : case scores
```

Source: `src/engram/evals/shared/domain.py`, `src/engram/evals/tuning/{policy,service}.py`, `src/engram/evals/tuning/domain/{objective,proposal}.py`, `src/engram/evals/tuning/infrastructure/proposer.py`, `src/engram/evals/generation/domain/{datasets,frozen}.py`, `src/engram/evals/harness/domain/evaluators.py`, `src/engram/evals/shared/infrastructure/orm.py`

### Blind trials and answer grading

`engram/evals/outcomes` grades what an answer is worth, which is a different question from the
recall the harness above measures. It is built on the general outcome-evaluation kernel in
`core.ai.evaluation`: `outcomes/domain/governance.py` is case custody, `outcomes/toolset.py` is the guarded
tracing toolset a trial runs behind, `outcomes/domain/evaluators.py` is the layered retrieval metrics,
`outcomes/domain/answers.py` plus `outcomes/infrastructure/answer_grader.py` are the hidden criterion and
the verdict over it, and `outcomes/service.py` holds the use cases that read delivered evidence back.
Callers outside the slice import `outcomes/contracts.py`. `harness/service.py` carries `BlindTrialRunner`, the `TaskRunner` a comparison drives.

**Execution is blind and grading is tool-free.** A trial receives only
`EvaluationInput(case_id, input)`. Its `AnswerCriterion` never leaves the case, so nothing execution
produces can be shaped by the labels it will be graded against. The trial runs behind
`GuardedTracingToolset` over the references that case withholds: every tool result is stored as
immutable `DeliveredEvidence` before the model reads it, and the trial's tool calls and model turns
land in an `ExecutionTrace`. `trial_id_for(case_id, identity, repetition)` names the record, so two
arms of one comparison never read or overwrite each other's evidence and the id itself names no arm.

`PydanticAnswerGrader` then grades that record. It holds no retrieval tools, never learns the
variant, and reads every payload back through a `DeliveredEvidenceReader`, which refuses anything
whose bytes, byte count or hash differ from the reference it names. A source that changed after the
trial therefore cannot change the verdict, because nothing rereads it. A reference the reader
refuses stops grading before any request is made: every dimension the criterion asks for reads
`unknown`. The model then sees exactly the criterion, the answer and those payloads, and nothing
else.

**Which dimensions a criterion asks for is the criterion's business, not the answer's.** Every other
dimension reads `not_applicable`, which is the only place that status is legal, and a dimension the
criterion does ask for is `pass`, `fail` or `unknown`, never `not_applicable`.

| Dimension | Graded when |
|---|---|
| `correctness` | answerability is `answerable` or `ambiguous` |
| `support` | always |
| `coverage` | the criterion names at least one required condition |
| `contradiction_handling` | at least one evidence requirement carries the `counterevidence` role |
| `abstention` | answerability is `unsupported`, or the criterion carries an abstention requirement |

**Every requirement expects one claim assessment, and the criterion decides which claim answered
it.** The grader instructions ask for one `ClaimAssessment` per entry of the criterion's `evidence`
with that entry's claim text copied verbatim. A claim answers a requirement when the two texts match
once whitespace is collapsed, the ends are trimmed and case is folded (`claim_key`, the same
normalization case custody uses for actor names), so the wording a grading model echoes back never
decides whether the requirement was checked. Matching is positional on both sides: requirements are
walked in their declared order, each takes the first claim not already taken, and the record of what
went unanswered is kept by position too, so a criterion stating one requirement twice is two
requirements to the binder and to the backstop alike. A requirement no claim answered is recorded as
an `unknown` claim naming the source that would have decided it. A `required` one is `critical`, so
silence about a deciding source folds the answer to `unknown` rather than passing. One that does not
decide the case is recorded the same way and left non-critical, visible in the verdict without
moving the fold.

The model's verdict is then bound back to the record before it counts. A claim citing a reference
this trial did not retain (matched on evidence id and payload hash) reads `unknown`, and that check
runs on every claim, including one answering no requirement at all. A claim answering a requirement
that pins `source_version` reads `unknown` unless the retained `DeliveredSource` carries that same
version and a content hash, so source-integrity-dependent support is never guessed. A claim
asserting support reads `unknown` unless its citations carry that requirement's own `source_ref`. A
claim graded `fail` needs no citation, since demanding evidence for a rejection would erase an
honest one. `overall` is then `fail` when any graded dimension or critical claim fails, `pass` only
when every one of them passes, and `unknown` otherwise. A grading model that times out or breaks its
own output contract folds to `unknown` keeping the usage it spent, never a crash and never a free
run.

Source: `src/engram/evals/outcomes/{contracts,internal_contracts,exceptions,service,toolset}.py`, `src/engram/evals/outcomes/domain/{answers,comparison,evaluators,governance,withheld}.py`, `src/engram/evals/outcomes/infrastructure/answer_grader.py`, `src/engram/evals/harness/service.py`
