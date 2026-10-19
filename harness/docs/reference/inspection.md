---
id: harness-reference-inspection
type: reference
title: 'inspection: the dispatch, the three lanes, the sweep, the CLI and the page'
summary: Inspection runs independently of scheduling, reconciles final transcripts, exposes complete recorded evidence, preserves uncertain verdicts and reports diagnostic experiments with explicit evidence limits.
status: current
aliases:
- harness-reference-inspection
- inspection
app: harness
tags:
- inspection
- coordination
- cqrs
- watches
- cli
- questions
- routes
created: '2026-09-12'
---

## Overview

Inspection reads recorded conversations without changing them. Rules produce leads. The inspector either proposes a finding, explicitly dismisses a false-positive lead, or records why it cannot decide. A finding names a symptom, evidence spans and causal hypotheses. A compatible taxonomy label does not establish that a proposed repair works.

The record, leads, findings and inspector conversations remain separate. Every model question is recorded under the inspector agent name and excluded from automatic inspection. The owner can inspect the evidence and continue the inspector's conversation.

## The three lanes

The durable jobs remain in `coordination.jobs`, with one live key per work unit:

| Kind | Key | Policy |
|---|---|---|
| `inspect_run` | `<watch>:<source>` | Three attempts, 600-second lease, exponential backoff from 30 to 600 seconds. |
| `attribute_lead` | Lead id | Two attempts, 600-second lease, backoff from 60 to 600 seconds. |
| `scan_run` | `scan:<watch>:<source>` | Two attempts, 600-second lease, backoff from 60 to 600 seconds. |

Five independent background loops serve scheduling and dispatch, reconciliation, rules, attribution, and scans. A slow model or a large reconciliation window cannot hold the scheduling loop. The timer loop retains the notification wake. Other loops poll independently so they do not compete for one notification queue. Shutdown cancels and awaits every loop and its pending waits.

`InspectRun` and `ScanRun` carry an optional positive `terminal_event` identifying the completed transcript they must process. Existing persisted payloads without this field still load. A successful earlier inspection is not proof that the final transcript was inspected. A final scan coalesced behind an earlier scan remains retryable.

`every_run` scans text-only runs too. `when_a_rule_led` scans runs with leads and a deterministic sample of runs without leads. The selection hashes the watch and source, so retries do not resample. `never` runs only the declared rules. A failed job retries under its policy and becomes visibly abandoned after its attempts run out.

## The dispatch and the sweep

Dispatch advances `inspection.dispatch` through the record's events: `model_request_completed`, `run_completed`, and `run_failed`. It matches each watch's agent/session selection, enqueues work, and then advances its cursor. A fresh consumer starts at the current position. Reads, cursor writes and job enqueue are not one transaction, so delivery is at least once and handlers must remain idempotent.

Event positions are assigned before commit. Reconciliation therefore covers live watches as well as explicit sweep watches. Each pass freezes an upper event position and pages through terminal events within the watch's time window. The next pass starts its window scan again, so a late commit behind an earlier cursor is still eligible. Keyset pagination prevents a 1,000-row read limit from starving later runs.

A run is current only when its successful final rules command matches its terminal marker and any matching final scan is complete. Pending, failed or abandoned final scans are not mistaken for completed inspection. An explicitly undecided scan is a completed attempt with an unresolved verdict, not a clean run, and is not resubmitted forever.

The separate reconciliation loop uses `sweep_seconds`. Live watches use `reconcile_hours`. Explicit sweep watches retain `sweep_hours`. These windows bound automatic recovery. The CLI can request a larger historical window.

## Settings

`InspectionSettings`, in `harness/config/harness_settings.json`:

| Field | Default | Meaning |
|---|---|---|
| `watches` | One `every-run` watch | Which runs, rules, scan policy and timing apply. |
| `world_changing` | `[]` | Tool names whose effects invalidate duplicate-read assumptions. |
| `result_cap_chars` | `null` | The overflow rule is inactive until a result-size threshold is supplied. |
| `inspector_tiers` | `["helper", "worker"]` | Attribution and scans ask these tiers in order. |
| `dispatch_seconds` | `15` | Dispatch interval. |
| `sweep_seconds` | `3600` | Reconciliation interval, independent of scheduling. |
| `job_poll_seconds` | `1` | Independent job-lane polling interval. |
| `batch` | `200` | Events read per dispatch pass. |

A watch's `clean_run_sample_rate` defaults to `0.1`, within 0 through 1, and controls sampling when `inspector` is `when_a_rule_led`. `every_run` ignores this sampling rate. `reconcile_hours` defaults to 24 for live watches. `sweep_hours` is required exactly when `when` is `sweep`.

The shipped watch selects every agent except the inspector itself, triggers at run end, and runs six rules: schema rejection, unchanged retry after error, repeated denied action, duplicate call, result overflow and unfinished budget exhaustion. A watch without rules must use `every_run`.

