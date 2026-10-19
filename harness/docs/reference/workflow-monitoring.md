---
id: harness-reference-workflow-monitoring
aliases: ["harness-reference-workflow-monitoring"]
type: reference
app: harness
status: current
tags: [runs, archive, schedules, gates]
title: "workflow monitoring, the run archive, the scheduler, and gate feedback"
---

# workflow monitoring, the run archive, the scheduler, and gate feedback

A **run** is the unit this station tracks: it belongs to one session (a conversation session when the agent started it,
a runner session when a person or a schedule did, see [ADR 0005](../adr/0005-runs-are-first-class-monitored-artifacts.md)),
it moves through one visible phase at a time, and everything it produces stays readable while it goes and after it ends.
Live runs are read off the session pool (`/api/run*`, `/api/runs`). Ended runs are read off the **run archive**
(`/api/runs/history/*`, `/api/runs/recent`, `/api/workflows/{id}/history`), run logs promoted to archive records on
ending rather than deleted. Schedules ([ADR 0006](../adr/0006-schedules-and-the-ticker.md)) are a third source of runs:
a scheduled fire is an ordinary run in a reused runner session, indistinguishable to every plugin below once it starts.
Gate feedback and worker memory ([ADR 0007](../adr/0007-flexible-gates-and-worker-memory.md)) change what happens
between a rejection and the value coming back to the gate, not the gate contract itself. Where a rejection's words go
is described under [Decision](#decision-the-human-half-of-a-gate) below, which supersedes that record's routing rules. Orchestrator gates and the
three-way run gate mode ([ADR 0008](../adr/0008-orchestrator-gates-and-run-gate-modes.md)) add a machine decider a
run's own gates may answer through, beside or instead of a person, without changing what a gate contract itself
promises.

## Run endpoints (`features/running/controller.py`)

Every unscoped route below has a session-scoped twin at `/api/sessions/{session_id}/...` sharing the same handler body.
A run behaves identically whichever plugin reached it. History and the archive are not session-scoped: a run's
history is keyed by workflow and run id, not by which session happened to start it.

```
POST   /api/run                {"workflow_id", "arguments", "run_title"?, "gate_mode"?}
                                                                                       -> {"ok": true, "running": id} | 409 | 404 | 400
POST   /api/run/automode        {"on": bool}                                        -> {"ok": true, "auto_approve": bool} | 409
POST   /api/run/pause                                                                -> {"ok": true, "paused": id} | 409
POST   /api/run/stop                                                                 -> {"ok": true, "stopped": id} | 409
POST   /api/run/resume          {"spec_yaml"?: str}                                  -> {"ok": true, "running", "replayed", "withdrawn"}
                                                                                          | 409 | 400
GET    /api/run/limits                                                               -> {"brakes": [...], "gate_modes": [...],
                                                                                          "default_gate_mode"} (below)
GET    /api/run/state?since=                                                         -> step-by-step transition state (below)
GET    /api/run/values                                                               -> {"workflow", "values": {node_id: rendered}}
GET    /api/pending                                                                  -> {"pending": gate payload | null}
POST   /api/decide              {"approve": bool, "feedback"?: str}                  -> {"ok": true, "approved": bool} | 409

GET    /api/runs                                                                      -> {"runs": [live run card, ...]}
GET    /api/workflows/{workflow_id}/history?limit=50                                 -> {"runs": [envelope, ...]}, newest first,
                                                                                          a live run of it prepended as `outcome: "live"`
GET    /api/runs/recent?limit=20                                                     -> {"runs": [envelope, ...]} across every workflow
GET    /api/runs/history/{run_id}                                                    -> the full record (below) | 404
PATCH  /api/runs/history/{run_id}   {"pinned": bool}                                 -> {"ok": true, "pinned": bool} | 404
DELETE /api/runs/history/{run_id}                                                     -> {"ok": true} | 404
```

`GET /api/run/state?since=` answers `{"running", "workflow", "result", "active": [node_id...], "done": [node_id...],
"failed": [node_id...], "waiting_at": gate_id | null, "events": [StepEvent...], "seq", "gate_mode", "auto_approve"}`.
`since` is a cursor into the transition log: pass the last-seen `seq` for only what happened in between, or `0` for
the whole history (what a reloaded page wants). `StepEvent` is `{seq, node, at, finished, failed}` (`at` is seconds
since the run's own first transition, not wall-clock time). `gate_mode` is the three-way mode this run's own gates
decide under (`"wait" | "orchestrate" | "auto"`, see [ADR 0008](../adr/0008-orchestrator-gates-and-run-gate-modes.md)).
`auto_approve` is derived from it (`gate_mode == "auto"`), never a second flag a launch's choice and a mid-run
`/api/run/automode` flip could disagree about. `/api/pending` and a live run card's `"gate"` field carry the same gate
payload: `{gate_id, workflow, reviewing, rejection_reruns}`. `reviewing` is the value rendered for reading,
`rejection_reruns` names the node a rejection would rerun from.

`RunView` (the live run card `GET /api/runs` answers, one per session with a live run, documented in
[`sessions.md`](sessions.md)) gains `spend`: `SpendView | null` (`features/running/spend.py`), `null` when the run has
opened no spend record yet. `SpendView` is `{"cost_usd": str, "usage": {"input_tokens", "output_tokens", "cache_read_tokens",
"cache_write_tokens"}, "steps": {<step name>: {"cost_usd": str, "usage": {...}}}}`: the run's own totals, and each
step's own, both priced through the same ladder every session turn is priced through.

**A run's own name.** `run_title` is what the run is called in front of a person, given by whoever started it on
`POST /api/run` and carried from there to everything that shows the run: `ActiveRun.run_title` while it goes, the live
run card and the live history row, the run log (so a resume or a boot-time restore keeps the name), the archive record,
and every projection of it. The durable engine carries it in its own `RunRequest`, so the server's history holds the
same name. A launch that names none leaves it `""`, and nothing fills it in: a reader that wants a name for an unnamed
run falls back to the workflow's own card title and then to the workflow id. The session a run lives in is a separate
fact with a separate name: one session can host several runs, and `session_id` says which container a run ran in, never
what it was started to do. Nothing on this side joins a session's title onto a run.

The spend a run card and a history record carry is that one run's own spend record, read fresh off disk. What a run,
a session, a step, a model or a day has cost across every run is a different question, answered by `GET /api/spend`
straight from the telemetry rather than the spend record. See [`spend.md`](spend.md).

## What a run is held to, before anybody starts one

`GET /api/run/limits` answers what this deployment would hold a run to, so a preview can say it
before a person commits to a start:

```json
{"brakes": [{"window": "run", "usd": "2", "warn_at": null},
            {"window": "day", "usd": "25", "warn_at": 0.8}],
 "gate_modes": ["wait", "orchestrate", "auto"],
 "default_gate_mode": "wait"}
```

`brakes` are read off the very `SpendLimits` this deployment mounts on every step agent, so the
figures shown and the figures that refuse cannot be two different sets. A budget that only counts
carries no ceiling and is left out. **An empty `brakes` list is a deployment that configured no
ceiling at all, and a reader states that as no limit configured.** It is never a ceiling of zero,
and nothing anywhere invents a figure for it.

`gate_modes` is what a launch here may actually ask for. On the durable engine an orchestrated gate
has no counterpart, so that deployment does not offer `"orchestrate"` and a preview does not draw a
choice nothing would accept.

## One deliberate start, and a start whose answer never came back

A run starts because a person confirmed a setup. Three rules hold that line.

**One start per session at a time.** Both start routes take the session's own turn hold for as long
as the start is being decided, the same hold a chat turn takes. Admission, the library read and the
launch all await, so without the hold two starts arriving together could each pass a check the
other had not yet invalidated. The second gets a `409` and the session ends up with one run. The
hold is given back the moment the run's own task exists, which is what lets the run's settlement
turn take it in the ordinary way.

**A start is never retried.** The browser sends it once. A request that got no answer may already
have made a run, so sending it again would make a second one nobody asked for.

**A lost answer is unknown, not failed.** When a start request gets no answer at all, the browser
holds the run as unknown, keeps every value the person typed exactly where they typed it, and
offers one act: read the runs and see. That reading is two ordinary reads, live first --
`GET /api/runs` for a run still in flight in that session, then `GET /api/runs/recent` for one that
has already ended -- matched to the session the start would have run in. A run found there is the
run, and the browser acts on it. Neither read naming that session means nothing started, said
plainly, with the typed values still on screen. Starting again from them is the person's own next
act, never one taken on their behalf.

## Reuse: running it again, and making the next version

A **rerun** is a new deliberate start. It opens the same setup the workflow always opens, holding
the values the previous run was given (`RunArchiveRecord.params`, read through
`GET /api/workflows/{id}/history`), every one of them editable, and it sends nothing until the
person confirms it like any other launch. Nothing about a rerun is a replay: it is one more run,
recorded as one more run.

An **adaptation** makes the next version out of a run that disappointed. `POST
/api/sessions/{session_id}/adapt` takes the parent's id, the typed diff to apply, and the run being
adapted from:

```
POST /api/sessions/{session_id}/adapt
  {"workflow_id", "ops": [edit op, ...], "source_run_id"?, "feedback"?, "evidence"?, "predictions"?}
                                     -> {"ok": true, "lineage": {...}, ...the draft} | 404 | 400
```

It answers with the child on that session's own screen, as a draft: the parent workflow is read and
never written, the cited run's record is read and never written, nothing is saved into the library,
and no run starts. The child's `Lineage.evidence` records the three facts separately -- the run it
came from, what that run was **observed** to do (`RunArchiveRecord.result`, the checked outcome),
and the owner's own
reason in their own words. None of the three is graded by anything. Whatever `evidence` the
caller sent is kept under those three rather than taken out by them, so citing a run never costs a
reader the sentence the caller wrote.

A `source_run_id` this deployment has not archived is a `404`: a citation of a run nobody can read
back is not evidence, so the child is not made at all. An adaptation with no ops is refused by
`core.ai.workflows.contracts.adapt`, because an adaptation with no operations is not one.

## Run arguments: typed JSON, admitted once

`arguments` on `POST /api/run` is one JSON value per declared parameter, each as the type that
parameter's own schema declares: an array where it declares an array, a number where it declares a
number, `true`/`false` where it declares a boolean, an object where it declares an object. The agent's
`run_workflow` tool, a schedule's stored `arguments`, and a schedule fire all carry the same shape, and
every one of them is admitted through `core.ai.workflows.contracts.normalize_arguments` -- so a value one entry
accepts is a value every entry accepts.

A client that can send nothing but text still works. Text under a parameter whose schema cannot hold
text is read once as JSON and then checked like any other value, so `"true"`, `"4"` and `'["research"]'`
reach a boolean, an integer and an array parameter. A string parameter keeps the text it was given, so
`"true"` under `{"type": "string"}` stays the four characters `true`. Text that is not JSON is refused
rather than coerced: a Python repr such as `"['research']"` is a 400, never an array. An empty string is
a value, not an omission, and cannot satisfy `minLength: 1`. An optional parameter declaring
`"type": "string"` and left out arrives as `""`. Every other omitted optional stays absent.

A value its parameter's schema refuses is `400` naming the parameter and the JSON path inside that
value (`knowledge_bases $[1]: '' should be non-empty`), answered before any session, task or record is
created. `POST /api/schedules` and `PATCH /api/schedules/{id}` admit the same way and refuse the same
way, so a schedule cannot be saved holding values its own fires would refuse. A patch that moves a
schedule to another workflow re-admits the arguments it already holds against that workflow.

**The first-party selector.** A saved workflow's card may give one of its inputs
`"control": "knowledge-base"` (`InputPresentation.control`, see
[`core-ai.md`](../../../core/docs/reference/core-ai.md)). That parameter's values are then checked
against the knowledge bases this deployment is registered to read, read fresh off the studio registry
(`GET /api/knowledge-bases`) at the moment of the launch, after the schema admission and before
anything starts. A name nothing is registered under is a 400, and a deployment that cannot read its
registry refuses such a run rather than admitting a name nothing checked. Only what the caller
actually supplied is checked, so an optional selector parameter left out arrives as the compatible
empty fill rather than as a knowledge base nobody named.

The declaration lives on the card and never in the parameter's JSON Schema, because `spec_identity`
hashes those schemas: a change to how a person is asked must not re-key the workflow or orphan its
evidence. Two things follow. A launch with no card behind it is not membership-checked, which is
every `run_workflow(workflow_id="draft")` call: the draft is admitted against its schemas alone and
the step's own knowledge-base read is what refuses a name nothing answers, at execution rather than
at launch. And an unknown control can never reach a launch at all, because `control` is a closed set
refused where the card is parsed.

The selector is a harness concern only: `normalize_arguments` knows nothing about knowledge bases.

A parameter whose schema cannot be resolved at all -- a hand-edited library page pointing at a
`$defs` entry it does not carry -- is a 400 naming the schema, never a crash.

The archive keeps what the run was given as the JSON it was given: `RunArchiveRecord.params`,
`RunEvidence.params`, the on-disk run log's `params`, and the durable `RunRequest.params` all carry it.
A record, a log or a history written when every argument was text still parses, and still reads as that
text.

## Step chat, a running step's own conversation, streamed and steered

Every agentic step a run performs (a briefing, an inline agent step, a judge's step, never an inline program or a
gate) opens a conversation line the moment it starts executing, keyed by its node and, for a map, its item. The three
routes below read and write that line while the run is held in the session pool. They are session-scoped only, because
the inspector always holds the run's session. Once the run ends, the same conversations are in the full archive record's `conversations` map
(keyed `"node[item]"`, the item empty for a step that is not a map item, so `"node[]"`), so an ended run needs no route of
its own.

```
GET    /api/sessions/{id}/run/steps                                 -> {"steps": [{"node", "item", "executing", "settled"}, ...]}
GET    /api/sessions/{id}/run/steps/{node}/stream?item=&since=0     -> text/event-stream (below) | 404
POST   /api/sessions/{id}/run/steps/{node}/steer?item=  {"message"} -> {"ok": true} | 409 | 404
```

The listing is one entry per open line in the order the run opened them. `executing` is true for the whole of the
step's run (every model request and tool round trip of it), `settled` once its messages are written, and no live run
answers `{"steps": []}`. The stream is the same Vercel v6 UI-message protocol the chat plugin emits, produced by the
same encoder over the step agent's own events: each frame is one chunk (text, reasoning, tool input and output,
`data-step` usage), replayed from index `since` and then tailed live, with one terminal `data: [DONE]` when the line's
stream closes because the run ended. The first frame is always `data-replay-floor` (`{"floor", "requested"}`): the
absolute index the replay starts from, which is `since` unless the line's live buffer (capped at `STEP_STREAM_BUFFER_BYTES`,
oldest frames evicted first) has already dropped it, in which case the viewer is told what it missed and the archive
still holds the whole conversation. Indices are absolute for the line's life, so a reconnect passes the floor it
started at plus the chunks it has seen. A step chat opened mid-step therefore shows the whole conversation from its
first token whenever the buffer still holds it. A steer is accepted only while the line is executing (409 otherwise,
404 for a line that never opened, which is what a temporal-engine run answers). It is appended to the line's inbox
and echoed onto the stream at once as a `data-steer` chunk (`{"message", "at"}`) so every viewer sees the person's
words, and the step's own delivery capability folds it into a following model request the way the chat's mid-turn
steer does, inside the run's loop, never as a concurrent write to its history (upstream delivers one request boundary
after the drain, or redirects a run that was about to end into one more request). A steer spends inside the step's
stated request budget. A step that overruns because of one fails as any overrun does.

