---
id: harness-session-journal
aliases: ["harness-session-journal"]
type: reference
app: harness
status: current
tags: [session-log, events, session, observability]
title: "session log, the harness session on an event-sourced log"
---

# session log, the harness session on an event-sourced log

One append-only JSONL file, `.state/session-log.jsonl`, owned by the server process: every act that changes what the model
sees on the next turn leaves one line (`{"seq": …, "at": "…", "kind": "…", …}`) and lines are never edited or deleted. The
invariant is **model-visible means logged**. The current state is derived by reading forward, never stored as the one truth.
This page is the contract.

## The grammar (`harness.domain.events`)

Typed, frozen, closed models under one `kind` discriminator: `boot` (what the process resumed), the turn lifecycle
(`turn_started` / `turn_settled` with the transcript run id it wrote / `turn_failed` / `turn_abandoned` for a start no
terminal event ever answered), `conversation_forgotten` (the boundary: forgetting is a watermark, never an erasure),
`draft_settled`, the setup moves (`plugin_set`, `profile_selected`, `model_selected`), `run_launched` (the session's own
act, naming the run it minted), the session lifecycle (`session_started`, `session_renamed`, `session_archived`,
`session_deleted`), `friction_flagged` (the agent's own report that something ground), and the external-agent lifecycle
(`external_agent_started` / `external_agent_update` / `external_agent_finished`). Events carry references,
never content: message text stays in the transcript, evidence and activations live in their own files. What became of a
run is not here either: the run's own log answers that (`features/running/infrastructure/run_events.py`, one file per run under
`.state/runs/<run_id>/events.jsonl`, with the events defined in `core.ai.workflows`), and the session log keeps only the
launch. The session log is the timeline that interleaves what is left, and the authority for exactly one thing, the
conversation lifecycle. Every `turn_started` carries the `session_id` it opened under and its `prompt_excerpt` (`null`
for an empty prompt). Every `turn_settled` and `turn_failed` carries the `started_seq` of the `turn_started` record it
closes, so a turn whose start the disk refused writes no end either. Every `session_started` carries its `session_kind`.
`conversation_forgotten` names the session forgotten, or `null` for a Forget pressed while the conversation held no
session id, which closes the session the newest `session_started` opened. `turn_settled` also carries `pending`, the tool
names a turn stopped for, empty when the turn answered outright. A line missing a field its event requires does not
decode. See [sessions.md](sessions.md) for the pool that stamps them and the API built over the result.

The codec and the projections are pure: `decode` answers `None` for a torn tail or a kind this reader does not know (one lost
line never costs a boot). `open_turns`, `last_forgotten_at` and `session_spans` are the questions the runtime asks.
Each project workspace carries its own session-log file under `<root>/.aifactory/state/`. The harness checkout keeps this
page's `.state/session-log.jsonl` as its own, separate home.

## The boot sweep (`harness.adapters.session_log.boot_sweep`)

Runs in the lifespan before resume: every turn a crash left open is closed as explicitly `turn_abandoned`. Resume then honours `last_forgotten_at` as its
watermark, and the `Boot` event records what it resumed.

## Process boundary

CLI acts (`harness activate`, `plugins`, `profile` from a shell) happen in another process and stay in their own activation
record. They do not write log entries. The session log is what this server process saw and did. A session log write
failure warns and never breaks the turn it was narrating.

## The owner's window

```
harness sessions list           # conversation spans: when, how it ended, turns held
harness sessions tail --n 20    # the newest events, seq and stamp first
harness sessions search TERM    # case-insensitive scan over the session log and this checkout's state home
harness sessions import         # copy the pre-record transcripts into the record, once
```

`search` is a scan, not an index. One owner's volume does not need one, and the knowledge base stays the semantic layer
above. Cite events by `seq` in postmortems. The ACP plugin's turns are not yet logged (pydantic-ai-harness's ACP session owns that
loop), while its message content still lands in the transcript like every other run.

`import` prints one line per directory:

```
<directory>: runs=N events=N snapshots=N effects=N skipped=N resumed=N unnamed=N reconciled=N malformed=N
```

or `<directory>: nothing there` when the path is not a directory. `skipped` counts runs the record already holds a
snapshot for, left untouched. `resumed` counts runs the record holds with no snapshot yet, because an earlier pass
stopped partway, finished by this one. `unnamed` counts runs carrying no conversation id, which the record cannot
hold. `reconciled` counts runs whose snapshot named their own conversation under a different spelling, corrected to
the run's own. `malformed` counts runs whose conversation id does not parse, or whose snapshot names a different
one, left behind untouched.