Runtime inspector calls retain the framework's request limit. The evaluation command uses separate explicit limits of 12 requests and 120 seconds per trial.

## What the inspector reads

The symptom's horizon chooses the initial view: `moment`, `run`, or `conversation`. Initial evidence and subsequent tools use bounded pages. Recorded content is evidence, never new instructions for the inspector.

| Tool | Read and continuation |
|---|---|
| `outline(run=None, offset=0)` | One line per step, in pages of at most 20,000 characters. |
| `read(step, start=0, end=None, run=None, resource="messages", offset=0)` | The selected message range, or recorded `instructions` or `tools`, in 2,000-character pages. |
| `find(text, run=None, offset=0)` | Up to 50 bounded matches across messages, instructions and tool metadata, with exact follow-up reads centered on the match. |
| `earlier_runs(offset=0)` | Earlier runs from the same conversation, newest first, in bounded pages. |

Every truncation names the next call and offset. The instructions resource includes `ModelRequest.instructions` as well as system-prompt parts. The tools resource includes recorded function and output tool definitions, JSON schemas and other recorded request output/native-tool metadata. Missing recordings are marked unknown rather than interpreted as missing instructions or capabilities.

Part citations use `step.message.part`. Tool schemas and instruction resources are read as recorded metadata rather than invented message positions. A requested other run must belong to the same conversation. Reading does not run a counterfactual experiment or test a stronger model.

## Questions

Each attribution or scan gets a question row linked to its own recorded conversation. The composition attaches `StepPersistence` and `RequestParametersRecorder`. Each tier has its own question, and retries stay in that question's conversation. A follow-up continues that conversation on the tier's current model.

Output validation happens inside the agent run. Unknown taxonomy names, incompatible causes, invalid spans and schema-invalid answers become recorded retries. Exhausted invalid output remains `invalid`. Provider/network failures remain job failures that can retry. A skipped output tool or an output without the framework's processing confirmation is not presented as an accepted diagnosis.

Lead outcomes are explicit:

| Resolution | Meaning | Settled? |
|---|---|---|
| `open` | No attribution outcome yet. | No |
| `undecided` | Evidence is insufficient to decide. | No |
| `invalid` | The inspector could not produce a valid answer. | No |
| `dismissed` | The inspector explicitly found the lead justified or false positive. | Yes |
| Finding exists | A symptom and causal hypotheses were recorded. | Yes, pending verification of the repair |

Existing attempted leads migrate to `undecided`, never to dismissal. Finalization locks the same lead row so concurrent answers cannot replace an earlier finding or dismissal. Uncertain and invalid answers do not reopen a settled lead. `NoIssue` is the explicit negative attribution output. `NothingSeen` is a supported clean scan, while `ScanUndecided` means the scan cannot assess what happened.

The UI distinguishes these outcomes in the run table, counts, timeline and trace. A job waiting to retry is not called finished.

## Reading it back

Eight routes, served by `InspectionController` (`harness/src/harness/features/inspection/controller.py`):

| Route | Answers |
|---|---|
| `GET /api/inspection/findings?symptom=&source=&found_by=&limit=` | Findings newest first. Each: its id, run, symptom, detector, model, when it was decided, its blames, its `explanation` (the inspector's own plain-sentence explanation, or the blames' `why` sentences with their `step.message.part` coordinates stripped when the detector wrote none), the detector of the lead under it, and the question that decided it. |
| `GET /api/inspection/findings/{finding_id}` | One finding, the lead under it, and every span's evidence: `step.message.part`, the part's label, its kind, and its text rendered for a person. |
| `GET /api/inspection/findings/{finding_id}/turn` | The turn under investigation: for each step the finding cites, its messages in the same five fields a session's transcript crosses the wire in, and which of them hold a cited part. |
| `GET /api/inspection/questions/{question_id}` | The question row and its recorded conversation as messages, in the same five fields a session's transcript crosses the wire in. |
| `GET /api/inspection/runs/{run_id}` | Everything the inspection did to one run: its leads, findings, questions, and the commands both run lanes and the lanes of the newest 50 leads issued. |
| `GET /api/inspection/runs/{run_id}/outline` | The run in one screen: the record's facts about it (agent, when it started and ended, how it ended, the model of its last request, how many tool calls), and one entry per step with how many messages the step added and what happened there in one line. 404 in words when the run cannot be read. |
| `GET /api/inspection/runs/{run_id}/steps/{step}` | The messages the run added at one step, in the same five fields the turn route answers, none cited. 404 in words when the run cannot be read or has no such step. |
| `POST /api/inspection/questions/{question_id}/ask` | One more question to the inspector that answered, on the question's own tier, with the same tools over the same run, recorded under the same conversation. Body `{"text": "..."}`, answer `{"answer": "...", "conversation_id": "...", "model": "..."}`, the model the tier actually ran on now, which may differ from the question's own `model` when the owner has since changed the tier. |

