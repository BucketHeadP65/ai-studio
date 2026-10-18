---
id: ref-harness-spend
aliases: ["ref-harness-spend"]
type: reference
app: harness
status: current
tags: [spend, telemetry, cost]
title: "Spend"
---

# Spend

What a run, a session, a step, a model or a day has cost is answered from Logfire, the telemetry this app already
ships every model call to. `spend_report` (the agent's own plugin) and `GET /api/spend` (AI Factory Studio's route) both
ask one of six named questions and answer with a total, then one line per key, summed from what each call was
priced at when pydantic-ai made the request.

## What a report is

A report is one SQL query against Logfire's `records` table. The query groups by the scope's own key and sums
`operation.cost` and the four token counts (input, output, cache read, cache write) over every span in the window
that carries `operation.cost`. `operation.cost` is what pydantic-ai priced the call at when it made the request,
through genai-prices' best effort price, written once and never repriced afterward. Nothing is stored here and
nothing is cached, so a reread after a new call always answers with what the telemetry holds right now, but a
price correction upstream reaches the spend record's next read and never a report already summed, which is why the two
can differ by design. A call genai-prices could not price carries no `operation.cost` and so appears in no scope
at all, unlike the spend record, which reads such a call as unknown rather than zero.

`sql_for` (`features/running/spend_report.py`) builds the one query a scope asks. The query for `run`, narrowed to
one particular run, is exactly what Logfire received.

```sql
with calls as (
  select start_timestamp, day, trace_id, attributes from records
  where start_timestamp >= now() - interval '24 hours' and attributes ? 'operation.cost' and attributes->>'run_id' = '20260827T233828150806-bafef50c'
)
select attributes->>'run_id' as key, count(*) as calls,
  coalesce(sum((attributes->>'operation.cost')::float), 0) as cost_usd,
  coalesce(sum((attributes->>'gen_ai.usage.input_tokens')::bigint), 0) as input_tokens,
  coalesce(sum((attributes->>'gen_ai.usage.output_tokens')::bigint), 0) as output_tokens,
  coalesce(sum((attributes->>'gen_ai.usage.cache_read.input_tokens')::bigint), 0) as cache_read_tokens,
  coalesce(sum((attributes->>'gen_ai.usage.cache_creation.input_tokens')::bigint), 0) as cache_write_tokens
from calls group by 1 order by cost_usd desc
```

A scope this module does not offer is a change to `Scope` in that file, never a query a caller composes itself.

## The six scopes

| scope | what it answers | what `subject` is | what a line is |
|---|---|---|---|
| `run` | what one run cost | a run id | one line, the run's own total |
| `workflow` | what a workflow's runs cost | a workflow id, such as `wf.digest.v9` | one line per run |
| `session` | what one session cost, its own runs and its ordinary chat turns | a session id | one line per run, plus one line keyed `chat` for turns with no run |
| `step` | what one run cost, step by step | a run id | one line per step |
| `model` | what each provider and model has cost | (none, `model` takes no subject) | one line per provider and model pair |
| `day` | what each day has cost | (none, `day` takes no subject) | one line per day |

`run`, `workflow`, `session` and `step` are about one named thing, so they refuse an unusable `subject` (a scope
that needs one, asked with none, reads as a 400 naming the fix). `model` and `day` ask across everything the window
holds, so `subject` is ignored when one is sent.

## Where the identities come from

`run_id`, `workflow` and `step` are baggage set around every step's model calls, by `composition.step_baggage`, so
every model span a step opens (a `chat` span included) carries all three underneath it.

```python
def step_baggage(active: ActiveRun, step: str) -> AbstractContextManager[None]:
    identity = run_identity(active)
    if identity is None:
        return nullcontext()
    return logfire.set_baggage(run_id=identity.run_id, workflow=identity.workflow_id, step=step)
```

A step running for no run (an ordinary chat turn with no workflow behind it) carries none of the three, which is why
`session` folds those turns under the key `chat` rather than under a run id.

A session is not baggage. It is read off the request span that names it, the one FastAPI opens for a call under
`/api/sessions/{session_id}/...`.

