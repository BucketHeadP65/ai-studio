# The rematch -- proof report

Path A (baseline, from scratch) against Path B (reuse, the saved workflow) over the research knowledge base's six withheld questions. Every run below is a row. A failed or contaminated one is marked and excluded from the aggregates beneath it, never averaged away.

## Per-question results

| question | path | recall | requests | in tok | out tok | seconds | flags | missed |
|---|---|---|---|---|---|---|---|---|
| q.fold-or-retrieve-long-horizon-context | baseline | 0.40 | 5 | 31225 | 876 | 38.35 | clean | ent.hipif, ent.long-context-degradation, ent.long-horizon-agent-task |
| q.fold-or-retrieve-long-horizon-context | reuse | 0.60 | 2 | 8026 | 1276 | 21.54 | clean | ent.long-context-degradation, ent.long-horizon-agent-task |
| q.llm-judged-eval-validity | baseline | 0.31 | 7 | 58099 | 971 | 32.56 | clean | paper.agents-k1-2026, paper.cuprate-expert-eval-2025, paper.gsw-2025, paper.hipporag2-2025, paper.hirag-2025, paper.hmem-2026, paper.memgraphrag-2026, paper.msrs-2025, paper.simgrag-2024 |
| q.llm-judged-eval-validity | reuse | 0.54 | 2 | 21102 | 1475 | 21.51 | clean | paper.gsw-2025, paper.hipporag2-2025, paper.hmem-2026, paper.memgraphrag-2026, paper.msrs-2025, paper.simgrag-2024 |
| q.memory-eval-commensurability | baseline | 0.50 | 4 | 29247 | 1043 | 25.59 | clean | paper.evermemos-2026, paper.zep-2025 |
| q.memory-eval-commensurability | reuse | 1.00 | 2 | 9388 | 1402 | 20.25 | clean | none |
| q.when-does-graph-structure-pay-off | baseline | 0.32 | 5 | 29558 | 848 | 24.61 | clean | ent.knowledge-integration, ent.naive-rag, paper.agents-k1-2026, paper.cag-2024, paper.comorag-2025, paper.eigen1-2025, paper.graphrag-2024, paper.hipporag-2024, paper.hipporag2-2025, paper.hirag-2025, paper.kag-2024, paper.leanrag-2025, paper.spathrag-2026 |
| q.when-does-graph-structure-pay-off | reuse | 0.53 | 2 | 6521 | 1456 | 19.65 | clean | ent.knowledge-integration, ent.naive-rag, paper.cag-2024, paper.comorag-2025, paper.eigen1-2025, paper.graph-rag-survey-2025, paper.gsw-2025, paper.hirag-2025, paper.hypergraphrag-2025 |
| q.when-does-optimization-beat-hand-design | baseline | 0.27 | 4 | 25536 | 825 | 23.14 | clean | ent.ace, ent.brevity-bias, ent.filesystem-feedback-channel, ent.grpo, ent.meta-harness, ent.prompt-optimization, paper.ahe-2026, paper.meta-harness-2026 |
| q.when-does-optimization-beat-hand-design | reuse | 0.73 | 2 | 7154 | 1779 | 25.34 | clean | ent.ace, ent.grpo, ent.prompt-optimization |
| q.when-to-skip-retrieval | baseline | 0.33 | 8 | 42022 | 613 | 30.98 | clean | ent.cache-augmented-generation, paper.comorag-2025, paper.gsw-2025, paper.msrs-2025 |
| q.when-to-skip-retrieval | reuse | 0.50 | 2 | 15523 | 1012 | 16.44 | clean | ent.cache-augmented-generation, paper.comorag-2025, paper.gsw-2025 |

## Aggregates (clean rows only)

| path | n | mean recall | mean requests | mean in tok | mean out tok | mean seconds |
|---|---|---|---|---|---|---|
| baseline | 6 | 0.35 | 5.5 | 35948 | 863 | 29.21 |
| reuse | 6 | 0.65 | 2.0 | 11286 | 1400 | 20.79 |

Excluded from the aggregates above:

None.

## Break-even

