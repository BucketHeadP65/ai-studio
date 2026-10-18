---
name: delegating-work
description: >-
  Hand self-contained work to a worker on the right model tier: when to delegate at all, which of quick, standard, deep
  fits the task, how to brief so the worker needs nothing from this conversation, when a script beats one delegation at a
  time, and what to do when a result comes back thin. Read it before the first delegate_task or run_agent_script of a turn.
---

# Delegating work

You have one worker and three tiers. The worker is a coding agent with the shell, the checkout's
files, and the knowledge bases' reads. It runs in a fresh context: it sees nothing of this
conversation, and the person never sees its output. Everything it knows comes from your brief,
and everything the person learns comes from your relay.

## Whether to delegate at all

Delegate work that is self-contained, checkable, and either heavy on reading or parallel: a
multi-file change with a test to run, a review of a diff against a stated standard, a search
across a knowledge base with a question to answer, three independent edits at once.

Do not delegate:

- a one-call lookup you can make yourself, the round trip costs more than the call
- an act the person must own: a gate decision, a save, a shell command that changes their machine
- anything that needs this conversation's context, because the worker has none of it

## Which tier

| Tier | Use it for |
|---|---|
| `quick` | mechanical edits, lookups, summaries, running a check, single-file work |
| `standard` | ordinary implementation, review with a clear brief, debugging with a repro |
| `deep` | design judgment, multi-file changes, hard debugging, the final review before you say done |

`standard` is the default when you pass no `model`. Pass one anyway: you know how hard the task is
at the moment you write the brief, and nobody else does.

Escalate, never repeat. A result that is thin, wrong, or stopped short goes up one tier with the
previous attempt attached to the brief, so the next worker starts from what the last one found.
The same brief on the same tier returns the same answer.

## How to brief

A brief is the whole instruction set, not a summary of one:

- the task, as one paragraph a stranger could act on
- the file paths involved, and the constraints that hold (style, what must not change)
- the acceptance check: the command to run and what passing looks like
- what to report back: paths touched, the check's output, anything unexpected

When fanning the same task over several targets, send the same brief verbatim with only the
target changed. Never send two workers the same file at the same time.

## One at a time, or a script

`delegate_task` is one task, one worker, the result back in your context. It is always on your
belt, nothing to load. Use it when each result needs your judgment before the next step, or when
there is one task. The workers are these tiers and nothing else: there is no external agent to
configure or look for.

`run_agent_script` is a script you write that calls `quick`, `standard`, and `deep` as async
functions. The tool is loaded on demand: call `load_capability` with `agent-scripts` first, and it
appears on your next step. Use it when the choreography is the work: fan out over a list, chain one
result into the next brief, vote three answers, retry until a check passes. Intermediate results
stay inside the script and only its final value comes back, so a script is also how you keep a
large fan-out out of your context. Catch a failing worker inside the script (`RuntimeError`) and
decide there whether to retry on a higher tier.

## Reporting

Relay what the worker found and did, in your own words, with the paths and the check output it
reported. Say which tier ran. The tool result carries only the worker's report, never its cost, so
do not invent a number. A worker's refusal or failing check is a fact to report, not a reason to
try the same thing again without saying so.