Live streaming and steering need the in-process engine (the default): on the temporal
engine the step closures never reach the browser-facing run state, so the listing stays empty and the stream and steer
routes answer 404.

## A multi-turn map on the durable engine: the line lives in the record

A map that names several `turns` gives each item one conversation carried through all of them. In
this process that conversation is a `StepConversation` in `StepConversations`, which dies with the
process. On the durable engine each turn is scheduled as its own activity, possibly on another
worker, so the line is carried in the conversation record instead and the two engines differ only
in where it lives.

Which arm runs is decided by whether the step is executing inside a Temporal activity that published
a line: `CONTINUING_STEP_CONVERSATION` is set there and nowhere else, so an in-process run keeps resuming its
item out of this process exactly as before. The durable arm opens no `StepConversation`, which is why step
chat and steering stay in-process only.

**Identities are derived, never stored or passed around.** One item's conversation is
`uuid5(WORKFLOW_NAMESPACE, "{workflow execution}:{node id}:{item key}")`. One logical attempt at one
turn is the SHA-256 of the workflow execution, the Temporal scheduled activity id, the node, the
item, the turn index and the authored retry count. Temporal's own delivery attempt is deliberately
absent, so a retried delivery of the same scheduled activity addresses the same stored attempt and
does not run the turn again. What the workflow schedules afresh (a rewind, a repair) carries a new
scheduled id and is a new attempt on the same line.

**What crosses the activity boundary is an address, never a transcript.** `InlineStepCall` carries
a `ContinuationRef | None` and the activity answers with a `InlineStepOutcome` (the step's output as
JSON, plus the ref it committed). The ref is capped at 512 bytes and the output at 1,000,000. The
messages themselves stay in the step record, which is the only authority on them: the turn reads the
exact step the ref names by `(run_id, seq)`, sends it as `message_history`, and reuses the system
prompt that checkpoint was opened with so the request prefix stays byte-stable across turns. The
workflow keeps each line's latest ref in its own state, rebuilt on replay from activity results in
history order.

**Each turn holds two short transactions and no lock across the model call.** The turn claims its
attempt in one, runs, then commits its output and the exact snapshot sequence in another. A crash
after the commit but before Temporal receives the completion returns the stored output on the next
delivery, with no second model or tool call.

**An uncertain turn is refused rather than repeated.** This does not promise exactly-once external
effects. A turn refuses to run automatically when another attempt on its line is unfinished, when its
own earlier attempt began and never settled, when the checkpoint it would resume was saved
interrupted, or when the earlier run left a tool call `started` in the record's tool effects. The refusal
names the attempt or the tool call in question. Settling such a line is the owner's call, not an
automatic retry's.

**A refused turn ends its run rather than retrying.** The three refusals above reach the activity
boundary as a non-retryable Temporal `ApplicationError` of type `ContinuationRefused`, naming the
attempt, its conversation, its run and the reason. This matters because every redelivery of one
scheduled activity carries the same attempt id: a retryable refusal would be re-delivered forever
while the run looked merely slow. The run fails instead, with the reason in its own history.

**Settling a wedged line.** A line stays wedged while an attempt sits `started`, which is the
runtime saying it cannot tell whether that turn's work happened. Read the two facts first, taking
the conversation id out of the failure message:

```sql
SELECT attempt_id, run_id, expected_revision, started_at
  FROM continuation_attempts
 WHERE conversation_id = '<conversation id>' AND status = 'started'

SELECT tool_name, tool_call_id, status, effect_summary
  FROM tool_effects
 WHERE run_id = '<the run id above>' AND status = 'started'
```

When the second query answers with nothing, or with calls you have confirmed did not act, free the
line by dropping the unfinished attempt:

```sql
DELETE FROM continuation_attempts
 WHERE conversation_id = '<conversation id>' AND attempt_id = '<attempt id>' AND status = 'started'
```

The run this attempt belonged to has already failed non-retryably over this refusal, so deleting the
row resumes nothing by itself: it only clears the line so a fresh attempt is possible. Re-run the
workflow, or reset the run from before this turn, once the row is gone.

When the turn's work did happen and you want the line to move past it instead, the row has to be
completed by hand with everything a commit would have written (the settled step's sequence and
digest, the output and its digest, the completion time) and `continuation_heads` advanced by one
revision in the same transaction, then re-run or reset the workflow the same way. Deleting and
re-running is the ordinary choice. There is no console verb for either yet.

**A turn's activity result carries its committed ref.** A turn of a step conversation answers with
an `InlineStepOutcome`, the committed ref beside the output, and a call that continues no step
conversation answers with the step's own output. Roll every worker on a task queue together, or
drain it, before changing either shape, since a worker on other code answers in its own shape.

What is **not** defended, here or anywhere else in this runtime, is changing which activity a node
is scheduled on while runs of it are in flight: renaming a step, or flipping its `replay_safe`
marker. Temporal replays workflow code against the recorded history, so a history recording a
scheduled activity `X` replayed by code that now schedules `Y` is a non-determinism error that fails
the workflow task. Drain, then rename.

