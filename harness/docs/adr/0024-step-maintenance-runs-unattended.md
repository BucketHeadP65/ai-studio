---
id: harness-adr-0024-step-maintenance-runs-unattended
type: adr
title: Step maintenance runs unattended, with an agent judge as its only gate, adoptions on a live trial that reverts on its own, and wiring edits only
summary: The step-maintenance workflow runs on an on-step-change schedule under gate mode auto. Its plan passes only an agent judge, each adoption lands as a new consumer version on a live trial whose next real runs are read by rules and revert it on the first failed or worse one, and an adoption may carry only rewire, add_parameter and set_output beside the replaced step.
status: accepted
aliases:
- harness-adr-0024-step-maintenance-runs-unattended
app: harness
created: '2026-11-06'
---

## Context

A workflow keeps the copy of each reusable step it was saved with. A new version of a step reaches a consumer only as
a new consumer version, so a person's edit to a step never changes every consumer at once, untested, and a workflow's
trust, counted by its content, keeps describing what runs. Root [ADR 0023](../../../docs/adr/0023-reusable-step-changes-arm-step-maintenance.md)
fires the `step-maintenance` workflow when a step version is saved. Nobody starts that run, and the owner wants the
consumers kept current without being asked about each one.

So the run decides, lands and checks its own work. Three things had to be settled: who approves a plan when no person
is there, what an adoption may change in a consumer, and how a bad adoption is undone.

## Decision

**An agent judge is the only gate.** `judge_plan` is a judge gate with `max_attempts: 3`, and the maintenance schedule
runs under gate mode `auto`. The judge holds read tools only and reviews the planner's decisions against each
consumer's stated goal and against evidence code made after the plan (the consumer list, the contract checks and the
case reports). It marks each change pass or fail, so one weak change costs at most one round and never the rest of the
plan. Under `auto` a judge's cannot judge is a rejection carrying its note (the amendment to
[0008](0008-orchestrator-gates-and-run-gate-modes.md)), so no plan lands that no one judged. The third rejection ends
the run with nothing landed and raises one `plan_rejected` maintenance flag per changed step, which a later landing for
the step resolves. There is no person gate.

**The landing writes only what the judge approved, and code copies every step.** A decision names its consumer and its
step version by id, and the landing copies the step from the library, so a model never retypes a step. Every adoption
into one consumer lands as one version, numbered one above the highest saved version of its name, saved by
`agent:step-maintenance`, its lineage touching the nodes of every replaced step. A kept copy writes a decline into the
consumer's record, and a flagged pair writes a `consumer_flag` page. Every result that waits for the owner is a
maintenance flag page with a stable id, never only a line on a run's record.

**An adoption may carry only wiring edits.** Beside the replaced step an adoption may carry `rewire`, `add_parameter`
and `set_output`, the edits a contract check can need. Any other edit is refused at landing and by `check_change`, so
a node, a gate, an orchestrator's charge or an inline step a model wrote never lands in a consumer, and a person's gate
is never removed by a model.

**Each adoption is on a live trial, and the first failed or worse run reverts it.** The adopted version carries a
`live_trial` marker naming every step version it tries and the version before the first of them, whose newest three
completed runs are the baseline. Schedules fire the newest live version, so its next real runs are the live trial. Each
run is read once by `live_trial_verdict`, from what the run and its grades recorded, with no model asked:

- ignored when a person stopped it, when it failed in execution anywhere but at a node running an adopted step, or
  when it completed with its checks failed and the previous version has no completed run to rank it against,
- failed when it failed in execution and a touched node did not produce while every node it reads did,
- worse when, against the baseline, its checked status ranks below every baseline run's (a completed run checked
  failed ranks lowest), a gate both versions hold was rejected more times, or a structural grade fell more than 0.1
  below the baseline's mean,
- counted otherwise. Three counted runs close the live trial and nothing is written.

The first failed or worse run reverts the version while it is still the newest live one: it is archived, the version
the marker names as previous is live again with its own trust record and every decline the retired versions recorded
for other steps (a revert undoes the adoptions, not the keeps), that version's record declines every step version on
the marker as reverted, its diverged copy flags are raised again, one `live_trial_reverted` flag per step is raised,
and the owner is told. An adoption into a version still on live trial builds on it, keeps its baseline and carries its
steps into the new marker, and its run count starts again. A version a person saved past is never reverted.

## Consequences

The owner hears of every landing in one notice, of a refused landing before the run fails, and of every revert. What
needs a decision waits as a flag under Home's "Needs your decision" and in `harness flags`.

A revert declines every step version on the marker for that consumer, so a good step version adopted together with a
bad one is declined until a newer version of it arrives. A completed run whose own check failed for a reason outside
the adopted steps can revert a good adoption, because a checked failure ranks against the baseline. A run's gate
rejections are counted from its recorded gate decisions, a judge's included, only since 2026-11-05, so a baseline run
from before then can carry too few rejections and make an honest live trial run read as worse.

Each run spends a planner turn and a judge turn even when no consumer needs a change, because the engine has no way to
skip a node or answer a gate without its model. The workflow runs on the in-process engine, because on the durable
engine a tool-less program never finishes (technical debt TD-84), and the judge reads evidence made again after each
plan (TD-83).

## Options considered

A person gate on every plan. Lost, because the run is fired by a schedule and the owner asked not to be asked about
each consumer. A person still decides every flag.

Land the plan and let the inspector's leads decide a revert. Lost, because a diagnosis is a hypothesis until it is
independently evaluated. The leads stay on the run's page and never revert anything.

Cost as a sign of worse. Lost, because a run's spend follows the size of its input.

Revert by saving the old copy as yet another version. Lost, because that resets a trusted version to unverified, while
making the previous version live again keeps its trust record.

Let an adoption carry any edit the planner drafts. Lost, because a model-written gate, charge or node would then land
in a consumer with no person reading it.

One live trial per step, each adoption its own version. Lost, because two steps adopted into one consumer, in one run
or across runs, left the earlier adoption superseded before any run tried it.