```sql
and trace_id in (select trace_id from records where parent_span_id is null
  and attributes->>'http.target' like '/api/sessions/{subject}/%')
```

## The plugin and the route

`spend_report(scope, subject, since_hours)` (`features/running/tools.py`) is the agent's own plugin. It answers a
total then one line per key, in plain sentences, priced through the same reporter the route uses below. The lines
below are real answers, called straight against this deployment's telemetry with the plugin's own function.

```
Spend for step 20260827T233828150806-bafef50c over the last 720 hours (telemetry) is $0.0000 across 1 calls, 344 in, 39 out, 0 cached.
- draft $0.0000, 1 calls, 344 in, 39 out, 0 cached
```

```
No priced model calls for run run-does-not-exist in the last 24 hours. Widen since_hours, or check the id with recent_runs.
```

```
No scope 'runs', the scopes are day, model, run, session, step, workflow.
```

`GET /api/spend?scope=&subject=&since_hours=` is the same reporter, for the Studio. `since_hours` defaults to
24 and is bounded between 1 and 2160 (90 days). `GET /api/spend?scope=day` answers one line per day the window
covers.

```json
{"available":true,"scope":"day","subject":"","since_hours":24,"lines":[{"key":"2026-08-27","calls":23,"cost_usd":"0.019907049951143563","input_tokens":68546,"output_tokens":6062,"cache_read_tokens":35200,"cache_write_tokens":0}],"total":{"key":"total","calls":23,"cost_usd":"0.019907049951143563","input_tokens":68546,"output_tokens":6062,"cache_read_tokens":35200,"cache_write_tokens":0},"source":"telemetry"}
```

`available` says whether the telemetry answered. True carries the report shown above. A bad scope, or a subject
or window the scope cannot use, is a 400 naming the fix, never a 500. Two real refusals.

```json
{"detail":"No scope 'runs', the scopes are day, model, run, session, step, workflow."}
```

```json
{"detail":"run needs an id of letters, digits, dots, dashes, colons or underscores, up to 80 long, not ''"}
```

## When the telemetry is away

`available` false carries `reason`, what stopped the query, and `fallback`, where a narrower answer still lives.
Five reasons cover every way the query can fail, none of them an exception that escapes the plugin or the route.

| reason | when |
|---|---|
| `Logfire is off: the settings file carries no infrastructure.logfire.read_token, so spend cannot be queried` | no read token is on file, so nothing is sent and the app still starts |
| `telemetry unreachable (ConnectError)` | Logfire could not be reached over the network |
| `telemetry refused the query (HTTP 403)` | the read token on file was refused |
| `telemetry rate limited, ask again in a minute` | Logfire's own per-minute query limit was hit |
| `telemetry answered in a shape this app does not read` | the response did not parse as `QueryResult` |

`fallback` is always the same sentence, both plugins carry it unchanged.

```
The run ledger on disk still answers per run, GET /api/runs/history/{run_id} carries its spend.
```

The spend record it points at is `FileSpendLog` (`features/running/infrastructure/spend_log.py`), one JSONL file per run, priced from
what a step actually produced rather than from the telemetry. It answers one run at a time, never a scope across
runs, which is the whole reason `spend_report` and `GET /api/spend` exist.

## Prices

genai-prices refreshes hourly from boot, through `core.observability.start_price_updates`, which starts the library's own
background updater and keeps whatever it last fetched when the network is away. A model priced after this app's
last snapshot is priced correctly once the first refresh past its release lands.

`price_response` (`adapters/models.py`) prices a DeepSeek response by dropping its dated snapshot before the
lookup, so `deepseek-v4-flash-0731` prices as `deepseek-v4-flash` on whichever route answered, ahead of the
registry's own family-level entry. A GLM response is priced under its wire name as it is, on the route that
answered, and a GLM response the registry does not price on that route is left unpriced. `priced_cost` is what the
spend record prices through on every read, the provider's own billed figure first, `price_response`'s own pricing
second, the plain registry lookup last, `None` only when no source can name a figure at all. A report never prices
through `priced_cost`, it only sums the fixed `operation.cost` pydantic-ai wrote at request time.