## `Decision`, the human half of a gate

```python
class Decision(BaseModel):
    approve: bool
    feedback: str = ""
```

Approve ignores `feedback` outright. It is read only on a rejection. A reject-with-feedback never itself approves the
retry, and the revised value comes back to the same gate for a person to read, unless automode is on for that run. A
step file may also carry `budget` (`requests`, optional `tool_calls`). The run enforces it, and the step reads the
number in its own first turn. A step file may also carry `skills` (names from the skills registry,
[ADR 0013](../adr/0013-skills-registry.md)), mounted on that one step alone.

### What a rejection carries into the rerun

A rejection travels through the run's own graph state, not through a slot keyed by step name. Before any gate decides,
the run writes the value it is about to review under an address of its own (`run id`, the node and turn that produced
it, and the SHA-256 of its canonical JSON) into `reviewed/<run id>/` beside the run log. That file is never
overwritten. It goes when the run log goes: the ending copies every one of them onto the archive record first, and
that copy is what a reference resolves against afterwards. A resume carries forward only the values its new run still
owes a repair over, re-filed under the new run's own id, and takes the paused run's subtree with it. Boot removes any
subtree no run log still names, once per folder and before it relaunches any run there.

When a gate rejects (a person, a judge or an orchestrator), the run does not remove values from itself. It marks the value
keys of the node the gate sends the run back to and of every node downstream of it, including a multi-turn map's
`<map>.<turn>` keys, after the attempt ceiling check. A write of a key clears its mark, and the run log leaves marked keys
out of what it saves. A run relaunched after a restart, or resumed after a pause, while the rerun is in flight runs those
nodes again and owes the repair, so the person is not shown the value they refused. An exhausted run keeps its last outputs.

Every run log records the kind of session the run was launched in, when the launch started it, and its gate mode, gate
timeout and runner. At boot the process's own session's run log is checked first, then every pooled session's, found in each
registered project's sessions folder (`restore_waiting_runs`). A run log relaunches by itself only when its session kind is
"runner" (the Run button in the library) or its runner starts with `app:schedule:` (a schedule). A run relaunching by itself
keeps its logged gate mode, timeout and runner, and its logged session kind. Any other run is paused by the restart. That
means any run in a chat or follow-me session, including the process's own session, and any run log written before the
session kind was recorded unless its runner is a schedule's. Nothing runs, the session keeps the run, and its card shows it
paused with its start time, gate mode, finished steps and spend. The person resumes or stops it.

Resuming a run paused by the restart uses the logged workflow unless the person sends an edited graph, which is validated,
and keeps the logged gate mode, gate timeout and runner. Stopping it ends it from its run log with its evidence and archive
record, on either engine. A run a person paused resumes with the session's live gate mode, so an automode switch holds, and
the logged timeout and runner. A session holding a paused run refuses a new launch (the chat `run_workflow` tool and
`digest_now` retry with a message, the run route answers 409) until the person resumes or stops it. An agent turn is never
resumed. A run log whose session no longer opens is deleted. When the process's own session takes a pooled session's id,
that session's run log moves into the process's own session and is then relaunched or paused by the same rule.

The rejection itself is filed under **the node the gate reruns** (`rejected_to`), carrying the gate id, who decided,
which reach of the gate refused, the words, the repair asked for, and the address of the value reviewed. The rerun
reads it off its own step identity, so every item of a rerun map gets the same rejection, and a different node that
happens to run the same step gets none. The step's next prompt carries one section per rejection: the run's original
parameters, the value that was actually refused (read back by address, not taken from whatever the run holds now),
what was wrong, and what to change.

Every decision is recorded as a fact of its own: the gate, the reach, the verdict (`approve`, `reject`,
`cannot_judge`), **who decided** (`judge`, `person`, `orchestrator`, `auto`, `timeout`) and the digest of the value
decided over. A judge that answers `cannot_judge` and the person it escalates to record one fact each and count as one
reach. `auto` is automode crossing a gate with nobody asked, and it may pass a gate but never refuse one. An approval
belongs to the generation it was given over: a gate re-reached because something downstream rewound must be approved
again, even when the new value is byte-identical to the one approved before.

### The ceiling on reruns

Every gate carries `max_attempts` (default 3): total reaches of that gate in one run, its first review included. A
rejection below the ceiling records the decision and reruns `rejected_to`. The rejection that spends the ceiling
records the decision, emits no rewind, and fails the run with `RetryExhaustedError`, which carries the whole rejection
-- reviewed value included -- so the ending can say what was asked for and never got done. On the durable engine that
arrives as a non-retryable `ApplicationError` of type `RetryExhaustedError` whose detail is that same rejection.

A gate that names no ceiling serializes without the field, so a workflow saved before the ceiling existed and one
saved now are the same document and key to the same content address.

