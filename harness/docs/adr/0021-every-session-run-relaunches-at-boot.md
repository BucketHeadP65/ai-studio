---
id: harness-adr-0021-every-session-run-relaunches-at-boot
type: adr
title: Every session's run relaunches at boot
summary: At boot the harness relaunches the run log of every pooled session in every registered project, not only the process's own session's, and DBOS is not adopted.
status: accepted
aliases:
- harness-adr-0021-every-session-run-relaunches-at-boot
app: harness
created: '2026-09-30'
---

Amended by [0022](0022-chat-runs-wait-and-rejections-drop-what-they-rerun.md): a chat session's run waits for the person, and an interrupted rejection rerun runs again.

## Context

The in-process engine writes every finished step of a run to a run log. At boot only the process's own session's run log was relaunched ([0015](0015-in-process-by-default-temporal-opt-in.md)). A pooled session, such as a chat session or a scheduled run's runner session, kept its run log on disk and nothing relaunched it.

A kill test of the engine inside a DBOS workflow showed that DBOS matches a recorded result to a step by the order the steps are called. Two parallel branches of more than one step start their steps in the order earlier steps finish, so a recovered run could take the other branch's result.

## Decision

At boot, after the process's own session, the harness relaunches the run log of every pooled session in every registered project's sessions folder. A run log whose session no longer opens is deleted. Each folder's orphaned reviewed values are swept once before any run in it is relaunched. DBOS is not adopted.

## Consequences

A run in any session survives the process dying. A step already in the run log does not run again. The step that was running when the process died runs again from its start, and a map that had not finished runs all its items again. A rerun that a person's rejection started does not run again: the run returns to the gate with the value that was refused and still owes the repair, as a resumed run does. A relaunched run takes each node's output by the node's id, so parallel branches never take each other's results. A relaunched run keeps the gate mode, gate timeout and runner it was launched with. When the process's own session has taken a pooled session's id, that session's run log moves to the process's own session and relaunches there. A pause leaves the run log as a process death does, so a restart relaunches a paused run too.

## Options considered

The engine's graph inside a DBOS workflow, with each step a DBOS step. Lost, because a recovered run with two parallel branches of two steps each took the other branch's result in 5 of 8 tries, or failed in 6 of 8 when the step names carried the node id.

A run loop of our own that starts steps in a fixed order, inside DBOS. Not taken now. It is a decision about the engine and would be made on its own.

Temporal as the default. Lost for the reason 0015 gives: no deployment wants a server by default.
