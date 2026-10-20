-- queries.sql — engram eval-run RCA saved SQL.
--
-- The named Logfire Query-API queries the RCA helpers load and run. The loader
-- (`engram.evals.rca.infrastructure.saved_queries.load_named_queries`) splits this file into `{name: sql}`
-- blocks: a `-- name: <key>` comment line opens a block and every line up to the
-- next `-- name:` or EOF is that query's SQL (whitespace stripped). This header,
-- and any blank lines before the first marker, are ignored.
--
-- Dialect & schema: Logfire's DataFusion engine (Postgres-like). Spans/logs live
-- in `records`; JSON attributes are accessed with `->`/`->>` and cast as needed
-- (e.g. `(attributes->>'gen_ai.usage.input_tokens')::bigint`). The eval harness
-- tags every span with `service_name = 'engram-evals'`
-- (`engram.evals.harness.infrastructure.instrumentation`), so each query scopes to that service.
--
-- Markers (discovered from the live schema + engram's instrumentation):
--   * tool-call spans → `attributes->>'gen_ai.operation.name' = 'execute_tool'`
--     (the version-stable tool marker pydantic-ai emits; the tool name is in
--     `attributes->>'gen_ai.tool.name'`). See `engram.evals.harness.domain.evaluators`.
--   * model token usage → `gen_ai.usage.{input_tokens,output_tokens}` attributes
--     on model-request spans.
--   * eval case spans → span-name template `case: {case_name}` with the case name
--     in `attributes->>'case_name'`; the per-case evaluator scores are a JSON
--     object under `attributes->'scores'`, keyed by evaluator name, each value a
--     serialized `EvaluationResult` whose numeric score is its `value` field —
--     so recall@5 is `(attributes->'scores'->'recall@5'->>'value')::float`
--     (pydantic_evals.dataset writes this attribute via `case_span.set_attribute('scores', ...)`).
--
-- Every query carries its own time predicate (`start_timestamp >= now() - interval '7 days'`),
-- the same seven days the RCA path passes to the Query API as `min_timestamp` (`RCA_WINDOW`).
-- Each query was validated to execute
-- against the live `engram` Logfire project (it is empty today, so they return 0
-- rows — but they parse and run without error).

-- name: slowest_tool_spans
-- The retrieval tool-call spans with the highest duration — the per-span view of
-- which tool invocations are slowest (latency question, D10). Raw spans (not
-- aggregated) so the worst individual calls, and the traces/cases they belong to,
-- are visible; `tool_latency_by_name` below gives the aggregate p95 x count view.
SELECT
    trace_id,
    span_id,
    span_name,
    attributes->>'gen_ai.tool.name' AS tool_name,
    duration AS duration_s,
    start_timestamp
FROM records
WHERE start_timestamp >= now() - interval '7 days'
  AND service_name = 'engram-evals'
  AND attributes->>'gen_ai.operation.name' = 'execute_tool'
ORDER BY duration DESC NULLS LAST
LIMIT 20

-- name: lowest_recall_cases
-- The eval cases with the worst recall@5 (e.g. recall@5 = 0 — the agent cited
-- none of the expected page ids), to explain retrieval misses (recall question,
-- D10). One row per evaluated case span (`case: {case_name}`); recall@5 and the
-- companion metrics are read from the serialized per-case `scores` attribute that
-- pydantic-evals writes. Worst first, then most tool calls (effort spent for the
-- miss). Assumption: a run has reached Logfire with the `RetrievalMetrics`
-- evaluator's `recall@5` score recorded under `attributes->'scores'`; cases
-- without that score are filtered out rather than sorted as 0.
SELECT
    trace_id,
    span_id,
    attributes->>'case_name' AS case_name,
    (attributes->'scores'->'recall@5'->>'value')::float AS recall_at_5,
    (attributes->'scores'->'recall@1'->>'value')::float AS recall_at_1,
    (attributes->'scores'->'mrr'->>'value')::float AS mrr,
    (attributes->'scores'->'n_tool_calls'->>'value')::float AS n_tool_calls,
    (attributes->>'task_duration')::float AS task_duration_s
FROM records
WHERE start_timestamp >= now() - interval '7 days'
  AND service_name = 'engram-evals'
  AND span_name = 'case: {case_name}'
  AND attributes->'scores'->'recall@5' IS NOT NULL
ORDER BY recall_at_5 ASC, n_tool_calls DESC
LIMIT 20

-- name: token_outliers
-- The runs (traces) with the highest token usage — the most token-hungry case
-- runs (cost question, D10). Aggregates `gen_ai.usage.*` across each trace's
-- model-request spans; one trace is one case run. Ordered by total tokens so the
-- outliers surface first.
SELECT
    trace_id,
    count(*) AS model_requests,
    sum((attributes->>'gen_ai.usage.input_tokens')::bigint) AS input_tokens,
    sum((attributes->>'gen_ai.usage.output_tokens')::bigint) AS output_tokens,
    sum(
        (attributes->>'gen_ai.usage.input_tokens')::bigint
        + (attributes->>'gen_ai.usage.output_tokens')::bigint
    ) AS total_tokens
FROM records
WHERE start_timestamp >= now() - interval '7 days'
  AND service_name = 'engram-evals'
  AND attributes->>'gen_ai.usage.input_tokens' IS NOT NULL
GROUP BY trace_id
ORDER BY total_tokens DESC NULLS LAST
LIMIT 20

-- name: tool_latency_by_name
-- Aggregate tool latency: per tool, the call count and p95 / max / total duration
-- — the "p95 duration x call count" ranking the RCA prompt asks for to find which
-- tool calls dominate end-to-end latency. Complements the per-span
-- `slowest_tool_spans` with the which-tool-overall view.
SELECT
    attributes->>'gen_ai.tool.name' AS tool_name,
    span_name,
    count(*) AS call_count,
    approx_percentile_cont(duration, 0.95) AS p95_duration_s,
    max(duration) AS max_duration_s,
    sum(duration) AS total_duration_s
FROM records
WHERE start_timestamp >= now() - interval '7 days'
  AND service_name = 'engram-evals'
  AND attributes->>'gen_ai.operation.name' = 'execute_tool'
GROUP BY tool_name, span_name
ORDER BY total_duration_s DESC NULLS LAST
LIMIT 20

-- name: tool_error_spans
-- Tool-call spans that raised — failed `search`/`fetch`/graph calls are a prime
-- cause of a recall miss (the agent had nothing to cite). Most recent first, with
-- the exception type/message and the trace/case to drill into.
SELECT
    trace_id,
    span_id,
    span_name,
    attributes->>'gen_ai.tool.name' AS tool_name,
    exception_type,
    exception_message,
    duration AS duration_s,
    start_timestamp
FROM records
WHERE start_timestamp >= now() - interval '7 days'
  AND service_name = 'engram-evals'
  AND attributes->>'gen_ai.operation.name' = 'execute_tool'
  AND is_exception = true
ORDER BY start_timestamp DESC
LIMIT 20