The SPA draws a findings list at `/inspection`, one finding with evidence and the inspector's trace at `/inspection/{id}`, and the run overview at `/inspection/runs/{run}`. The list filters the newest 200 findings. The overview shows leads, findings, questions and jobs, distinguishing dismissed, uncertain and invalid outcomes. The ask route returns one JSON body. The trace shows recorded validation retries and distinguishes processed, skipped and unvalidated structured outputs.

## Operating it

`uv run harness inspect --run <id>` issues `InspectRun` under every watch that selects that run, whatever its `when`,
and prints one line per watch: `queued  inspect_run <job id>  watch <name>` or `already live  inspect_run <job id>
watch <name>`. `uv run harness inspect --since <hours>` runs the sweep's window for every watch over the last `<hours>` and
prints, per watch, `watch <name>: <considered> run(s) in the last <hours>h, <enqueued> queued`. Both commands only
enqueue. The running harness's worker serves the jobs.

SQL against the sessions database:

```sql
-- jobs by kind and state
SELECT kind, state, count(*) FROM coordination.jobs GROUP BY 1, 2 ORDER BY 1, 2;

-- the dispatch's own position
SELECT consumer, position FROM inspection.cursors;

-- how much an inspection has found
SELECT count(*) FROM inspection.leads;
SELECT count(*) FROM inspection.findings;

-- an InspectRun's own report, most recent first
SELECT result FROM coordination.jobs WHERE kind = 'inspect_run' ORDER BY id DESC LIMIT 5;

-- what a scan reported, most recent first
SELECT result FROM coordination.jobs WHERE kind = 'scan_run' ORDER BY id DESC LIMIT 5;

-- who found what
SELECT found_by, count(*) FROM inspection.findings GROUP BY 1 ORDER BY 1;

-- why a job gave up
SELECT id, key, attempts, max_attempts, last_error, result
  FROM coordination.jobs WHERE kind = 'inspect_run' AND state = 'abandoned' ORDER BY id;

-- what the inspector was asked, and how each one ended
SELECT kind, tier, model, answer FROM inspection.questions ORDER BY question_id DESC LIMIT 10;
```

A filed flag's `source` field can also read `retro`: a value a reader still accepts for lines the retired retro pass
wrote before this slice, so an old session-log line still decodes. `live` is the only value written today, by
`flag_friction` (`harness/src/harness/features/sessions/flags.py`).

## Tables

**`inspection.leads`**

| Column | Type | Notes |
|---|---|---|
| `lead_id` | bigserial, PK | |
| `source` | text | The transcript, `<scheme>:<locator>`. |
| `watch` | text | Which watch's `InspectRun` found it. |
| `symptom` | text | A symptom name from the taxonomy. |
| `spans` | jsonb | The span list. |
| `spans_digest` | text | With `source` and `symptom`, the uniqueness key: a lead is stored once. |
| `found_by` | text | The detector's name: a rule key, or `inspector`. |
| `seen_at` | timestamptz | Defaults to `now()`. |
| `attempted_at` | timestamptz, nullable | Stamped once the inspector's lane has tried every tier. |
| `refusal` | text, nullable | The reason for a dismissal or an unresolved attribution. |
| `resolution` | text, not null | `open`, `undecided`, `invalid`, or `dismissed`. Defaults to `open`. |

**`inspection.findings`**

| Column | Type | Notes |
|---|---|---|
| `finding_id` | bigserial, PK | |
| `lead_id` | bigint, unique, FK `leads` (cascade), nullable | Null for a model-only symptom found with no lead. |
| `source` | text | |
| `symptom` | text | |
| `spans` | jsonb | |
| `blames` | jsonb | `[{root_cause, surface_kind, surface_name, why}, ...]`. |
| `found_by` | text | `rule` or `inspector`. |
| `model` | text, nullable | The inspector's model name, null for a rule. |
| `question_id` | bigint, nullable, FK `questions` (set null) | The question that decided it. Null for a finding a rule decided. |
| `decided_at` | timestamptz | Defaults to `now()`. |
| `explanation` | text | What went wrong and what to fix, in plain sentences, when the detector wrote one. Empty when it did not, and the finding route then falls back to the blames' `why` sentences with their coordinates stripped. |

A finding and a dismissal compete through the same locked lead row. The first terminal decision stands, and a duplicate finding returns its existing id. A late answer cannot silently change a dismissed lead into a finding.

**`inspection.questions`**

