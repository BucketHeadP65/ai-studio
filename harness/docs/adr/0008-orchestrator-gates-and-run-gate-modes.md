---
id: harness-adr-0008-orchestrator-gates-and-run-gate-modes
aliases: ["harness-adr-0008-orchestrator-gates-and-run-gate-modes"]
type: adr
app: harness
status: accepted
tags: []
title: "0008 — Orchestrator gates: a machine decider the artifact can name, and the run modes that convert or bypass a human one"
---

# 0008 — Orchestrator gates: a machine decider the artifact can name, and the run modes that convert or bypass a human one

**Status:** Accepted (2026-08-24). Extends [0007](0007-flexible-gates-and-worker-memory.md), whose verdict shape and
feedback-routing machinery this decision reuses rather than duplicates, and [0006](0006-schedules-and-the-ticker.md),
whose schedule row gains this decision's third gate mode alongside its own `gate_policy`.

## Context

A run that must go unattended today has exactly one way to skip a human gate: automode, `auto_approve_gates`, which
passes every gate with nobody asked and is recorded as such, deliberately never trust. That is honest, but it is also
blunt in two ways. First, an artifact cannot leave one gate as a real check while the rest of a run goes unattended —
the switch lives at launch, not in the file, so a promised human gate can be swapped for nothing at all by whoever
launches the run rather than by whoever wrote it. Second, a schedule firing at 3am, or a chat turn already driving a run
through the `run_workflow` tool, has no way to hand a gate to something that actually decides and says why — only "wait"
or "auto". The owner's ruling: an artifact must be able to *say*, in its own words, that a machine decides a given gate,
and a run must have a middle setting between parking every gate for a person and waving every gate through for nobody.

## Decision

**A third authored gate kind, `orchestrator-gate`, whose decider is the host's own standing agent, not a launch-time
switch.** `OrchestratorGate` (`core/src/core/ai/workflows/_vocabulary.py`) joins `HumanGate` and `JudgeGate` in the
`Node` union: `id`, `charge` (the review brief, floored at 40 characters — the same floor `TaskSpec.instructions`
carries, so a token gesture is refused at parse time), `reviews` (the same kind of `Wire` a `HumanGate` reviews), and
`rejected_to`. Named `orchestrator-gate` rather than `auto`, because what clears it is a named party — the session
agent this run already has, reading the artifact's own brief — and an artifact must be able to state that a machine
decides, not have that fact live only in how the run happened to be launched. This is the authored way to run
unattended: the spec itself carries the brief a person would otherwise have read, and passes its reviewed value through
on approval or routes a rejection's feedback to `rejected_to` exactly as `HumanGate` and `JudgeGate` already do.

**The decider is a term of the registry, not an argument threaded through `compile_spec`.** `OrchestratorDecider`
(`core.ai.workflows`) is a `Protocol` — `async def __call__(self, gate_id, charge, value, /) -> GateDecision` — carried
as `WorkRegistry.decide_gate`, with `WorkRegistry.decides_gates` the property `_check_orchestrator_node` reads. A
registry with no decider refuses an orchestrator-gated spec cold, at `validate()` — `IssueKind.NO_ORCHESTRATOR` — the
exact shape a registry with no runner already refuses a brought program in, and `IssueKind.BLANK_CHARGE` catches a
charge that clears the length floor but does not survive a strip, since forty spaces are not a brief. Carried on the
registry rather than passed to one run, because that is what lets the refusal happen before compilation rather than as
a surprise the first time the graph actually reaches the gate. `value` arrives unrendered — core owns no renderer, and
the harness decider calls the same `render_for_review` a human gate's own reviewer reads, so an approver and a person
are never shown two different versions of one object.