Distillation model calls: 16.
Validator round-trips: 4.
Notes: Measured from the studio step transcript (.state/steps/studio-author-0d3b0543/events.jsonl): the one distillation turn made 7 model requests and called check_draft 4 times before the draft came back clean. The prior exploration turn (the answer being distilled) ran on the pre-restart studio process and is not counted here. It is the cost of solving the task once, not of saving it. Session driven by the controller agent on the user's delegation, 2026-08-02. DISCLOSURE (Logfire audit of the exploration turn): the agent never deliberately queried a withheld q.* page (0 targeted reads in 21 tool calls), but knowledge base search RETURNED the question page among its hits in 6 results, snippets included, prompt-level withholding does not control what search serves. Only the scored experiment paths run behind GuardedCorpus's mechanical scrub, and both do. The exploration's exposure was to the question page's framing snippet, not its citation list. The scoreboard is unaffected. ADAPTATION (v1->v2, 2026-08-02): all six v1 headless runs failed (retrieve briefing exhausted its 24-request budget, separately a pydantic-monty worker-protocol bug killed sandbox programs that raise). The field gap it exposed (no replace_task op) was shipped, and the retrieve step became a deterministic program via adapt_workflow with lineage intact. Adaptation turn cost: 3 model calls, counted here. The six failed v1 runs remain as evidence. History is never edited. v2->v3 (straight-line program fix, 2026-08-02): the v2 program wrapped its work in async def main and returned the function object, the authoring trap is now named in the `saving-a-workflow` skill. Adaptation turn ~2 calls. v3->v4 (SQL quoting, 2026-08-02): json.dumps double-quotes read as a Postgres identifier, single-quote escape instead. The misdirection was the human driver hint, not the skill, ~2 calls. v4->v5 (2026-08-02): the glue node had wired the string question parameter into two integer slots since v1, masked by earlier-stage failures in every prior sweep, uncaught by the validator because parameter wires were runtime-checked only. The field gap was shipped as a validator improvement (parameter-wire scalar check, catches this cold at check_draft). The glue program also carried the v2 function-wrap trap and read fields the declared row shape never had. Replaced with a straight-line dedupe/rank/truncate with the bound folded in as a constant. This adaptation was authored directly by the controller agent via the adapt API, not by a studio-agent turn. Charged here as a conservative +2 model calls.
Mean baseline requests (clean): 5.50. Mean reuse requests (clean): 2.00.
Break-even: 5 reuse run(s) recover the distillation cost, at 3.50 requests saved per run.

## Hit-rank (Path B -- where the matched workflow ranked in search)

| question | workflow | hit rank |
|---|---|---|
| q.fold-or-retrieve-long-horizon-context | wf.primary-source-fork-answer.v5 | 2 |
| q.llm-judged-eval-validity | wf.primary-source-fork-answer.v5 | not found |
| q.memory-eval-commensurability | wf.primary-source-fork-answer.v5 | 2 |
| q.when-does-graph-structure-pay-off | wf.primary-source-fork-answer.v5 | 4 |
| q.when-does-optimization-beat-hand-design | wf.primary-source-fork-answer.v5 | 3 |
| q.when-to-skip-retrieval | wf.primary-source-fork-answer.v5 | 3 |

## Drift (Path B -- adaptations the matched workflow needed)

- q.fold-or-retrieve-long-horizon-context: no drift -- the workflow still matched the environment it was saved against.
- q.llm-judged-eval-validity: no drift -- the workflow still matched the environment it was saved against.
- q.memory-eval-commensurability: no drift -- the workflow still matched the environment it was saved against.
- q.when-does-graph-structure-pay-off: no drift -- the workflow still matched the environment it was saved against.
- q.when-does-optimization-beat-hand-design: no drift -- the workflow still matched the environment it was saved against.
- q.when-to-skip-retrieval: no drift -- the workflow still matched the environment it was saved against.

## Stated caps

`BASELINE_BUDGET` is 48 requests -- twice the workflow's own `DEFINED_STEP_BUDGET` (24), a runaway cap rather than the cost claim: every cost figure above comes from recorded actuals, never from this number. The saved workflow's collective step budget (`sum(DEFINED_STEP_BUDGET if step.tools else BRIEFING_ONLY_BUDGET for step in spec.tasks)`) is 36 requests. The check that `BASELINE_BUDGET` (48) is at least that holds.
