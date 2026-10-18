---
id: adr-0013-self-authoring-gates-on-owner-activation
type: adr
title: Agent-authored harness artifacts stay pending until the owner activates them
summary: The agent grows its own harness through four authoring pens, but nothing it writes joins the harness by being written. Every authored capability, leaf, agent, and profile lands pending in a git-tracke
status: proposed
aliases:
- adr-0013-self-authoring-gates-on-owner-activation
app: root
absorbs:
- '[[spec-2026-08-20-self-authoring-harness-design]]'
created: '2026-08-29'
---

## Context

The owner asked (2026-08-20) for the agent to grow its own harness: capabilities, agents, workflow leaves, and workflows. The inventory in the initiative's analysis (bs.self-authoring-harness) found that most of the machinery already exists. pydantic-ai core ships the AgentSpec and capability registries. The harness's capability_creation feature already authors, validates, and stores capability source with validate-on-write and next-run load, but the studio agent never adopts it, and its stock store activates authored code on the next run by default. The WorkRegistry already refuses Any and demands full annotations at its front door. The workflow store already content-addresses WorkflowSpec artifacts behind verify_packable. What is missing is one registry (agents), two pens (a leaf author and a Python builder for specs), one adoption, and one bridge.

The constraint that ruled options out is the tenancy rule, one level up: nothing joins the harness as hand-wired code, and an agent maintaining its own machinery without a review step just moves the drift somewhere less visible. So authored artifacts cannot land live, and the activation act cannot belong to the agent that wrote the artifact.

No accepted ADR already carries this decision. The harness ADRs that exist govern adjacent ground (ADR-0006 scheduled workflows, ADR-0008 orchestrator gates, ADR-0012 library search scope) and none of them decides how agent-authored artifacts become live. This ADR absorbs the executed design spec-2026-08-20-self-authoring-harness-design from the 2026-08-20 self-authoring-harness initiative folder, together with its implementation plan, and carries only its durable decisions, not its task list.

## Decision

D1. Pending is the only landing state, and activation is the owner's attested act. All authored kinds write disabled whether or not they validated, and a broken artifact lands identically to a clean one until the owner acts. Agents never self-activate, including through activate-after-N-clean-runs heuristics. Activation and deactivation ride the harness CLI (and its dashboard door) and every act is recorded to an append-only activation ledger distinct from the workflow trust ledger. The reason it won: it keeps the review step visible and diffable, which is the whole point of a gate.

D2. Two pens for agents. Spec-authored agents (AgentSpec YAML) compose registered behavior only: model, instructions, settings, capability names with config. The document cannot contain a function body, which is what declarative means. Python-authored agents exist for bespoke behavior and ride the capability store as agent-factories until promoted. Bespoke is a temporary state, not a category. Code is where new behavior is born, specs are how existing behavior is composed.

D3. capability_creation is adopted, pending by default. A studio-side store subclass writes every authored entry disabled. The vendor's manifest format is unchanged and no vendor code is edited. Upstream contributions, if any, are filed as issues.

D4. The leaf pen is the capability-creation pattern pointed at the WorkRegistry. An authored leaf is validated by actually registering it into a throwaway registry copy, so the engine's own front door (async only, full annotations, Any refused) is the validator, and its error text is the refusal. Active leaves load into the session's registry copy at world-build. A leaf is an ingredient, never a workflow, and nothing inspects its internals.

D5. Authored agents are AgentSpec documents in a new agent registry, validated on write against the schema and against capability-name resolution, including promoted custom capabilities. A workflow step may reference a stored agent by name beside the inline document, and host kwargs keep overriding the document.

D6. The pen for workflows is a Python builder, the artifact is the spec. WorkflowBuilder is a fluent builder over the four verbs that refuses invalid wiring at construction and emits a WorkflowSpec, which then flows through the unchanged check_draft and pack_workflow gates, lineage, and attestations. The runtime never executes authored orchestration code. Workflows authored as durable code artifacts are abandoned by owner decision (2026-08-20): no author_workflow(code), no graph programs as artifacts. Expressiveness grows in leaves, the four verbs stay closed, and no fifth verb is added.

D7. The promotion bridge closes the loop. An authored capability that declares get_serialization_name() and is itself declared a dataclass on the class (not frozen, which Python forbids over the non-frozen AbstractCapability base, per the amendment landed in the plan) joins the capability registry via custom_capability_types at every Agent.from_spec site. At that point agent-authored agents can declare agent-authored capabilities inside spec'd workflow steps.

D8. Authored artifacts live in one git-tracked root, authored/ with a subdirectory per kind, each with its manifest. Files declare, the run fingerprint reports. A database store was rejected for the same reason the corpus registry rejected one: two sources of truth about what exists will disagree.

## Consequences

Easier. The full closure now has a home: an authored capability can be promoted, declared by an authored agent, and run inside a spec'd workflow step, every link gated. The agent's expressiveness grows by adding leaves rather than by growing the workflow grammar, so the four verbs stay closed and the spec stays diffable. The owner reviews everything through one git root and one CLI door, and every activation is attested.

Harder. Every authored artifact costs an owner round-trip before it does anything, which is deliberate friction that will feel slow when the agent produces many small artifacts in one session. The promotion bridge must be threaded at every Agent.from_spec call site, present and future. Bespoke Python agents are held in a temporary state that only promotion resolves, so a backlog of unpromoted capabilities can accumulate.

The repo must keep honoring the gate. The pending default lives in the studio-side subclass only, so the vendor's next-run default must never be adopted back. There is no self-activation path of any kind, including heuristics, until a fork changes this ADR. Activation, deactivation, promotion, and demotion all write the activation ledger, and the ledger stays append-only. Leaves stay ingredients: no leaf is ever wired as a workflow, and no fifth workflow verb is added. Vendor code stays unedited, with upstream wants filed as issues. The deferred forks (python-authored agents' promotion shape, activation-by-eval, dynamic_workflow adoption for the studio agent) stay open and are decided when their triggering conditions arrive, not silently here.

## Options considered

Code artifacts as durable workflows. The analysis favored it initially and the owner abandoned it (2026-08-20): you cannot content-address behavior or diff two functions' semantics, and a live-script runtime re-admits the opaque scheduler the engine's commitments reject. The builder pen plus the data spec keeps determinism, diffability, and the trust ledger on the spec.

The vendor store's own default (authored capability active on the next run). Rejected because it makes authoring itself the activation act, which is exactly the drift-hiding the tenancy rule forbids.

A database store for artifacts. Rejected: files under git are diffable and reviewable in the same medium the owner already reviews, and a second store invites disagreement about what exists.

No agent registry, inline AgentSpec only. Rejected: stored agents are what let a workflow step name a maintained, reviewable worker instead of inlining a document nobody owns.

Activation by passing a declared eval. Deferred, not decided here. It needs the eval harness to emit typed evidence and is recorded as a fork in the design, not this slice.