**Three run gate modes, with the legacy switch folded into the middle of them.** `GateMode = Literal["wait",
"orchestrate", "auto"]` (`harness/src/harness/domain/gates.py`) is what a run's own gates decide under: `"wait"` parks
a human gate for a person (unchanged), `"auto"` passes every gate with nobody asked (automode, unchanged), and
`"orchestrate"` hands a human gate to this run's own orchestrator instead of parking it. `resolve_gate_mode(gate_mode,
*, auto_approve_gates)` is the one function every wire boundary calls: `auto_approve_gates=True` has only ever meant
`"auto"`, so it still does when `gate_mode` is left at its default, and the one shape refused in words is the two
disagreeing outright — `auto_approve_gates=True` together with an explicit `gate_mode="orchestrate"` asks for two
different gate behaviors at once. `RunRequest.gate_mode`, `Schedule.gate_mode` (behind alembic `0030`, a plain
`public.schedules.gate_mode text not null default 'wait'` column, agreement with `auto_approve_gates` kept by the
application rather than a database constraint), and `service.start`/`launch`'s own parameter all resolve through this
one function, so a launch, a schedule row, and a live mid-run flip can never drift into disagreeing about the same run.

**Precedence: an authored gate decides the same way under every mode; a run mode only touches a gate that would
otherwise wait on a person.** `_build_orchestrator_step` never consults `ChatApproval` or automode at all — an
`OrchestratorGate` reaches `registry.decide_gate` directly, by construction, whichever of the three modes the run is
in. `gate_mode="orchestrate"` only widens what happens to a `HumanGate`: `ChatApproval.review()` checks automode first
(live off the session, so a mid-run flip takes effect on the very next gate), then — only for a human gate with none of
that — whether `gate_mode == "orchestrate"` and a decider is installed, in which case the same `OrchestratorDecider`
protocol answers it, given a charge of the run's own wording ("Decide in place of the owner at gate '<id>'. Review what
is presented and approve it or reject it with concrete feedback.") rather than the artifact's, since a human gate
carries no charge of its own to read. No decider installed falls back to the park path rather than hanging:
`gate_mode="orchestrate"` with nothing wired behaves as `"wait"`.

**Evidence three ways, and trust reads exactly one of them.** `Attestation.gates_orchestrated` (defaulted, so a legacy
record still parses) sits beside `gates_approved` and `gates_auto`, never inside either. `orchestrated_gates(spec,
values)` derives an authored gate's own ids off run state, the same asymmetry `approved_gates` already reads — an
approved gate wrote its pass-through value under its own id, a rejected or still-parked one wrote nothing — and
`ChatApproval.orchestrated_gates()` separately names the human gates `gate_mode="orchestrate"` converted and approved;
`combined_orchestrated_gates` reads the two together. `trust_record` folds `gates_approved` alone: a machine clearing a
gate the artifact said it would clear, or a mode converting one for it, is honest evidence of what happened and never a
person's approval, by omission from that one count — the same rule `gates_auto` already established, now extended to a
third column rather than reopened. `RunArchiveRecord.gates_orchestrated` carries the same field through to the archive
and to every listing envelope, live and ended alike.

**A bounded wait for the decider's own session, failing the run honestly rather than looping it forever.** An
orchestrator gate needs an answer — unlike a step's rejection turn, which is skipped outright on a busy session because
nothing is waiting on it, a gate has something waiting. `_await_turn` retries the session's turn lock with a capped,
doubling backoff, unbounded on its own; `ActiveRun.gate_timeout` (defaulted to 900.0, the same figure `ChatApproval`
was already built with, set at launch from `approval.timeout_seconds` so the two can never drift apart) is what bounds
it. `_turn_within_gate_timeout` wraps the wait in `asyncio.wait_for` when the ceiling is not `None`, and a session that
never frees within it raises `OrchestratorUnavailableError` naming the gate, left uncaught through
`_build_orchestrator_step` into the run's own generic failure handling — `outcome="failed"`, the sentence carried in
`session.active.result`. Never a fake rejection: a rejection would only rerun `rejected_to` and land the graph back on
this same starved gate, looping on a decision nobody could ever make. Failing the run in words is the honest ending.

**Rejected.**
- A run-level switch as the only way to run unattended, with no authored counterpart — the vocabulary's own reading is
  that "approve everything" changes no word in the artifact, so a promised human gate can be swapped for a machine
  silently; an authored `orchestrator-gate` is what lets the file itself say a machine decides, not the door that
  happened to launch it.
- Threading the decider through `compile_spec` as a run argument rather than a registry term — a spec's fitness to run
  has to be knowable before compilation, the way a brought program's fitness already is; a registry term is what lets
  `_check_orchestrator_node` refuse cold, where an argument would only surface the same failure once the graph actually
  reached the gate.
- A second decider protocol for `gate_mode="orchestrate"`'s converted gates, distinct from `OrchestratorDecider` — one
  protocol answering both an authored gate and a converted one keeps the wiring at one seam, `registry.decide_gate`,
  rather than two a host would have to keep in step with each other.
- A fake rejection when the decider's own session starves past the gate timeout — rejecting reruns `rejected_to` and
  re-reaches the identical starved gate, which loops rather than ends; failing the run honestly is the only ending
  that is actually one.
- Folding `gates_orchestrated` into `gates_approved` with a distinguishing flag — the same reasoning `gates_auto`
  already settled: the honesty rule has to survive at the storage layer itself, not only in a reader who remembers to
  check a flag it could ignore.

## Consequences

- An artifact with an `orchestrator-gate` cannot be validated, let alone run, against a registry built with no
  decider — the refusal is cold, at `validate()`, naming the gate, not a stall the first time the graph reaches it.
- `resolve_gate_mode`'s one guarded conflict is narrow by design: `auto_approve_gates=True` alongside an *explicit*
  `gate_mode="wait"` is not treated as disagreement — there is no way, short of tracking which fields a caller actually
  set through every door including an agent tool call, to tell "explicitly wait" from "left at its default" — so it
  resolves to `"auto"`, the legacy meaning, everywhere this is asked.
- The model call itself, once the session's turn lock is actually held, has no ceiling of its own on either path — an
  authored gate's decider or a converted human gate's. A decider whose own model call hangs still hangs the run,
  exactly as a slow human decision with `timeout_seconds=None` already could; `gate_timeout` bounds only the wait for
  the turn lock, never what happens once it is granted.
- A converted human gate is decided against a standing stand-in charge ("Decide in place of the owner...") rather than
  any authored prose, because a `HumanGate` carries no charge of its own to read — `gate_mode="orchestrate"` gives
  every human gate in that run the same generic brief, never the specific one an `orchestrator-gate` would have
  shipped with.
- `gates_orchestrated` is now a permanent field on `Attestation` and `RunArchiveRecord`; trust-scoring code written
  against `gates_approved` alone was already correct and needs no change — the same residual
  [0007](0007-flexible-gates-and-worker-memory.md) already left standing for `gates_auto`.
- `Schedule.gate_mode` and `auto_approve_gates` are kept in agreement by the application at every write door, never by
  a database constraint — `public.schedules` gained a plain defaulted column (`0030`), not a check that could reject a
  row the application itself produced.
