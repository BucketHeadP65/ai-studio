---
id: bs.judges-operators-telemetry
aliases: ["bs.judges-operators-telemetry"]
type: brainstorming-doc
title: Judges and operators over agent telemetry
summary: Dialogue notes separating in-band capabilities from out-of-band consumers of agent telemetry, and unifying operators, monitors, and judges as bindings of one scoring function.
status: active
emits:
  - "[[mu.three-planes-model]]"
  - "[[mu.verdict-binding-algebra]]"
  - "[[mu.emission-contract]]"
  - "[[mu.evidence-kinds-doctrine]]"
---

# Judges and operators over agent telemetry — draft

**Status:** Brainstorming (2026-08-08). Dialogue notes; no decisions made. **Prompt:** An agent's every move is captured in Logfire —
instructions, user prompts, tool calls, spans. We want (a) live monitors on agentic workflows, (b) judges that run periodically over a
golden set, (c) deterministic analytics views (tool-usage averages, dashboards). The hunch: judges/operators feel like *observers or
consumers triggered by events* — a log emitted, a timer. The open question: what would a judge or operator *be*, "in terms of a capability"?
"Observe an aspect of this agent" — but what kind of thing is that?

---

## 1. Why "judge as a capability" resists definition

The question feels unclear because it is a category mismatch, and naming the mismatch dissolves most of the fog.

A **capability** (in the pydantic-ai-harness sense — `AbstractCapability`) is **in-band**: it wraps the run path. It sees the run *while it
happens* and has the power to change it — rewrite context, block a tool call, force a retry, inject a reminder. It is synchronous with the
run and scoped to the run.

The judges and operators in the hunch are **out-of-band**: they consume the telemetry record *after* emission. They can never affect the run
that produced the signal — only future behavior (raise an alert, gate a deploy, mint a new golden case, recalibrate a policy). They are
asynchronous, and their natural scope is not "this run" but "this stream" or "this suite".

