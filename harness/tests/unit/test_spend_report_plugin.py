"""`spend_report`: the report as the chat reads it, one line per key, and a sentence when the telemetry is away."""

from __future__ import annotations

from harness.adapters.logfire_query import QueryColumn, QueryResult, TelemetryUnreachable
from harness.features.running.spend_report import SpendReporter
from harness.features.running.tools import spend_report
from tests.support.fakes.telemetry import FakeTelemetry


def _result() -> QueryResult:
    return QueryResult(
        columns=[
            QueryColumn(name="key", datatype="Utf8", values=["draft", "relate"]),
            QueryColumn(name="calls", datatype="Int64", values=[5, 2]),
            QueryColumn(name="cost_usd", datatype="Float64", values=[0.25, 0.05]),
            QueryColumn(name="input_tokens", datatype="Int64", values=[1000, 200]),
            QueryColumn(name="output_tokens", datatype="Int64", values=[100, 20]),
            QueryColumn(name="cache_read_tokens", datatype="Int64", values=[400, 0]),
            QueryColumn(name="cache_write_tokens", datatype="Int64", values=[0, 0]),
        ]
    )


async def test_the_report_reads_as_a_total_then_one_line_per_key() -> None:
    said = await spend_report(SpendReporter(FakeTelemetry(result=_result())), "step", "run-1")

    assert said.splitlines() == [
        "Spend for step run-1 over the last 24 hours (telemetry) is $0.3000 across 7 calls, 1200 in, 120 out, 400 cached.",
        "- draft $0.2500, 5 calls, 1000 in, 100 out, 400 cached",
        "- relate $0.0500, 2 calls, 200 in, 20 out, 0 cached",
    ]


async def test_nothing_priced_says_so_and_how_to_widen() -> None:
    said = await spend_report(SpendReporter(FakeTelemetry()), "run", "run-9")
    assert said == "No priced model calls for run run-9 in the last 24 hours. Widen since_hours, or check the id with recent_runs."


async def test_unreachable_telemetry_answers_the_reason_and_the_ledger() -> None:
    failure = TelemetryUnreachable("telemetry unreachable (ConnectError)")
    said = await spend_report(SpendReporter(FakeTelemetry(failure=failure)), "model")
    assert said == (
        "telemetry unreachable (ConnectError). The spend log on disk still answers per run, "
        "GET /api/runs/history/{run_id} carries its spend."
    )


async def test_a_bad_scope_or_subject_is_a_sentence_not_an_exception() -> None:
    reporter = SpendReporter(FakeTelemetry())
    assert await spend_report(reporter, "runs") == "No scope 'runs', the scopes are day, model, run, session, step, workflow."
    assert (await spend_report(reporter, "run", "")).startswith("run needs an id of letters")