Which decider a gate actually reaches (a person waiting at `/api/decide`, this run's own orchestrator, or nobody)
depends on the run's `gate_mode` and on whether the gate itself is a custom `orchestrator-gate` rather than a
`PersonGate`. See [ADR 0008](../adr/0008-orchestrator-gates-and-run-gate-modes.md) and the gate-modes section below.

## The run archive: envelope, full record, retention, pinning

An **envelope** (a listing row, `GET /api/workflows/{id}/history`, `GET /api/runs/recent`) is `RunArchiveRecord`
(`features/running/domain/ended_run.py`) with `spec`, `values`, `events`, and `conversations` left out: `run_id`, `workflow_id`,
`run_title`, `outcome` (`"completed" | "failed" | "stopped"`), `failure`, `started_at`, `ended_at`, `runner`
(`"app:harness" | "app:chat" | "app:schedule:<id>"`), `session_id`, `params`, `gates_approved`, `gates_rejected`,
`gates_auto`, `gates_orchestrated`, `result` (the run's checked outcome, below), `pinned`. The values this run's gates reviewed are
kept on the record too and are left out of every projection: they are reached by resolving a reference, below.
A listing row stays light on purpose. The single-record endpoint below is where every field, heavy or not, is
answered. A live run in the same listing carries `outcome: "live"` and the same shape with its heavy fields empty.
Read `/api/run/state`, `/api/pending`, and `/api/run/values` for its real detail. `GET /api/runs/history/{run_id}`
answers the **full record**: every envelope field, plus `values` rendered node by node (`render_for_review`),
`conversations` (each worker step's own messages at the moment the run ended, keyed by step name), a `graph` built
from the record's own `spec` snapshot, the same drawing the live workflow page uses, so a historical run renders
exactly as it ran, not as the library holds the workflow today, and `owner_reviews`: every `OwnerReview` committed
against this run, read fresh off its own append-only store by the run id the record already names, below. `graph` is
`null` when the snapshot no longer parses as a `Workflow` (a record survives a schema move even without a drawing).
`owner_reviews` is `[]` for a run nobody has reviewed (unknown owner acceptance, never a silent rejection) and is
left out of both envelopes: a listing row stays light, and reading who accepted or rejected what is always a
single-run question.

Both the envelope and the full record also carry `spend`: `SpendView | null`, the same shape `RunView` carries above,
read fresh off the run's own spend record by the `run_id` the record already names rather than stored on
`RunArchiveRecord` itself. A live run's own envelope row carries the same field, read off its spend record while the
run is still going.

**The spend record.** `FileSpendLog` (`features/running/infrastructure/spend_log.py`) keeps one JSONL file per run at
`.state/runs/spend/<workflow_id>/<run_id>.jsonl`: a `RunSpendHeader` first line naming who ran what, for whom, and from
when, then one `SpendRow` per model response a step produced, appended as the run goes. Every read prices each row
through `response_cost`, so nothing on disk stores a price a catalog or pricing fix could not recompute on the next
read. The fold is total: a torn row costs that one row, an unreadable file reads as nothing, and a header whose
`started_at` will not parse belongs to no time window, so a damaged spend record degrades what a page shows rather
than breaking it. The run id is the one the run archive itself files a record under, so a run's spend and its
evidence name the same run. For a durable run this id is Temporal's own execution run id (`durable.launch`,
`handle.first_execution_run_id`), so a Temporal run's spend record, its archive record, and the server's own execution
history all agree on one name for it. `workflow_spend(workflow_id)` folds every run of a workflow into the
`{"cost_usd": str, "runs": int}` that `GET /api/library`'s own card list and `GET /api/workflow/{workflow_id}`'s detail
both carry as `spend` (`features/browsing/controller.py`), and `schedule_spend` folds one schedule's own fires into the two
figures its own row carries, below. A run whose spend record holds a header and no rows answers a real, zeroed
`SpendView` rather than nothing at all, and that zero folds into `workflow_spend` and `schedule_spend` the same way
any other figure does, so a run that recorded nothing reads as costing nothing rather than as missing.

**Retention.** `APP_RUN__ARCHIVE_DAYS` (default `30`) is how long an unpinned record rests before the sweep removes it.
`0` disables archiving outright: `archive_run()` becomes a no-op and nothing is ever written. The sweep runs at boot
and then daily, and `pinned: true` survives every sweep, the owner's per-run "forever," never a default. `PATCH
/api/runs/history/{run_id} {"pinned": bool}` sets it. `DELETE /api/runs/history/{run_id}` removes a record outright,
pinned or not, the owner's explicit delete, distinct from a sweep. Records live at
`.state/runs/archive/<workflow_id>/<run_id>.json`, one file per run, behind the `RunArchive` protocol (`put`, `get`,
`list_for`, `recent`, `set_pinned`, `delete`, `sweep`). A future blob-store adapter implements the same protocol
rather than replacing these callers.

## The checked outcome: what a run did, beside how it ended

`outcome` says the execution reached its end. It never says the work was done. `result` is the separate answer, an
`Outcome` (`core.ai.workflows`) classified at the ending from the run's own facts: every piece of the primary work
settling exactly once under its own identity, and every decision each required gate received over the exact bytes the
run ended holding. Both endings write it, to the evidence record and to the archive record alike.

`result.status` is one of `succeeded`, `partial`, `noop`, `failed` and `unknown`, beside the counts
(`attempted`, `succeeded`, `skipped`, `failed`, always summing), `retry_attempts` counted apart, `primary_input`,
the `generation` the counts belong to, one `OutcomeCheck` per required gate, and the `ArtifactRef`s the run's fixed
values earned. A run that completes having skipped everything is `failed`, not `completed`-and-fine. A run whose gate
spent its last reach is `failed` and its check carries the refusal whole: who refused, over which generation and
digest, the feedback and the repair that was asked for. A run that did not complete, whose current generation is still
missing a disposition, whose facts contradict each other, or whose workflow declares no result at all is `unknown`,
and so is a run that predates this field. `unknown` is a projection, never a rewrite: the record on disk keeps
whatever it was written with.

`succeeded` is checked producer success and nothing more: every piece of the declared primary work
settled and every required gate's decision covers the exact bytes the run ended holding. It is not a
claim that the work was useful, or that anyone reviewed and accepted it -- those are separate events,
recorded elsewhere, never inferred from this status.

A run executed on the durable (Temporal) engine is `unknown` by construction, whatever it did: its item dispositions
live in the workflow's own state on the server and nothing reads them back, so the record says "this engine cannot
say" rather than leaving the field absent. A deployment on that engine therefore holds `unverified` for every
workflow however often it runs, and rises above `unverified` only through approval, or through runs on the
in-process engine.

`trust_level` reads that field. Only a completed run whose result is `succeeded` or an explicit `noop` counts toward
machine confirmation. Every other completed run counts in `unconfirmed_runs`. A workflow that declares no result
therefore stays `unverified` however often it runs. Approval is untouched and still independent.

**`recent_runs`** (the chat read) says both halves per line, `ran <outcome>, checked outcome <status> (n/m succeeded,
…)`, and says plainly when a run predates the field. **`run_values`** answers what the run recorded and is not a check
of any kind: a full list of values there is no evidence at all about whether the work was any good.

## A measured pilot trial: what it spends, and what its run is graded as

`features/evals/infrastructure/budgeted_model.py` and `features/evals/domain/outcomes.py` are what turns a person's journey
through the Studio into evaluation evidence: one persisted budget over every model request the
journey made, and one graded outcome over the run it archived.

### The Studio on :8770 cannot be metered as the code stands

Read this before anything else in this section, because everything after it is conditional on it.

The environment the Studio's own journeys run in is composed by `session`
(`harness/src/harness/features/authoring/wiring.py`) out of `SessionCollaborators`. That record has
no field for a `wrap_model` hook, `session` does not pass one to `harness_session`, and `wrap_model`
appears in no container registration, no setting and no entry point. **There is no supported way to
put a meter on the harness serving :8770.** A person who starts the stack, opens the Studio and
drives a journey through it is spending unmetered, and no call `start_development_pilot` can make
changes that.

**The only metered environment today is one a caller composes itself**, through
`harness_session(..., wrap_model=meter)` or `build_environment(..., wrap_model=meter)`, and then
drives the journey through that environment rather than through the Studio. A pilot opened beside the
Studio and a journey driven inside it are two different processes' worth of models, and the meter is
on the wrong one.

Making the Studio meterable is deployment wiring: a `wrap_model` field on `SessionCollaborators`,
filled from a container registration that is empty unless a pilot is open. It reaches every session
in the process whether a pilot exists or not, which is why it is not done here.

### The budget every request asks first

`BudgetedModel` is a pydantic-ai `WrapperModel` mounted on the designer, worker, reviewer and grader
models of a measured trial. It admits through `core.ai.evaluation`'s budget record before the
provider is reached, and settles exactly once afterwards. Both request paths go through it:

| Path | What settles it | Charge |
|---|---|---|
| `request` returns | the call itself, after `super().request` | the resolver's figure for that response |
| `request` raises | the exception path, shielded from cancellation | unresolved |
| `request_stream` runs to the end | the context's own exit, from `stream.get()` while `state == "complete"` | the resolver's figure |
| `request_stream` closed early, cancelled, incomplete or interrupted | the same exit, once | unresolved |
| `request_stream` iteration raises | the inner exception path | unresolved |
| entering the provider context raises | the outer exception path | unresolved |
| the context exits after any of the above | nothing. A local settled flag and the record's own idempotency make it one settlement | unchanged |

Admission happens before the provider context is entered, so a refused seventeenth request never
reaches a provider at all. Every settlement runs shielded from cancellation, the known charge as
well as the unresolved one, and a cancellation that stops one from being confirmed leaves the
request in flight, where it blocks the next admission rather than vanishing. Nothing here proves a
zero charge: a provider that failed after billing looks exactly like one that failed before.

Two more of a wrapped model's paths reach a provider, and a budgeted model refuses both rather than
forwarding them: `count_tokens`, the round trip a run asks for when it counts tokens before a
request, and `compact_messages`, a billed generative request on the providers that implement it.
Neither is admitted or settled, and a coverage manifest is structurally unable to see either,
because both live inside a model the manifest already counts as budgeted. Under a budgeted scope
they raise `UnmeteredModelCallError` before any provider call. The harness sets neither today.

The deployment's own `SpendLimits` (`$2` per run, `$25` per day) stays mounted and independent. It
refuses in a capability hook, before a request reaches this wrapper, so the stricter of the two
brakes is the one that stops a run and the record never carries a request the other brake refused.

### Preflight, before a live trial runs

`check_pilot(preflight)` refuses a trial that could not be measured honestly, and
`start_pilot_cycle` opens the cycle only after it passes:

- every generative model path is budgeted and no path is unmetered (`require_full_coverage`), so an
  external CLI agent or any unwrapped model path refuses the trial rather than being measured around,
- every participant verifies the same canonical state root on the same POSIX device and can take a
  cooperative lock on it. A remote Temporal worker, which cannot read that device at all, is refused,
- the record the cycle will open on is that same root and that same device. `pilot_preflight(record, ...)`
  reads both off the record rather than taking them beside it, and `start_pilot_cycle` refuses before
  opening anything when they differ, so a preflight verified against a configured root cannot be
  measured in a directory nobody checked,
- every configured role's model has a price the same resolver that settles a response can name.
  `roles_without_price` probes each one and an unpriced role refuses the cycle before its first
  request.

The limits a development pilot may opt into are `WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS`: USD 10 per
cycle, USD 2 per trial, 180 seconds, 16 requests and 3 attempts. The dollar figures are accrual stop
thresholds, not ceilings, and a crossing call records its overshoot.

### What the archived run is graded as

`ArchivedRunOutcomeQueries` reads `RunArchiveRecord.result` and grades four dimensions apart. None of
them is inferred from another, and none of them invents a success:

| Dimension | Read from | pass | fail | unknown |
|---|---|---|---|---|
| `structural_validity` | `record.outcome` and the result's counts | completed, counts add up | counts disagree, or the execution failed or stopped | never, since every archived run carries its result |
| `workflow_design` | the `WorkflowIntent` beside the workflow and the `WorkflowResultSpec` it declares | the request is stated, all four expected answers are written, a result is declared | the intent still owes an answer, or the workflow declares no result | no intent was recorded |
| `evidence_support` | the required `OutcomeCheck`s | every required gate accepted the value the run ended holding | a required gate refused | a required check could not be put. `not_applicable` when the result requires no acceptance |
| `task_success` | `result.status` and the case's own criterion | `succeeded`, or `noop` where the case permits no work | `partial`, `failed`, or a `noop` the case does not permit | `status == "unknown"` |

A required gate that spent its last reach is the one place a reviewer's own words reach the checked
outcome: the refused check carries the `feedback` it gave and the `requested_repair` it asked for,
and the run's summary names the repair. A gate that refused and was then satisfied carries the
acceptance instead, because that is the decision the run ended on.

A missing legacy result stays unknown and is never filled in. A completed run whose result is
`partial`, `failed` or `unknown` cannot pass task outcome however much of the work landed. A skip is
never work done. Owner acceptance is not among these four: it is a separate append-only event, and a
graded outcome carries none of it.

The post-pilot benchmark does not call this reader. `harness evals outcomes compare` grades each
candidate slot with the benchmark's own grader over the run's summary text, and reads only the run's
checked status beside it (see [The shipped grader judges a string](#the-shipped-grader-judges-a-string-and-a-sealed-benchmark-is-expected-to-replace-it)).
None of the four dimensions above reaches a benchmark report.

`usage` comes from the run's own spend record, with `cost_usd` left unknown while any row of it is
unpriced. That record folds rows into totals as it writes them and keeps no count of the responses
behind them, so `requests` stays at zero rather than reporting a number nothing holds.

### The development cases

`WorkflowAutomationTaskRunner` performs a case by reading the run the journey left behind, and
receives only `EvaluationInput(case_id, input)`. A case whose trial never ran, or whose run has left
the archive, raises `EvaluationError` so the slot is recorded as a failed trial rather than scored.

The cases themselves are template data, in
`templates/repo-docs/evals/workflow-automation-development.v1.json` beside its stamp. Every case is
author-visible development evidence, the stamp names no held-out cases and no verified holdout, and
each case's grader labels live in a `labels` block that never crosses into a run. A caller passes
the path in. Nothing in this app knows where a template keeps its evaluation data.

A case may also name the kind of work it is about, in `task_family`. That name is the set's own word
for it and is read here as an opaque label: nothing in this app knows what any of them mean. **The
set that owns the vocabulary is the set that closes it.** The dataset file declares its own
`task_families` beside `name` and `version`, `WorkflowAutomationDataset` refuses a case naming one
the file did not declare, and `require_stamped` then cross-checks that declaration against the stamp
beside it, which is a real check between two files written independently rather than a comparison of
a list with itself. The v1 set declares two, the research decision brief and documentation analysis,
beside the cases that are about the journey itself and name none.

### What a preflight proves, and what it does not

**The two dollar stops bind only while the meter is on the models that actually run.**
`BudgetedModel` is the only thing that puts a figure on a cycle at all. A journey whose models are
unwrapped spends real money while the record accrues nothing, and every refusal below passes while it
happens.

Of the three things a preflight refuses on, none of them establishes that:

| Check | What it actually establishes |
|---|---|
| metering | The meter handed in is this cycle's, was built over this very record, and **has been asked for at least one model**, each of which came back budgeted. The last of those is a length test on a list the meter appends to when it is called. One call by hand satisfies it. Nothing here can see which environment did the asking, or which environment the journey will run through. |
| pricing | The model the owner **named** for each role has a resolvable price. It says nothing about the model the role actually runs, and nothing about whether the price is current. |
| coverage | The roles the owner **declared** are the roles the owner **declared**, since `PilotAdmission.coverage` builds both sides of `require_full_coverage` out of `role_models`. It catches a declared path outside them and nothing else. |

So a passing preflight says: the deployment the owner described could be measured, and a meter for
this cycle exists and has been called at least once. Whether the models that run are that meter's is
the owner's to guarantee, and "The Studio on :8770 cannot be metered as the code stands" is why it
cannot be checked here.

### What `PilotMeter` covers, and what it never reaches

`PilotMeter` is a `StepModelWrapper`, and `build_environment` asks a `StepModelWrapper` about exactly one
thing: the three step-agent tiers. **Step agents are the only models it can ever reach.** Three
generative paths in the same process never meet the hook:

| Path | Built at | Metered |
|---|---|---|
| The Studio chat agent, which is the design conversation | `harness/src/harness/composition.py`, `build_agent(TierModel("orchestrator", tiers), ...)` | No. `build_agent` takes a model and never wraps it. |
| Every delegated worker | `harness/src/harness/features/authoring/workers.py`, `{tier.key: TierModel(tier.tier, tiers) for tier in TIERS}` | No. |
| The CLI workflow process | `harness/src/harness/composition.py`, a separate composition root | No. |

Read that against the journey a pilot grades: a person drives the Studio, the designer drafts, the
workflow runs, a reviewer repairs. Only "the workflow runs" is step agents. **The drafting and the
repairing are the chat agent and its delegated workers, and neither is ever metered.** A correctly
mounted meter still seals `metered_roles = ("worker", "reviewer", "designer")`, and the `designer`
there is a step agent's **helper tier**, not the design conversation. A later reader of that seal
should not read it as the designer's work having been measured.

**What does bound the unmetered half:** `_default_spend_limits` (`harness/src/harness/composition.py`)
mounts `$2` per run and `$25` per day, warned at 80%, on the chat agent, its whole worker tree and
the step agents alike. That is a separate brake with different numbers, priced by `price_response`
rather than by the evaluation record, and neither side reports the other's figures. It is what stands
between an unmetered journey and an unbounded bill, and it is not the 10.00 this section puts in
front of the owner.

### A live pilot, and what one trial is measured by

`start_development_pilot(record, request, journey, metering)` is the one way into a pilot. It loads
the stamped dataset, requires an answer about metering, seals the pilot, derives the coverage and the
pricing from the deployment's own configured roles, refuses anything unmeasurable, opens the cycle
and hands back an `OpenedPilot` carrying the handoff, the preflight, the dataset and the runner.

**`metering` has no default and there are only two answers.** A `PilotMeter` bound to this pilot's
own cycle and record that something has asked for a model, or an `UnmeteredPilot` carrying a written
reason for running with nothing measuring. That reason is stripped and a blank one is refused, since
it lands in a permanent seal. The unmetered answer is recorded as `unmetered_reason`, and
`FrozenPilot` refuses to be both, so a seal always says which it was.

`PilotMeter` answers the hook with a `BudgetedModel` per tier: the worker tier spends as `worker`,
the orchestrator tier as `reviewer` and the helper tier as `designer`. It remembers every model it
was asked for, and `for_trial` moves them all onto the trial now being performed. Before any trial has
begun the models name a trial nothing ever opens, so an early request is refused by the record rather
than charged to a real one.
`tests/integration/test_automation_journey.py::test_a_metered_environment_puts_every_step_agents_model_through_the_record`
composes a real environment through the hook and runs a real step, and the request lands on the cycle.

**The one order that produces a metered run**, and it is the composed-environment path this section
opened with, not the Studio:

1. load the stamped dataset and name the cycle with `pilot_cycle_id`, which is pure over the dataset
   and the inputs and so can be named before anything is built,
2. build a `PilotMeter` over that cycle and the budget record,
3. compose the environment **through** `harness_session(..., wrap_model=meter)`, and drive every
   journey of this pilot through that environment and no other,
4. `start_development_pilot(...)`, which refuses a meter nothing has ever asked for a model. It
   cannot check that step 3 happened, only that something asked,
5. `meter.for_trial(trial_id)` between trials, never during one.

Step 3 is the whole of the metering and none of it is verified from step 4. A journey driven through
the Studio while a meter sits on some other environment satisfies every check on this page and is
unmetered from end to end.

**It opens exactly one cycle per pilot, and it names that cycle itself.** `pilot_cycle_id` derives
the id from the pilot's own content, its cases and its inputs, so opening the same pilot again
continues the same cycle. That is what makes `cycle_cost_stop_usd` a bound on the pilot rather than
on one sitting of it: a caller picking cycle ids of its own would get that threshold once per id.
Change a case, the prompt, the implementation, the tools or the configuration and it is a different
pilot with a cycle and a threshold of its own. That is correct about what a pilot is, and it is also
how a total walks past 10.00 without anybody deciding to spend more: **a corrected word in
`configuration` between two sittings is a second pilot with a second 10.00.** Read the figure with
that condition attached.

`_open_development_pilot` is the step underneath, and it is private on purpose: a function that takes
a caller's own cycle id can be called as often as a caller likes, each time under its own threshold,
so `start_development_pilot` is the only public way in. It builds the preflight off
the record itself, checks it, and opens the cycle under `WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS`.
The limits are not a caller's to choose. A cycle already open under that id is continued rather than
opened again, since its counters are the whole point of a persisted record, and every check runs
again on the way back in. Two processes opening one cycle at the same moment meet that same
continuation rather than a complaint about limits, because the only thing they differ on is the
instant each read the clock at.

`PilotAdmission` is the deployment as the owner describes it. It carries `role_models`, every model
role a measured trial makes requests through, and `unbudgeted_model_paths`, anywhere else this
deployment reaches a model from. Two things come out of it, with the caveat in the table above:

- **Coverage.** `PilotAdmission.coverage` names each role's path `role:provider:model` and budgets
  all of them. A path outside them has to be declared, and declaring one refuses the pilot rather
  than measuring around it
  (`tests/integration/test_automation_journey.py::test_a_model_path_this_deployment_does_not_budget_refuses_the_pilot`).
  Leaving one out is the way this check is fooled, and it is a statement about the owner's own
  deployment rather than anything the code can read.
- **Pricing.** `_open_development_pilot` probes every configured role through `roles_without_price`
  against the same resolver that settles a real response, `priced_cost`. Nobody tells it which roles
  are unpriceable, so a caller that has not noticed cannot open a monetary-bounded cycle by staying
  quiet
  (`tests/integration/test_automation_journey.py::test_a_role_the_deployments_own_resolver_cannot_price_refuses_the_pilot`).
  It asks whether a figure can be named, never whether the figure is current.

`LivePilotTaskRunner` then reads each case the same blind way the archived runner does and puts the
trial's own measured spending beside it. `trial_usage` decides where each figure comes from:

| Figure | Read from | Why |
|---|---|---|
| `requests` | the budget record's own trial counter | the record admitted them, so it is the only thing that counts them |
| `cost_usd` | the budget record's measured trial charge | it is what the trial is measured by, not the deployment's view of the run |
| `cost_usd`, with any unresolved charge on the cycle | nothing. It is unknown | the priced part of an unresolvable charge is not the charge |
| `input_tokens`, `output_tokens` | the spend record the run itself wrote | the only record that counts tokens at all |

The two halves are scoped differently. Requests and cost cover the whole trial, which spans the
journey including its designer turn. Tokens cover one run. A `token_source_run` diagnostic names
that run, so a reader of the usage cannot take the two for figures of one record. The overshoot past
a stop threshold, the attempt count and an unresolved charge ride out the same way, as observations
nothing grades. A trial the cycle never opened is a failed slot carrying whatever usage the run
itself recorded, never an empty one.

### Freezing a pilot, and handing it on

`freeze_pilot(dataset, inputs, cycle_id=..., baseline=..., limitations=...)` seals what ran, and
`start_development_pilot` calls it before anything opens.
`PilotInputs` is content rather than names for content, and each part is hashed over its own bytes:
the prompt, the implementation, the tools (as a set, so a reorder is the same pilot and a tool taken
away is not), and the execution conditions together with the limits. The dataset is hashed whole.
The result is a `FrozenPilot` carrying a `RunIdentity`, a `tool_fingerprint`, the baseline it is read
against and the limitations, which is never empty: a pilot claiming no limitation has not been read.
It also carries `metered_roles`, the roles a meter was actually mounted on when the pilot opened, or
`unmetered_reason` when none was. Exactly one of the two, so a later reader of the seal can tell
whether its limits bounded anything.

`PilotHandoff` carries that seal beside whatever has actually been measured under it. Its `outcomes`
are empty until the pilot runs, and an empty handoff reports nothing rather than a measured zero:
`output_refs` is empty, `correction_seconds` is `None`, and `measured` is false. Once there are
rows, `correction_seconds` totals only when every row recorded one (a row nobody timed is unknown,
never zero) and `usage` leaves the cost unknown while any row's is.

The baseline and the limitations are whoever freezes the pilot's to write. For the author-visible
development pilot they are:

- **Baseline.** The same work done by hand: a person reads the week's reviews and the initiative
  documents and writes the pages or the brief themselves. Nobody has measured how long that takes,
  so the baseline is a description and `observations` stays `None`. A described baseline is never a
  figure to compare against.
- **Limitations.** Every case is author-visible, so no result from this set is evidence about cases
  nobody wrote. The set is synthetic and unreviewed until an owner reviews it. Owner acceptance is a
  separate event and is unknown until somebody records one. The dollar figures are accrual stop
  thresholds, so a crossing call completes and its overshoot is recorded rather than prevented.

## The post-pilot benchmark: `harness evals outcomes`

The benchmark reads the runs a person's journeys archived, beside a baseline, and says whether the
automation produced more useful work. It does not read the frozen pilot: nothing on its path opens a
`FrozenPilot`, a `PilotSeal` or a `PilotHandoff`. The candidate's `RunIdentity` is asserted by the
command's composer from the deployment's worker tier at the moment the benchmark runs, not read from
a seal or from the archived runs, so a pilot that ran on another model is not caught by
`identity_drift`. The benchmark is a paid, irreversible act, so the command is built to refuse before
it measures. Two verbs:

```bash
harness evals outcomes replay <dataset.json> <stamp.json> --inputs <inputs.json> \
  --report-root <dir> --shared-budget-root <dir>

harness evals outcomes compare <sealed.json> <custody.json> --journey <index.json> \
  --inputs <inputs.json> --report-root <dir> --shared-budget-root <dir> \
  --objective task-success|measured-generative-model-cost \
  --cycle-cost-stop-usd 10 --trial-cost-stop-usd 2 \
  --trial-deadline-seconds 180 --trial-request-limit 16 --trial-attempt-limit 3
```

`--inputs` names a `BenchmarkInputs` file carrying the exact content every fingerprint is taken
over: the workflow, the prompt, the model and its version, the tools, the sources and the grader.
Each field is content, never a name for content, so two benchmarks whose prompt differs by a word
have different prompt fingerprints and a reader can see why. `--shared-budget-root` is the one POSIX
directory every participating process opens the record in, and the command verifies the device it
can actually see and takes a real cooperative lock rather than claiming it can.

**The five thresholds are all or nothing.** State every one of them or state none. A benchmark under
four of them has one dimension nobody bounded, and filling the fifth in from a default would put a
threshold nobody asked for on the owner's money. A benchmark whose models reach a provider and that
states none of them is refused as `limits_absent`.

### What blocks a benchmark, and what each refusal means

| Refusal | What it found |
|---|---|
| `development_dataset_only` | The set declares itself author-visible, so no result over it is independent generalization evidence |
| `custody_absent` | The sealed dataset or the custody record is not on disk, so there is nothing an independent custodian has sealed |
| `custody_unverified` | The audit found no verified held-out claim: a seal mismatch, a broken freeze order, an authorship overlap, a recorded author or implementer exposure, a collision or an era-invalidating freshness verdict |
| `limits_absent` | A model that reaches a provider was composed with no stated stop thresholds |
| `remote_temporal_worker` | Temporal workers serve another host, so their model requests reach no record of ours |
| `no_declared_model_path` | The composition declares no generative model path, and every benchmark makes at least a grader request |
| `no_declared_grader_path` | The grader does not ask through any declared grader-role path, so its requests are outside the cycle the stop thresholds bound |
| `unstated_usage_source` | One of the three declares no model path while claiming this record measured it, or declares paths while claiming another record did. The two states are the whole of what one may be |
| `unrecorded_model_requests` | One of the three reported more model requests than the record admitted for that trial, so some were made where nothing could meter them. Refused after the slot rather than before it |
| `undeclared_model_path` | An arm requests through a path the benchmark never declared, or a declared path no arm requests through |
| `unmetered_model_path` | A generative path the benchmark holds does not admit on this record and this cycle |
| `unbudgeted_model_path` | A declared path is outside the budgeted set |
| `pricing_unknown` | A configured role's model has no price the same resolver that settles a response can name |
| `unverified_state_root` | A participant verifies another root or another device than the record measures on |
| `no_cooperative_lock` | A participant cannot take a cooperative lock on the shared root |
| `arms_identical` | Both arms carry one identity, so nothing is being compared with anything |
| `identity_drift` | The model, the version or the grader differs between the arms, or from what the seal froze |

Every one of these except one is decided before a cycle is opened. A blocked report is filed either
way, and a run refused on its cases never builds the models it would have run. The exception is
`unrecorded_model_requests`, which cannot be: it is a reading of what a slot actually spent, so it
fires after that slot and stops every later one.

### Metering is discovered, and what the arms state is held against the declaration

This command is the one caller that can mount the meter at all, for the reason this page opens with:
the environment a person's journeys run in is composed once when the process boots. `_DeploymentComposer`
puts `BudgetedModel` on the two paths that ask a model during a benchmark, the simple baseline and
the grader. **The workflow arm is not one of them.** `WorkflowAutomationTaskRunner` reads the runs a
person's journeys already archived, so it asks nothing now, and whatever those journeys spent was
admitted under the pilot's own cycle. Under `measured-generative-model-cost` the two arms' dollar
figures come from two different records, and nothing folds them together.

Three separate checks stand behind the word metered, and they read different things. Keeping them
apart is the point, because only the third reads behaviour.

**One, the classifier reads the objects.** `discovered_coverage` looks at the model object behind
each declared path, so a path somebody forgot to mount lands in `unmetered_model_paths` and refuses
the run. A caller cannot pass it by saying it mounted something.

**Two, the declaration is held against what the two arms and the grader say.** Each one answers two
questions about itself: `request_paths`, the declared entries it asks through, and `usage_source`,
whether the figures it reports were admitted here (`this_record`) or measured by some other record
(`another_record`). Four rules read those answers.

- `unbound_paths` compares `composed.models` with the union of the three `request_paths`, object for
  object and in both directions. An object handed to one of them and never declared is refused as
  `undeclared_model_path`, and so is a declared path none of them asks through.
- `unstated_usage_sources` refuses as `unstated_usage_source` anything declaring no path while
  claiming `this_record`, and anything declaring paths while claiming `another_record`. The two
  states are the whole of what one may be. The exemption is real, because
  `WorkflowAutomationTaskRunner` reports what the pilot's own record measured, and stating it is what
  stops it being taken in silence. **Declaring nothing is otherwise the cheapest way past every check
  here and it costs no lie at all.**
- `grader_off_the_record` refuses as `no_declared_grader_path` unless the grader's own
  `request_paths` contains a declared entry with `role == "grader"`. It asks the grader rather than
  the declaration, because a declaration carrying a grader path says only that somebody built one.
  **This apparatus requires a model-backed grader on the record.** A grader that reads a record
  rather than asking a model cannot be run here.
- A declaration with no path at all is refused as `no_declared_model_path`, and `model_type_of(())`
  answers `live` rather than `test_double`.

**All four compare statements.** One that misstates its own `request_paths` or its own
`usage_source` is not caught by any of them. Only `SingleRunTaskRunner` and `ModelTaskGrader` cannot
misstate their paths, because they build their agent from the declared entry itself. A custodian's
own arm can say anything.

**Three, what was spent is read against what the record admitted.** After the arm and again after the
grader, whether either returned or raised, `unrecorded_requests` compares the requests it reports
with the requests the record admitted for that trial. A failure carries its own usage
(`EvaluationError.usage`) and is read the same way, because one `raise` after spending would
otherwise skip the reading entirely. A request that never touched the record cannot appear in the
record's count, so reporting more than the record saw means asking somewhere nothing could meter, and
the slot is refused as `unrecorded_model_requests`.

**Read the scope of this one exactly.** It is the only check that reads behaviour, and it reads less
than its name suggests. It is asked only of one that claims `this_record`, and it reads one direction
only: reporting fewer requests than were made passes, including reporting none. What it establishes
is this and nothing wider: **one that declares at least one path cannot claim to have spent more than
the record admitted without stopping the sitting.**

**It detects rather than prevents.** The requests of the slot it fires on are already made,
unmetered, before the counts can be compared, and no threshold bounded them: `EvaluationLimits` is
enforced inside `BudgetedModel` and these are by definition requests no `BudgetedModel` saw. The only
bound is the number of slots, and that number is one, because every later slot refuses before its arm
runs. What one slot is worth is whatever it chose to do inside it. The refusal is named on the report
and the status is `inconclusive` rather than `measured`. A loud refusal over money nothing counted is
the point.

**What to rely on.** Two sentences, because there are two cases.

For `harness evals outcomes compare` as shipped: nothing inside that command can reach an unwrapped
model. It declares both paths that ask a model, mounts both on the record, builds both agents from
the declared objects, and a later edit that swapped an object in would be refused before a cycle
opened.

For a composition a custodian supplies: the rules refuse a path asked through and never declared, a
path declared that none of them asks through, one that declares nothing without claiming another
record's figures, a grader that does not ask through a declared grader-role path, and one that says
its figures come from this record and claims more requests than the record admitted. They do **not** catch one that under-reports its own usage,
one that reports honestly before its last request, or one that asks after it has returned. Each of
those spends real money with nothing refusing. `OutcomeStateReport.usage_declarations` names all
three, their declared paths and their stated usage source, so a reader can see which of them the
record covered rather than assume it covered all of them.

### The shipped grader judges a string, and a sealed benchmark is expected to replace it

`ModelTaskGrader` asks a model for `{success, reason}` over the case's own success criterion and the
arm's answer text. That is all it does. It makes no evidence check, it reads no `DeliveredEvidence`
from the trial, it resolves no artifact, and `TaskGrade` carries no third verdict, so it cannot say
`cannot_judge`. A benchmark whose cost conclusion or usefulness conclusion matters wants a grader
that reads the immutable delivered evidence the trial actually produced, which is what
`engram.evals.outcomes` builds for the retrieval side.

This is deliberate rather than an oversight. `BenchmarkArms.grader` takes any `BenchmarkGrader`, so
a custodian who seals the cases also supplies the grader they are willing to be judged by, and the
grader is fingerprinted into the seal like every other input. The shipped one is a working default
for exercising the apparatus, not the grader a sealed benchmark should run under. Its fingerprint is
over `GRADER_IDENTITY`, the class name and the instructions it is given, so a `BenchmarkInputs` file
for the shipped grader carries that exact text as its `grader` field.

**A guard reads the candidate's checked status.** Whatever grader runs, `checked_grade` lets a
candidate slot's pass stand only when the archived run's checked status is `succeeded`. A pass over a
run that `failed`, ended `partial`, did nothing (`noop`), established nothing (`unknown`) or carries
no checked outcome at all becomes a fail whose reason names that status. A sealed `TaskCase` carries
no statement that doing nothing is the right answer, so a `noop` never passes here. The guard never
turns a fail into a pass, and it leaves the baseline's grade as the grader gave it, because the
baseline runs no workflow and has no checked status. The grader itself still judges text. A
succeeded run whose summary reads badly can still fail, and nothing of the run beyond its status is
read.

### What the report is allowed to claim

`empirical_status` is the whole of what a reader may take from a report. `blocked` is nothing
measured and no claim of any kind. `inconclusive` is something measured and not enough of it.
`measured` is every planned slot graded with no unresolved charge. Beside it, `model_kind` says what
the measurement ran on: `live` reaches a provider, `test_double` spends nothing so its figures are
apparatus output, and `unbuilt` is a benchmark refused before any model existed. `statistical_claim`
is always `descriptive_only` and `recommendation` is always `none`, written into the record so a
reader sees them rather than inferring them from an absence.

Every slot is filed under its own name in `<report-root>/<report-id>.trials/` before any verdict is
folded over it, carrying its requests, attempts, measured charge, overshoot, unresolved state and
whichever rule refused it. The grader's own request belongs to the slot it graded, so a slot is
filed after grading rather than before, and a slot whose grader was refused is filed with that
refusal on it. A slot that was graded also carries its `grade` (success, reason and the grader's
own usage) as it stood after the checked-status guard.

A report that ran carries what the paired comparison concluded: `verdict` (`accepted` or
`rejected`), `verdict_reasons`, `baseline_success_rate` and `candidate_success_rate` over every
planned slot, `regressions` (the cases where the candidate passed fewer repetitions than the
baseline) and each arm's `baseline_usage` and `candidate_usage`. A blocked report carries none of
them. The verdict is descriptive evidence for review, and `recommendation` stays `none` beside it.

A report is written once under a name derived from its own content and its sitting, and a second
write under that name is refused.

## Resolving a reference: `POST /api/runs/history/{run_id}/artifacts/resolve`

The one read-only route a value reference resolves through. The body is `{"ref": ArtifactRef}` and nothing else. There
is no URL form of this question and no live state is ever read. The answer is an `ArtifactDocument`, exactly
`{"ref": ..., "value": ...}`.

The Studio reaches this route only through a closed artifact action a presentation declared, never by building a
request of its own (see [What UX, EV, and WA hold](#what-ux-ev-and-wa-hold) below). The references it sends are the
ones the selected run minted, so what a person acts on is what the run fixed.

A declared artifact is resolved against the workflow snapshot the record itself carries: the node, turn and top-level
field that workflow named, out of the archived `values` (or `params`, for an artifact wired from a parameter). A
reference naming no declared artifact falls back to the values this run's gates reviewed, which are kept on the record
under the address the gate captured them at, the producing node and turn of its `reviews` wire (or
`parameter:<name>`). That is how a refused draft stays readable after the run log it was captured beside is gone, and
it is swept exactly when the record is.

Refusals are specific: **409** for a reference naming another run than the path, a reference addressing a node or turn
the declaration does not name, and bytes whose digest disagrees with the reference. **404** for a run nothing
archived, an artifact the workflow never declared with no reviewed value at that address, and a value or a declared
field the record does not carry. **415** for a text media type over a value that is not a string. **413** for a value
whose canonical bytes pass 1,000,000. A body that is not a reference is a **422** before anything is read.

**What a client of this route owes.** The route is the whole surface: a reader in another application reaches it over
HTTP at a base URL its own application configures and trusts, and imports nothing from this app. Studio's
`HarnessArtifactResolver` is that reader. It sends the reference, and before it hands the answer on it checks the answer
against the reference it sent: the returned `ref` equals the one asked for field for field, the value's canonical
digest is the one the reference names, the value matches the declared media type (a text type carries a string), and
the canonical bytes are within 1,000,000. Anything else is an unresolved reference, not a value. A base URL that is
not the configured one is never followed, and no URL ever arrives inside a reference, so nothing a run produced can
send a reader somewhere its own application did not choose.

## Recording an owner review: `POST /api/runs/history/{run_id}/review`

The one route an owner's accept or reject decision is recorded through. The body is `core.ai.evaluation.contracts.OwnerReviewRequest`
(`review_id`, `actor`, `outcome`: `"accepted" | "rejected"`, `reason`, `artifact_refs`, at least one) and nothing else,
same closed shape as the artifact-resolve body above: a URL, a filesystem path or an HTTP method arriving inside it is
refused before anything is read. The answer is an `OwnerReviewResult`: the minted `OwnerReview` (the request, its
route source, and when it was decided) and `created`, whether this call was the one that minted it.

Every reference is resolved and confirmed against this run's own archived record before anything is written, through
`RunArtifactResolver`, the same `_resolved` the artifacts-resolve route answers with (one mapping, never
reimplemented). Refusals carry the same statuses: **409** for a reference naming another run than the path or bytes
whose digest disagrees, **404** for a run nothing archived or an artifact this run never fixed, **415** for a text
media type over a value that is not a string, **413** for a value past the canonical ceiling. A review id already
filed under a different request, run or route source is also a **409**: a review, once minted, cannot be overwritten,
only repeated identically or refused. An identical retry (same review id, same request) returns the very event
first minted, even once the artifacts it named can no longer be resolved: `core.ai.evaluation.contracts.OwnerReviewService`
checks the stored event's own fingerprint before touching a resolver at all. `actor` is whoever the request says
decided: text the caller supplies under the current local-owner model, never verified identity.

Committed reviews are read back on the full record above, `owner_reviews`, in the order they were appended. A run can
carry as many as people give it, each under its own review id, and a later one is another event rather than a
correction of the one before it: reading them back shows every reading the run got, with the references and digests
each one actually named. Nothing a run did to itself is touched by any of them. Its `outcome` and its checked
`result` say what the execution was observed to do, and a review says what a person made of the result, which is why
a rejection leaves a `succeeded` run `succeeded`. Committed reviews never reach a listing row. **WA** (workflow
automation) calls this route exactly as it resolves a reference: through its own HTTP adapter, over one
application-configured trusted base URL, importing no harness module.

The Studio SPA reaches it through the closed `record_owner_review` artifact action on a run's result. Pressing the
action resolves every declared reference through `artifacts/resolve` first and opens a form over exactly what came
back, so what is posted here is the host's own references and never something the screen assembled. The person types
the name recorded as `actor`, under the same local-owner model this route documents, and the form mints one
`review_id` per opening, so a retry after a lost answer meets the event it already wrote. Where the SPA shows those
reviews, it shows them apart from the checked result and apart from the gate decisions, and a run with none reads as
acceptance unknown.

## The intent and the presentation: two records, one card

A saved workflow carries two optional records beside its spec, and they answer different questions. Neither is part of
the workflow. Both are written only when somebody wrote them, and an absent one is stored absent, so a workflow saved
without either holds exactly the keys a workflow saved before either existed holds.

**The presentation** (`core.ai.workflows.contracts.WorkflowPresentation`) is the author's words about the workflow, for the
surfaces a person meets it on: what it is for, what a run produces, what the person does during one, what to reach for
something else for, plus the label each input is asked for under, the recorded-output facts a reader is shown, and the
artifact actions offered. It is what a reader is shown. It is checked against the spec before a library keeps it, because
an input label naming no parameter and an action naming no declared artifact are both nonsense.

**The intent** (`core.ai.workflows.contracts.WorkflowIntent`) is what the person who wanted the automation asked for: the request
in their words, the result they expect back, one concrete example of a run that would count as a success, what a run
does with nothing to work on, what it does when it cannot finish, whether a person starts each run or a schedule does,
and the assumptions somebody made on their behalf. It is what was asked for. It is kept as it was stated and checked
against nothing, because a request is not a claim about the graph.

Both live on the card, never on the spec. `spec_identity` hashes the workflow alone, so writing, rewriting or removing
either changes no identity and orphans no evidence. Neither grants anything: an intent selects no work, names no tool,
and leaves `StepRegistry.grants` and the fingerprint's grant digest exactly as they were. In a library they ride in the
workflow page's own frontmatter under `presentation` and `intent`, and rewriting a card's prose (`redescribe`,
archive, restore) leaves both where they are.

While a workflow is being drafted both live on the harness `Draft`, which is kept in the session's `draft.json`, so a
restart between drafting and saving loses neither. An incomplete intent is a normal draft: only the request is
required, and `intent_issues(intent)` names which of `expected_result`, `success_example`, `empty_input_behavior` and
`failure_behavior` are still blank. It counts blank fields and reads no English for meaning, so an empty answer says
every field carries text and says nothing about whether the text is any good. It never refuses a draft.

The readiness gate a save refuses on (`card_readiness`) reads the card: the title, the summary and the author's four
sentences. It does not read the intent. A draft carrying half an intent saves, and a draft carrying none saves.

## Which record answers which question about a run

A run's page shows several things at once, and four of them are about how the run went. A reader who
takes one for another gets a plausible answer to a question they did not ask. Each row below names
the one field that answers a question. Nothing else answers it.

| The question | Read | Where it is |
|---|---|---|
| What was this started to do? | `run_title`, falling back to the workflow's card title and then to the workflow id | the live row, the history envelope and the archive record |
| Which conversation did it happen in? | `session_id` | the same three |
| How did the execution end? | `outcome`: `completed`, `failed`, `stopped`, or `live` | the same three |
| What did the run actually do? | `result`, the checked `Outcome`: its status, its one-sentence reading, the counts it is derived from, and each required gate's check | the same three |
| What did it leave behind? | `result.artifacts`, one reference per declared artifact, resolved through the resolve route when somebody asks for the bytes | the run's page |
| What did the run report about itself? | the values a presentation's `result_fields` names, read off the archived values under the author's own labels | the run's page only. A board row leaves `values` out |
| Was the work any good, and did the owner take it? | `owner_reviews` on the full record. Empty means nobody has said, which is unknown and not rejection | the full record |

Four readings that look alike and are not:

- **`outcome` is not `result`.** `outcome` says how the execution ended. `result` says what it was
  checked to have done. They come apart in both directions: a run stopped halfway can hold work that
  succeeded, and a run that completed can hold a `result` of `unknown` because the facts it would be
  derived from were never captured.
- **`result.status` is not a judgement.** `succeeded` means every piece of the declared primary work
  settled and every required gate's decision covered the exact bytes the run ended holding. Whether
  the result was worth having is `owner_reviews`, and a rejection there leaves a `succeeded` run
  `succeeded`. Nothing reconciles the two, because they are answers to different questions.
- **A recorded number is not a status.** The counters a presentation names are read off what the run
  wrote about itself. They sit beside the status word under their own labels and never move it. A run
  that recorded twelve notes and failed its gate is a failed run that recorded twelve.
- **A run's name is not its session's.** One session can host several runs, so the container's name
  could never have named any of them. Nothing on this side joins a session title onto a run, and a
  run nobody named reads as unnamed rather than borrowing one.

Each of those readings is served over the routes above. What the surfaces reading them may and may
not do with them is next.

## What UX, EV, and WA hold

Three surfaces read this runtime without owning any of it. Each holds exactly the types and routes
named below, and none of them may reinterpret what those types already say.

**UX** (the run and workflow presentation layer) reads `Outcome`, the lightweight
`core.ai.artifacts.ArtifactRef`, the run-level `result` (above), and `referenced_steps(workflow)`. It
renders `result.status` as what it checks, never as a claim about usefulness or that an owner
accepted the work. An artifact action it offers is one of a closed, first-party set of intents, each
naming a fixed handler and route, never a caller-supplied URL, HTTP method, or request body.

In the Studio that layer is the fleet board and each run's own page. A card and a run page are read in one order: what
the run is (its `run_title`, falling back to the workflow's card title and then its id) and what it wants from a person
(a waiting gate, or how a machine decided instead), then what it did (`result.status` in one word and one sentence,
with the counts the status is derived from), then what it left behind (one entry per `result.artifacts` reference),
then how it ran (elapsed, cost, who started it), and last the execution graph, folded away until it is asked for. On a
run's own page, a recorded-output fact a presentation's `result_fields` names is shown under its own label as recorded
output, read off what that run recorded, and never changes the status word beside it. A board row carries no recorded
output at all: a listing row leaves `values` out, so the counts it shows are the checked outcome's own. An artifact's bytes are read only when a person
asks for them, through `POST /api/runs/history/{run_id}/artifacts/resolve` with the reference exactly as the run minted
it. An action whose first-party handler is not installed in the deployment renders as unavailable and does nothing. It
never falls back to a route the surface invented.

**EV** (the evaluation layer) reads `Outcome` as machine-produced evidence of what a run's own facts
established. `succeeded` is checked producer success, stated above, never usefulness and never
acceptance. EV is the layer that scores usefulness and records owner acceptance, in its own
record, as events distinct from a run's checked outcome, never by reinterpreting `status`.

**WA** (workflow automation) calls `POST /api/runs/history/{run_id}/artifacts/resolve` through its
own HTTP adapter, over one application-configured trusted base URL, importing no harness module (see
"What a client of this route owes" above). It resolves every `ArtifactRef` it is handed before
using the value behind it for anything, exactly as EV resolves a reference before appending an owner
review.

## Gate modes (wait, orchestrate, auto) and the honesty rule across three columns

A run's own gates decide under one of three modes, `GateMode = "wait" | "orchestrate" | "auto"`
(`harness/domain/gates.py`, see [ADR 0008](../adr/0008-orchestrator-gates-and-run-gate-modes.md)): `"wait"` (default)
steps a `PersonGate` waiting for a person exactly as before. `"auto"` is automode, passing every gate the run reaches
with nobody asked. `"orchestrate"` hands a `PersonGate` to this run's own orchestrator instead of leaving it waiting.
Set at launch (`RunRequest.gate_mode` on `POST /api/run`, the `gate_mode` argument of the agent's own `run_workflow`
tool, a schedule's own `gate_mode`) or flipped mid-run via `POST /api/run/automode {"on": bool}` (which sets
`gate_mode` to `"auto"` or `"wait"`, and answers with the `auto_approve` it now reads, derived from `gate_mode`). A
durable run carries its mode in core's Temporal `RunRequest.gate_mode`, `"wait"` or `"auto"` only. A
workflow may also author an `orchestrator-gate` node in the graph itself, naming its own review brief for this run's
orchestrator to decide against. That gate is answered the same way under every one of the three modes. A run's
`gate_mode` only changes what happens to a `PersonGate`, the one kind that would otherwise wait on a person.

A `judge` node is answered by an inline step rather than by a person or this run's orchestrator directly: its `work`
names a step file whose typed output carries the verdict, and the step answers `approve`, `reject` with feedback, or
`cannot_judge`. A `cannot_judge` verdict steps the run waiting for a person exactly as a `PersonGate` would, the
reviewer's own words appearing above the workflow in the gate card so the owner reads why before deciding. Under
`gate_mode="orchestrate"` this run's own orchestrator answers that escalation instead, the same conversion a
`PersonGate` gets under that mode.

**The honesty rule, now three columns.** `gates_approved` means a person clicked approve. `gates_auto` means automode
passed the gate with nobody asked. `gates_orchestrated` means this run's own orchestrator decided it: either because
the gate was a custom `orchestrator-gate`, or because `gate_mode="orchestrate"` converted a `PersonGate` that would
otherwise have waited. **Trust derivation reads `gates_approved` alone.** A gate recorded in either of the other two
columns confirms nothing human, by omission from that one count, never by a flag a reader has to remember to check.
A run can be automode or orchestrate-mode from the start (a fully unattended schedule) or flipped mid-flight (an
owner walks away from a run they were watching). Either way the record it steps behind states plainly which gates
were actually seen by a person, which passed themselves, and which this run's own orchestrator decided.

## Schedule endpoints (`features/scheduling/controller.py`)

```
GET    /api/schedules                                                     -> {"schedules": [ScheduleOut, ...]}
POST   /api/schedules            {"workflow_id", "arguments"?, "cadence", "gate_policy"?, "gate_mode"?}
                                                                            -> {"schedule": ScheduleOut} (201) | 404 | 400
PATCH  /api/schedules/{id}       {"workflow_id"?, "arguments"?, "cadence"?, "enabled"?, "gate_policy"?, "gate_mode"?}
                                                                            -> {"schedule": ScheduleOut} | 404 | 400
DELETE /api/schedules/{id}                                                 -> {"ok": true} | 404
POST   /api/schedules/{id}/run-now                                        -> {"ok": true, "outcome": str} | 404
POST   /api/schedules/preview    {"cadence": str}                         -> {"next": [iso8601, iso8601, iso8601]} | 400
POST   /api/schedules/refresh-runs {"event_id", "knowledge_base", "sources_created"?, "requested_by_entry"?}
                                                                            -> {"repeat", "notified", "armed"} | 422 | 503
```

`POST /api/schedules/refresh-runs` is the studio's report of an ended refresh run (its `harness_forward` subscriber
posts it). A run that created sources arms every enabled `on refresh` schedule whose arguments select the knowledge
base and sends the owner one notification (`New sources`, such as `notes: 6 new sources (6 found by x:someone)`). The
event id is recorded in `coordination.event_deliveries` of the workflow library's database under
`schedules.on_refresh` in the same transaction as the arm, so a second report of the same run answers
`"repeat": true` and changes nothing. A run that created nothing answers at once and records nothing. A card the
library cannot read answers 503, records nothing, and the studio delivers again.

`ScheduleOut` is the schedule's own stored fields (`id`, `workflow_id`, `arguments`, `cadence`, `enabled`, `gate_policy`,
`gate_mode`, `session_id`, `created_at`, `last_run_at`, `last_outcome`, `next_run_at`) plus
`live`: where its runner session's run stands right now (`"running" | "paused" | "waiting"`, or `null` when nothing is
in flight), computed fresh off the session pool on every read, never stored. A schedule points at a workflow, not at
one version of it: `workflow_id` in the listing, and the version a fire launches, is the newest live version of the
workflow the row names (the library's `current`), resolved on every read and every fire. The row keeps the id it was
written with. `gate_mode` is the one stored gate switch (alembic `0072` folded the earlier flag into it). On a
deployment that runs on Temporal, a `POST` or `PATCH` that sets `gate_mode` to `"orchestrate"` answers 400 with words
saying the durable engine has no orchestrated gate. A fire of a schedule already stored that way launches nothing and
stamps `refused: ...` as its outcome, keeping its cadence.

`spend_today_usd` and `spend_week_usd` are `schedule_spend`'s own two figures (`features/running/spend.py`), computed
fresh the same way as `live` rather than stored. Each counts only a fire whose spend record names this schedule's own
runner (`"app:schedule:<id>"`): `spend_today_usd` sums since UTC midnight, `spend_week_usd` over the trailing seven
days, neither a calendar boundary.

**Cadence** is an interval (`"every 30m"`, `"every 2h"`, `"every 1d"`), a 5-field cron expression (`"0 9 * * 1"`, parsed by `croniter`), `"on refresh"` or `"after <workflow name>"`: `POST /api/schedules` and `PATCH /api/schedules/{id}` refuse anything else in
words, and `POST /api/schedules/preview` walks a candidate cadence forward three fires without storing anything, so a
cadence can be checked before it is saved. `workflow_id` is revalidated against the process's own environment on
create and on any patch that changes it. A schedule naming a workflow that does not check out here is refused, not
saved broken. `arguments` are admitted against the named workflow's own parameters at the same moment, and stored as
the JSON they were admitted as: see [Run arguments](#run-arguments-typed-json-admitted-once).

**Event cadences.** `on refresh` and `after <workflow name>` never come due by the clock. A schedule holding one
rests with `next_run_at` null until it is armed: the studio's report of a refresh run that created sources arms
every enabled `on refresh` schedule whose arguments select that knowledge base, and a run that ends `succeeded` or
`partial` arms every enabled `after <its name>` schedule that selects a knowledge base the run also selected. A
schedule's knowledge base is read through its workflow card's knowledge-base selector. The tick fires an armed
schedule through the same holds as any other, an overlap or a blackout keeps the arm whatever `catch_up` says,
and the fire leaves it resting. A change that sets an event cadence clears the next fire it held. A schedule
cannot fire after its own workflow.

**Gate policy** is `"wait"` (default: a gated fire waits for a person indefinitely) or `"fail_after:<seconds>"`, how
long **one reach** of a gate waits before nobody deciding counts as a refusal. Such a refusal is recorded with source
`timeout`, reruns the upstream step and steps the same gate waiting again, exactly as a person's rejection would. The
fire ends when the gate's own `max_attempts` is spent, honestly attested, no silent drop either way. **Overlap** is fixed at skip for v1: a due
tick while the schedule's previous fire is still live stamps `last_outcome: "missed (previous run still live)"` and
is never queued. `POST .../run-now` shares this same firing path, so a fire an owner clicks and one the ticker takes
leave identical session state and tick-log entries behind them.

## What this station does not do (v1)

Agent-authored schedules, catch-up replay of ticks missed while the process was down, retention policy narrower than
the one global default plus a per-run pin, and editing a historical run's graph in place (resume-with-edits already
exists for a *paused* run, history itself is read-only).
