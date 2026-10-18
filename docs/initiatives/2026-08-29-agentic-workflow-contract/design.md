---
id: spec-2026-08-29-agentic-workflow-contract
aliases: ["agentic-workflow-contract-design", "spec-2026-08-29-agentic-workflow-contract"]
type: spec-doc
title: "The agentic workflow contract: a deterministic spine, reviewers that read everything, steps with budgets and examples"
status: executed
owner: maintainer
date: 2026-08-29
---

# The agentic workflow contract: a deterministic spine, reviewers that read everything, steps with budgets and examples

Status: approved 2026-08-29 in chat, point by point (P1 to P6 agreed, D2 with the owner's caveat that the orchestrator stays
and is redesigned later).

## Problem

A workflow here is already a program the machinery runs over agentic steps, and the steps work: a block with a tight brief, a
few doors and a typed output ran fine every time this week. What fails is everything around a step, and the 2026-08-29 promote
run shows all of it at once. The gate that reviewed three drafted ADRs was the session's chat agent, with forty tools, code
mode, a loop detector and a request budget it learned about at 72 percent, handed a 4,000 character preview of one draft out
of three. It searched for thirteen minutes, mistook an earlier failed run for its own, and approved with a caveat nobody reads.
It had two answers available, approve or reject, so "I could not read this" had nowhere to go.

Nothing in the contract of a step says how much it may spend, what it must be handed in full, which tools a reviewer may hold,
or what a reviewer answers when it cannot judge. No step ships the examples that would show it works before a workflow trusts
it. And one wiring shape, a node reading a gate and a sibling of the gate, runs before the verdict exists, which nothing
refuses at pack time.

## User Stories

- As the owner, when a reviewer cannot judge what it was handed, the run parks and I see the reviewer's reasons above the
  artifact in the gate card, then I decide.
- As the owner, I read a step block and know what it may spend, what it may touch, and what it answers with.
- As the owner, I run a step's examples from the command line and read a report before I put the step in a workflow.
- As an agent reviewing at a gate, I am handed the whole artifact, a few read tools, a budget I know from the first turn, and a
  third answer for the case where I could not judge.

## Acceptance Criteria

- [ ] A `judge` node's `work` may name a brought step. The step's output shape carries the field `approved_when` names, typed
      either `bool` (a leaf, as today) or the closed set `approve`, `reject`, `cannot_judge`. The validator refuses any other
      type with a sentence naming the field.
- [ ] The validator refuses a judge step that asks for a tool outside the host's declared read set, naming the tool. As a guard
      for UB-5 it refuses a node wired to a gate and a sibling of that gate at once, naming both and the fault.
- [ ] A `cannot_judge` verdict parks the run the way a human gate does. `session.active.pending.reviewing` opens with the
      reviewer's feedback, then the artifact. Approve releases the value, reject reruns `rejected_to` with the person's feedback.
      Under `gate_mode: orchestrate` the orchestrator answers the escalation, under `auto` it passes, both as today.
- [ ] `TaskSpec.budget` (`requests`, optional `tool_calls`) sets `UsageLimits` and the guards for that step, and the step's first
      user turn states the number. A block without `budget` runs on today's constants.
- [ ] The orchestrator's rejection turn receives the whole reviewed text, not 500 characters.
- [ ] `harness/blocks/steps/check-adrs.md` is a judge step (read tools only, `budget: {requests: 8}`, verdict output) and
      `docs-promote-initiative` v3 reviews `drafts` with `kind: judge`. The blocks test runs it, `pack` lists it, and a live run on
      repo-docs completes with the judge deciding inside its budget after reading every draft (checked in the transcript).
- [ ] `harness/blocks/evals/check-adrs.yaml` holds at least four cases (approve, reject for restating the design, reject for wrong
      sections, cannot judge for an empty artifact) and `harness evals run st.check_adrs.v1` runs them through the same executor a
      workflow uses and prints a report with every case passing.
- [ ] Root ADR-0014 states the definition and the six practices. `harness/docs/reference/workflow-monitoring.md` documents the
      judge's third answer. `CONTEXT-MAP.md` carries the rows. The UB-5 entry records the validator guard. `validate_docs.py` OK.
- [ ] Gates green in core, engram, studio and harness. Restart ceremony for harness and studio-api.

## Constraints

- Upstream first. Budgets are `pydantic_ai.usage.UsageLimits`, the near-limit nudge is `WarnNearLimits`, examples are
  `pydantic_evals` datasets, tool narrowing is `FilteredToolset`. Nothing parallel is built.
