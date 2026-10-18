---
id: harness-adr-0022-chat-runs-wait-and-rejections-drop-what-they-rerun
type: adr
title: An interrupted rejection rerun runs again after a restart, and chat runs wait for the person
summary: A restart relaunches only runner-session and scheduled runs by itself, a chat session's run waits for the person, and a rerun a rejection started no longer shows the refused value after a restart.
status: accepted
aliases:
- harness-adr-0022-chat-runs-wait-and-rejections-drop-what-they-rerun
app: harness
created: '2026-09-30'
---

## Context

After [0021](0021-every-session-run-relaunches-at-boot.md), every session's run relaunched at boot, including runs a person was working with in a chat. A restart started them again without the person asking.

A run interrupted during a rejection's rerun also came back to the gate showing the value the person had refused.

## Decision

Every run log records the kind of session the run was launched in. At boot a run log relaunches by itself only when its session kind is "runner" (the Run button in the library) or its runner starts with `app:schedule:` (a schedule). Any other run is paused by the restart. That includes every run in a chat or follow-me session, the process's own session among them, and every run log written before this change unless its runner is a schedule's.

A run paused by the restart runs nothing. The session keeps it, and the person resumes or stops it. Resuming uses the logged workflow unless the person sends an edited graph, which is validated. It keeps the logged gate mode, gate timeout and runner. Stopping ends the run from its run log with its evidence and archive record. A run a person paused resumes with the session's live gate mode and the logged timeout and runner. A session holding a paused run refuses a new launch until the person resumes or stops it. An agent turn is never resumed.

When a gate rejects (a person, a judge or an orchestrator), the run does not remove values from itself. It marks the value keys of the node the gate sends the run back to and of every node downstream of it, and of a multi-turn map's `<map>.<turn>` keys, after the attempt ceiling check. A write of a key clears its mark. The run log leaves marked keys out of what it saves. An exhausted run keeps its last outputs.

## Consequences

After a restart, a chat session's run shows as paused with its start time and spend, and it runs only when the person resumes it. Until then the session refuses a new launch, so nothing overwrites the paused run. Runner-session and scheduled runs relaunch by themselves as before. A run log from before this change is paused by the restart unless its runner is a schedule's.

A run relaunched or resumed while a rejection's rerun was in flight runs the marked nodes again and owes the repair. The person is not asked to review the value they refused.

Four consequences of 0021 no longer hold. A restart no longer relaunches every session's run, and an interrupted rejection rerun no longer returns to the gate with the refused value. An adopted chat log is paused in the process's own session, not relaunched, and a person-paused chat run stays paused after a restart. The rest of 0021 stands.

## Options considered

Relaunch everything, as 0021 does. Lost, because a run in a chat should wait for its person.

Keep the refused value in the run log. Lost, because it asks the person to refuse the same value twice.
