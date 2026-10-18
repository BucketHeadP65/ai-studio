---
id: harness-adr-0007-flexible-gates-and-worker-memory
aliases: ["harness-adr-0007-flexible-gates-and-worker-memory"]
type: adr
app: harness
status: accepted
tags: []
title: "0007 — Flexible gates: feedback that reruns with context, and automode that never counts as a human"
---

# 0007 — Flexible gates: feedback that reruns with context, and automode that never counts as a human

**Status:** Accepted (2026-08-24). Extends [0005](0005-runs-are-first-class-monitored-artifacts.md) (the run archive now
also carries worker conversations) and depends on [0004](0004-parallel-sessions-workspace-pool.md)'s pool for the
orchestrator turn and `message_worker` to have a session to run in.

## Context

A gate before this decision heard only `approve` or `reject`, and a rejection reran the upstream step with nothing said —
the step repeated its own prior attempt verbatim rather than revising it. The owner's mandate: a person at a gate can
attach feedback, and that feedback has to reach an agent that can act on it, whichever kind of step produced what is under
review. Separately, a worker step's invocation carried no memory across a run: a rejection's rerun and any later look at
the same worker started from nothing, and a person could not ask a worker anything outside the graph's own execution of
it. Both problems meet at the same seam — what happens between a rejection and the value coming back to the gate — and
needed one coherent answer, not two.

## Decision

**The verdict widens; the vocabulary does not.** `GateVerdict(approve: bool, feedback: str = "")` replaces the boolean
future a gate resolves; `POST .../decide` becomes `{approve: bool, feedback?: str}`. Approve ignores feedback outright —
`ChatApproval.review()` returns `verdict.approve` before ever looking at `verdict.feedback` — so a stray remark on an
approval can never trigger a rerun nobody asked for. There is no third verdict: "revise" is a rejection that says why.

**Routing depends on what the rejected step *is*, not on any new field.** `ChatApproval._route_feedback()` inspects the
gate's `rejected_to` target: when that node names any step the artifact brought (`spec.tasks` — a worker, a task
briefing, or a deterministic program), the feedback is stashed on `session.active.feedback[work_name]`; a worker's or a
task briefing's own rerun reads it back — as the next message in the worker's in-run conversation, or as an appended
revision note on the briefing. When the target is instead a step this host registered itself — a step with no briefing
of its own to carry a comment into — the feedback reaches `on_step_feedback` instead, which
`ChatApproval.__post_init__` binds by default to `feedback.orchestrator_turn()`: one headless turn of the run's own
owning session agent (`features/running/feedback.py`), naming the workflow, the gate, the value under review, and the
feedback, run under that session's own turn lock so it can never race a person's own chat message into the same session.
Either way, the run stays parked at the gate while the revision happens; a reject-with-feedback never itself approves the
retry — the revised value always comes back to the same gate for a person to read, unless automode is on.

**Workers keep memory for the run's own duration.** `WorkerConversations` (`features/running/conversations.py`) holds one
`WorkerLine` per step name — its spec, output type, and `messages: list[ModelMessage]` — shared between the compiled
graph's own closures and `session.active.conversations`, so the same object backs every invocation of that step across
the run: the step's first call starts the line, a gate-feedback rerun continues it, and the new `message_worker(node_id,
message) -> str` tool (`features/running/tools.py`, mounted beside `run_workflow` and `check_runs`) appends to it too.
`message_worker` is scoped hard — refused unless the run is parked at a gate (so it can never write the same conversation
the graph's own execution is writing), and refused for any node id outside the calling session's own live run. The
conversation is written into the run archive's `conversations` field on every ending path, keyed by step name, so a paused
run resumes its workers' memory intact and a finished run's node drawer can show a worker step's whole conversation, not
just its final value. Memory is reset at every fresh launch (`WorkerConversations.reset()`) and never crosses a run
boundary — nothing a worker was told in one run is visible to another.

**Automode is a per-run bypass, and it is never trust.** `auto_approve_gates`, set at launch (the Run button, `run_workflow`,
or a schedule's own record) or flipped mid-run through `POST /api/run/automode`, makes `ChatApproval.review()` return
`True` immediately for every gate the run reaches, before either the asker or the browser's park is ever consulted — the
gate is never shown to anyone. Every gate it passed this way is recorded in `gates_auto`, a field the attestation and the
archive record both carry beside — never inside — `gates_approved`. This is the deliberate rule, stated once so no reader
has to re-derive it: **`gates_approved` means a human clicked; automode's `gates_auto` means nobody was asked, and trust
derivation reads the second as confirming nothing human, by omission from the count `gates_approved` feeds.** The shape is
the same one a coding agent's own bypass-permissions mode already has: available, deliberate, and always visible in the
record, never disguised as a review that happened.

**Rejected.**
- A third gate verdict ("revise") distinct from reject — the spec's own reading is that a revision request *is* a
  rejection that explains itself; a third verdict would give trust derivation, the archive, and every future reader two
  rejection shapes to reconcile instead of one.
- Routing feedback by a flag on the node rather than by inspecting what the rejected step actually is — a flag can drift
  from the node it describes; asking `rejected_to`'s own node whether it names something the artifact brought or a step
  this host registered itself is a fact the spec already carries, so there is nothing to keep in sync.
- Global worker memory (a worker remembers across runs) — the spec is explicit that memory ends with the run; a worker
  that remembered across runs would make two unrelated runs of the same workflow silently share state through it.
- Automode folded into `gates_approved` with a boolean flag distinguishing them — the owner's honesty rule needs the
  distinction to survive at the storage layer itself, not only in a reader's interpretation of a flag it might ignore.

## Consequences

- `ChatApproval` now owns feedback routing as well as the approve/reject decision; a future gate-adjacent feature (a
  third routing target, say) extends `_route_feedback()`, not the callers that construct `ChatApproval`.
- `message_worker` gives the orchestrating agent a narrow, run-scoped write path into a worker's own conversation — any
  widening of its scope (reaching another session's run, working while the graph itself is executing) is a decision this
  ADR's refusals already made once and should not be silently relaxed.
- The run archive's `conversations` field must be read by anything that renders a node drawer for a worker step; a record
  archived before this decision simply carries none (the field defaults empty), which every reader must treat as "no
  conversation," not as an error.
- `gates_auto` is now a permanent, distinct field on `Attestation` and `RunArchiveRecord`; any trust-scoring code written
  against `gates_approved` alone was already correct and needs no change — the honesty rule is enforced by omission, not
  by a check a scorer has to remember to add.
- An orchestrator turn fired from a step's rejection runs under the owning session's own turn lock, so it can starve a
  person's own chat message in that session for as long as the investigation takes; this is accepted as the cost of
  reusing one turn lock rather than inventing a second kind of turn.