| Column | Type | Notes |
|---|---|---|
| `question_id` | bigserial, PK | |
| `conversation_id` | uuid, unique | The conversation the question's run is recorded in. |
| `kind` | text | `attribute` or `scan`. |
| `source` | text | The run the question is about, `<scheme>:<locator>`. |
| `watch` | text | The watch the question was asked under. |
| `lead_id` | bigint, nullable, FK `leads` (set null) | The lead being attributed. Null for a scan. |
| `tier` | text | The tier the question was asked on, which a follow-up rebuilds its model from. |
| `model` | text | The model that answered, as the finding also records it. |
| `asked_at` | timestamptz | Defaults to `now()`. |
| `answer` | text, nullable | The one line the handler ended that question with. |

**`inspection.cursors`**

| Column | Type | Notes |
|---|---|---|
| `consumer` | text, PK | For example `inspection.dispatch`. |
| `position` | bigint | The last `run_events.id` this consumer has handled. |

**`coordination.jobs`**

| Column | Type | Notes |
|---|---|---|
| `id` | bigint, identity, PK | |
| `kind` | text | `inspect_run`, `attribute_lead` or `scan_run`, among any other kind the worker serves. |
| `key` | text | The live key, unique among `queued`/`running`/`failed` rows of the same kind. |
| `payload` | jsonb | The command, `InspectRun`, `AttributeLead` or `ScanRun`. |
| `state` | text | `queued`, `running`, `failed`, `done`, or `abandoned`. |
| `attempts` | integer | |
| `max_attempts` | integer | |
| `available_at` | timestamptz | When a `queued` or `failed` row becomes claimable. |
| `locked_at`, `locked_by` | timestamptz, text | Who is running it, and since when. |
| `lease_seconds` | integer | How long a lock survives with no heartbeat. |
| `last_error` | text, nullable | |
| `result` | text, nullable | The handler's one-line report. |
| `created_at`, `started_at`, `finished_at` | timestamptz | |

## Evaluation and learning

Run from `harness/`:

```bash
uv run harness evals inspection --repetitions 2
uv run harness evals inspection --dataset evals/inspection/evidence-access.json --output /tmp/inspection-evaluations --tier helper --repetitions 3
```

The command freezes one configured model for both variants, validates every nested case before spending tokens, and compares complete recorded evidence against an ablation hiding request instructions and tool definitions. The shipped versioned dataset has two development cases and nine synthetic evaluation cases. Labels are explicitly agent-reviewed, not owner-reviewed. Source names are replaced with opaque identifiers before the model can read them.

Each evaluation case runs independently at least twice per variant. The grader sees the task criterion and prediction, without variant identity, and checks verdict, symptom and supported causes. Acceptance requires actual diagnostic task-success gain with no per-case regression or failed trial. Symptom disappearance alone never earns acceptance. The diagnostic reports reuse those exact paired trials, making no extra model calls.

Each run writes `comparison.json`, `baseline.json`, `candidate.json`, `summary.json` and its inspector traces into a fresh directory under the selected output root. The default is `.state/inspection-evaluations`. Reports fingerprint the dataset, code, taxonomy, prompts, libraries, limits and conditions. Configured model names and observed provider identities are recorded separately. An unreported provider revision stays unresolved, and observed identity drift fails comparability. A timed-out provider call leaves total cost unknown.

The `learning/` directory accumulates validated records about a concrete problem and repair surface. Evidence roles come from comparable paired outcomes. Failed or missing-grade cases contribute no supporting/contradicting stance, while their complete rejected attempt remains. Positive, negative and successful-alternative evidence has run-specific ids. Atomic locked writes merge history, reject changed historical ids, and retain accepted and rejected proposals. No score automatically changes a skill or activates code.

The public contracts live in `core.ai.inspection.contracts`. Other task runners and independent graders can use the same paired comparison contract. This inspector ablation measures diagnostic correctness on a small synthetic set. It does not establish downstream agent improvement or agreement with human labels.

## Limits of the shipped demonstration

The 2026-09-13 result of 11/18 versus 18/18 came from cases authored and seen by the implementer. The two `train` rows were excluded from scoring, but no training or optimization run used them. The nine `test` rows were not independently withheld. Eight supplied the suspected symptom as a lead, and the remaining case was a clean scan, so this is mostly an attribution check, not a failure-discovery benchmark.

Both variants ran the same updated inspector. The baseline removed request instructions and tool definitions. The previous attribution implementation already supplied instructions and tool names/descriptions, so the stripped baseline does not represent the previous release. The expected verdict and cause fields were not passed to execution, and the saved traces contain no answer-bearing case ids. Nevertheless, author exposure, constructed cases and the stronger artificial deprivation prevent interpreting the numbers as before/after release accuracy or generalization.

The compact result retains the measurements with a `methodology_audit`. An independent evaluation requires fresh scenario families and labels prepared after the implementations are frozen, with the actual previous and candidate implementations compared on equivalent allowed inputs. Repetitions of the same nine cases do not increase the number of independent cases.