- The `orchestrator-gate` kind, `gate_mode` and the orchestrator itself stay as they are. The research workflows are untouched.
- `core` stays app-agnostic: it knows a registry may declare which tool names a judge may hold, never which names those are.
- `studio-client` is not touched. It already draws `judge` as an agent gate, and its chat components carry another session's work.
- Agent-facing copy (block instructions, verdict prompts, refusal sentences) follows the agent-ux skill: the rule and the move,
  never the library, the version or the ledger.
- Copy punctuation: only `, . ? ( )`. Docstrings state the contract. Never `Any`, `object` as an escape, `cast` to silence,
  `# noqa`, `# type: ignore`. Fakes over mocks, no monkeypatching.
- A core change runs the consumers' full gates (engram, studio, harness) before it is done.

## Decisions

- **D1. The definition, as a root ADR.** An agentic workflow is a deterministic spine that calls agentic steps. The spine owns
  order, parallelism, retries, persistence, budgets and state, and never asks a model what runs next. A step is a function
  whose body is a model turn: typed input, typed output, the smallest tool set that does the job, a budget known from the
  first turn, and examples that show it works. The six practices: whole input or explicit paging, never silent truncation; a
  reviewer holds read tools only and answers approve, reject with reasons, or cannot judge; the budget is in the contract;
  control flow changes only through a typed verdict the spine interprets; every step is evaluated on its own before a
  workflow trusts it; every step's full input and output is retrievable afterwards. ADR-0014 replaces "a workflow is a DAG
  spec" as the sentence the vocabulary serves.
- **D2. The reviewer is a judge answered by a brought step.** `kind: judge` already exists, nothing on the shelf uses it, and the
  UI already draws it as an agent gate, so its meaning is free to sharpen. `work` names a step block. The block's output shape
  carries the field `approved_when` names, typed as the closed set `approve`, `reject`, `cannot_judge` (a leaf returning `bool`
  stays valid). The step gets the reviewed value whole as its one input, runs on the tier the node or the block names, through
  the same executor every brought step uses. A new node kind was rejected: it would touch the vocabulary, the validator, the
  compiler and the UI for a distinction `judge` already draws. Keeping the orchestrator as the reviewer was rejected on the
  owner's word: the orchestrator is needed elsewhere and is redesigned later.
- **D3. Cannot judge escalates to the person.** In the compiler a `cannot_judge` verdict calls the approval hook the way a human
  gate does, with the reviewer's feedback ahead of the value, so the run parks and the owner decides in the existing gate card.
  No third graph branch: the park path exists, is tested, and already answers under every gate mode.
- **D4. The budget is in the contract.** `TaskSpec.budget` with `requests` and optional `tool_calls`. `run_defined_step` builds
  `UsageLimits` and the guards from it and states it in the step's first user turn, at the end of the message so the prefix
  stays cacheable. Without a budget the constants apply. A warning alone was rejected by the transcript: a reviewer near a limit
  it did not know it had approved to escape it.
- **D5. Only the verdict moves the run.** The validator refuses a judge step that asks for a tool outside the read set the host
  declares on the registry (`WorkRegistry.judge_tools`), so a reviewer can never run, message or schedule anything. The check
  sits in the validator because pack time is the gate that refuses before anything runs.
- **D6. Whole input wherever a model reads.** The gate prompt and `run_values` are whole since 4294ab88. The rejection turn's
  prompt becomes whole. Screens keep their preview, `render_for_review(limit=4_000)` stays for cards and panels.
- **D7. A guard for a known engine fault, never a design rule.** A node wired to a gate and a sibling of it is a legitimate
  shape that the engine currently runs wrong (UB-5). The validator refuses it with a message that names the fault and the
  workaround, so a run fails at pack time instead of at minute six, and the guard is removed with the upstream fix. The fault
  goes upstream as an issue with the bare repro, posted by the owner. No design guidance carries the workaround.
- **D8. A step ships its examples.** A `pydantic_evals` dataset file beside the blocks, `harness/blocks/evals/<step id>.yaml`
  in `Dataset.from_file` form, run by `harness evals run <step id>` through `run_defined_step` on the step's tier, report
  printed. Grading is `EqualsExpected` on the verdict for a judge, an `LLMJudge` rubric where a step's output is prose. No new
  framework: `engram.evals` already stands on `pydantic_evals`. The first dataset is the ADR reviewer, because its grading is
  exact.
- **D9. The proof is the promote workflow.** `docs-promote-initiative` v3 reviews through the judge. The research workflows keep
  their orchestrator gate until the orchestrator's own redesign.

## Out of Scope

The orchestrator's redesign (the owner returns to it). Gate card changes in `studio-client` (the reviewer's reasons ride in
the reviewing text). Migrating `research-compound` to the judge. Datasets for every existing step (the mechanism lands with
one). The upstream fix for UB-5. Token and cost budgets on steps (`UsageLimits` carries them, a block declares requests and
tool calls for now).