So a judge is not a capability *of the agent*. It is a consumer *of the agent's evidence*. The only genuinely agent-side artifact is the
**tap**: instrumentation plus a promise about what gets emitted (§4). Everything else lives off the run path — which is exactly why the
harness repo ships guardrails (in-band) but has no judge capability, and why its eval framework proposal (pydantic-ai-harness #120) is filed
as "agent-level feature, NOT a capability".

## 2. Three planes

| Plane | What it is | Tense | Powers |
|---|---|---|---|
| **Run plane** | The agent + in-band capabilities (guardrails, advisor, reminders) | present | intervene: block, rewrite, retry |
| **Record plane** | The OTel/Logfire span stream — the trajectory as append-only data | past | none; it is the immutable fact base |
| **Judgment plane** | Consumers of the record: operators, monitors, judges | future | influence: alert, gate, recalibrate, grow the golden set |

The record plane is the load-bearing boundary. Everything downstream can only be as good as what crosses it — a judge cannot score
tool-choice quality if tool arguments are logged as truncated strings. Fidelity of emission bounds quality of judgment.

## 3. The three consumer species

The hunch lumps "operators and judges" together as event-triggered observers. They do share the consumer shape, but they split cleanly on
**determinism** and **trigger**:

| | **Operator** | **Monitor** | **Judge** |
|---|---|---|---|
| Nature | deterministic aggregation | rule or sampled model verdict on live traffic | model verdict under a rubric |
| Trigger | continuous / on-ingest | event (span emitted, window closed, threshold crossed) | schedule or release |
| Input | span populations | individual live trajectories | fresh runs over a **golden set** |
| Output | metrics, views, dashboards | alerts, trace annotations | verdicts, regression reports, deploy gates |
| Variance | none — total functions | low (rules) to judge-level (sampled LLM) | real, must be managed (§5) |
| Cost | cheap, run on everything | medium — sample or pre-filter | expensive, bounded by suite size |

- **Operator** — classic analytics on spans: tool-call counts, latency percentiles, cost per run, loop detection. No interpretation, no
  variance. Note Logfire itself already provides most of this tier natively (SQL over spans, dashboards, alerts) — the build-vs-buy line
  probably sits *between* operators and monitors.
- **Monitor** — subscribed to the live stream; fires per-event or per-window. Can be a deterministic rule (SLO breach, guardrail-trip rate)
  or an LLM judge sampled over live traces (what vendors call "online evals").
- **Judge** — the periodic, golden-set instrument. Re-runs the agent on frozen inputs and scores fresh trajectories against a baseline. This
  is what engram's eval harness already is, offline.

## 4. The unifying algebra: one function, many bindings

Strip the operational clothing and a judge proper is just a pure function:

```
judge : Trajectory × Rubric → Verdict
```

Everything else in the hunch is a **binding** of that function:

```
binding = (trigger, input source, scope, authority)
  trigger      ∈ {in-band hook, event, window, schedule, release}
  input source ∈ {live run (in flight), live trace (emitted), replayed trace, fresh run over golden case}
  scope        ∈ {one run, window, suite}
  authority    ∈ {block this run, alert, annotate, gate deploy, report only}
```

The same scoring function bound three ways yields three familiar things:

- bound **in-band, one run, authority=block** → a *guardrail* (the degenerate judge whose verdict has immediate power — the only binding
  that is legitimately a capability);
- bound **on-event, live traces, authority=alert** → a *monitor*;
- bound **on-schedule, golden suite, authority=gate/report** → an *eval judge*.

This reframes the unclear question. Instead of "what is a judge as a capability?", ask: **"what binding does this verdict need, and what
authority does it carry?"** The capability question only even arises for the in-band binding.

And "observe an aspect of this agent" decomposes the same way:

> aspect = an **emission contract** (the agent promises typed spans/attributes for that
> aspect — in-band, cheap, deterministic) **+ a subscriber** bound to that contract
> (out-of-band, where the judgment lives).

The aspect lives in the *contract*, not in the agent and not in the observer. That is the piece the hunch was circling: the agent-side thing
is small and dumb (emit well-typed evidence); the observer-side thing is free to be arbitrarily smart because it is off the run path.

## 5. The test suite, philosophically

What the `Dataset / Case / Evaluator` model (pydantic_evals, harness #120's `EvalCase / EvalScorer / EvalSuite`) actually commits you to:

**Evaluation is a controlled experiment; monitoring is field observation.** The suite fixes inputs (cases), controls execution (fresh runs,
not production traces), and compares against a baseline. Lab conditions buy *comparability across time* — "did my change make it worse?" is
only answerable when the inputs are held still. Monitors answer the complement under field conditions: "is reality drifting away from what
my lab covers?" Neither substitutes for the other.

**The unit of judgment is the trajectory, not the answer.** An agent can emit the right answer through broken reasoning (fails on the next
similar input), or the right answer at 47 tool calls (cost problem), or the right answer having retrieved garbage and gotten lucky
(reliability problem). Output eval, process eval, and memory/learning eval catch different failure modes. The Logfire trace *is* the
trajectory — which is why the record plane's fidelity (§2) is the real substrate of the whole system.

**The judge is a fallible instrument, so a verdict is evidence, not truth.** Field report from the #120 thread: the same trajectory scored
0.6 / 0.8 / 0.7 across three runs at temperature 0; usable signal required median-of-5 at 3× cost, and regression detection required
paired-difference tests on bootstrapped samples rather than comparing means. Consequences:

- a verdict must carry provenance (judge model, rubric version, samples, dispersion) — i.e. it is a **typed, signed evidence kind** under
  owner policy, exactly the trust doctrine already adopted for engram: any evidence kind is admissible, *self-assessment is not* — the judge
  must be independent of the agent it judges, which is a second, independent argument for keeping judges out-of-band;
- suite deltas are statistical claims, not arithmetic ones; a 50-case suite mostly measures its own noise unless treated statistically.

**Descriptive metrics become normative targets, and then Goodhart bites.** A `ToolCountScorer` (fewer calls = better planning) rewarded
agents for skipping verification and hallucinating confidently. The same number is safe as an operator's *descriptive* metric and dangerous
the moment a judge makes it *normative* (gates something). Prefer usefulness measures ("did this tool result change the next step?") over
count proxies wherever a verdict carries authority.

**The golden set is a frozen distribution, and that is both its power and its decay mode.** Frozen inputs give comparability; meanwhile
production drifts. The repair is the drift-as-trigger loop: a monitor detects live trajectories the suite doesn't cover (novel failure
shape, distribution shift) → that trace gets labeled and minted as a new golden case → the suite grows to re-cover reality. Offline judge
and live monitor are the same calibration cycle at two frequencies — which is precisely the shape of engram's existing retrieval-calibration
loop, so this whole design is that loop generalized from retrieval to agent behavior.

## 6. The control loop, assembled

The species compose into an escalation ladder — cheap and total at the bottom, expensive and discriminating at the top:

```
spans ──▶ operators (all traffic, deterministic)      metrics, dashboards
              │ anomaly / threshold / novelty
              ▼
          monitors (flagged or sampled traffic)       alerts, annotated traces
              │ uncovered failure shape
              ▼
          golden-set minting (label the trace)        suite growth
              ▼
          judges (scheduled, suite, baselined)        verdicts, gates, calibration input
              ▼
          verdicts emitted back as spans ─────────────▶ judge drift is itself observable
```

Two properties worth keeping:

- **Operators are tripwires that focus judgment.** You cannot afford an LLM verdict on every span; deterministic pre-filters decide what
  deserves one.
- **The judgment plane writes its own telemetry.** Verdicts appended as events/annotations make judgments auditable and make *judge*
  regression measurable (judge-the-judge against a small human-labeled anchor set, on a slow cadence).

## 7. Where this sits on our substrate

CQRS is the house vocabulary and it maps exactly:

| This design | CQRS/engram term |
|---|---|
| span stream | event stream (`core/observability.py` is the tap) |
| operator | projection / read model |
| monitor | event-triggered policy / process manager |
| judge | scheduled process producing evidence documents |
| verdict | typed, signed evidence (trust doctrine) |
| dashboard | query over projections |

Already in place: the offline judge (engram `features/evals` — harness, evaluators, experiments, runner, instrumentation; golden sets in
`engram/evals/datasets`; the calibration loop). Logfire natively covers most of the operator tier.

Missing, in dependency order: (1) the **emission contract** — are current spans typed enough for consumers to read attributes instead of
scraping rendered text?; (2) **live consumers** — the monitor tier and its trigger plumbing; (3) a **verdict store** — Logfire annotations
vs a first-class table; (4) the **minting path** from flagged trace to golden case.

## 8. Worked example: tool-choice quality

Stress-test of the algebra: run one aspect end-to-end. Aspect: *"did the agent pick the right tool, with the right arguments, at each
decision point — and did the result advance the task?"*

**Emission contract.** Per model-request step: the **toolset offered at that step** (names + schema hash — with dynamic/deferred toolsets
this changes per step, so it must be snapshotted per step, not per run), the chosen call(s) with untruncated arguments, and linkage (step
order, run id). Per tool execution: result (or externalized reference), error/retry status, latency, cost.

**Operator tier** (descriptive, all traffic): calls per tool, error and retry rates, latency/cost per tool, loop signature (same tool +
near-identical args ≥ N), offered-but- never-used toolset breadth (context cost with no utility). Dashboards; no verdicts.

**Monitor tier** (event-triggered): deterministic rules on operator output (loop signature fires → annotate trace + alert; per-release
error-rate spike), plus a sampled LLM monitor on flagged runs asking "was each call the best choice *given the offered set*?" — impossible
without the alternatives in the contract.

**Judge tier** (scheduled, golden set): frozen tasks with a **replayable tool environment** (deterministic tool results — otherwise two runs
of the same case aren't comparable). Rubric: per-step choice quality (was a strictly better tool offered?), argument quality, usefulness
("did the result change the next step?"), terminal task completion. Scoring: median-of-k LLM verdicts + deterministic sub-scores; baseline
comparison as a paired difference over the same cases.

**Verdict shape** (evidence): `{trace/case id, judge model, rubric version, k samples, median, dispersion, per-step findings, redactions
disclosed, signed-by}` — typed, signed, never produced by the agent under evaluation.

**Authority.** Operator metrics never gate (tool *count* as a target is the Goodhart trap — §5). Monitors may annotate, alert, and
*nominate* golden-case candidates for labeling. The judge may gate a release only on statistically-treated regression in usefulness or
completion — never on raw counts.

**Where the model creaked — four discoveries:**

1. **The contract is aspect-driven and bigger than default tracing.** Judging a *choice* requires recording the *alternatives* (the offered
   toolset per step). Standard instrumentation logs the call made, not the option set it was chosen from. You cannot bolt this aspect onto
   whatever OTel happens to emit.
2. **Golden-set judging of tool-using agents needs replayable tool backends** — fakes or recorded fixtures. The eval suite converges on the
   same fake infrastructure as the unit tests (fakes-over-mocks extends from testing doctrine to eval doctrine). Open fork: is that
   convergence a requirement or a tier (fakes for the regression suite, live tools for a small canary suite)?
3. **Run-plane redaction fights record-plane fidelity.** Guardrails scrub secrets in-band; the record plane needs its own redaction policy,
   and a verdict computed over redacted evidence must disclose that fidelity loss in its provenance.
4. **"Usefulness" is computable out-of-band** (did the result change the next step — it's all in the trajectory), which confirms the
   consumer placement: nothing in this aspect needed to touch the run plane except emission.

(A second example worth running later: *goal drift* — it would exercise the multi-turn dimension this one doesn't: information planted at
turn 3, tested at turn 18.)

## 9. Open forks

1. **Venue** — *decided 2026-08-08: stay conceptual for now; refine the model before picking a substrate.* (Options were: engram feature,
   pydantic-ai-harness contribution — #120 is greenfield but constraint-inheriting — or a standalone experiment.)
2. **Emission contract** — audit what engram/workflows spans actually carry today; define semantic attributes per aspect before building any
   consumer.
3. **Verdict storage** — trace annotations (verdicts live with evidence) vs app DB (verdicts queryable/governable); possibly both with one
   as projection of the other.
4. **Judge calibration** — who judges the judges: anchor-set size, human-label cadence, drift tolerance before a rubric is declared stale.
5. **Authority policy** — which verdict kinds may gate what (deploy, config rollback, golden-set admission), under the evidence-kinds owner
   policy.
6. **Replayable tool backends** (from §8) — *decided 2026-08-08: tiered.* Frozen fakes/recorded fixtures for the regression suite
   (comparable baselines, reuses the fakes-over-mocks infrastructure); a small live-tool canary suite on top. The canary's purpose is
   distinct: it does not measure agent regression (too noisy for that) — it measures the **fidelity gap** between frozen and live
   environments, so environment drift becomes an operator metric (canary-vs-frozen delta) and a stale fixture set becomes detectable rather
   than silently misleading.
7. **Record-plane redaction policy** (from §8) — separate from in-band guardrail scrubbing; verdicts must disclose fidelity losses from
   redacted evidence.
