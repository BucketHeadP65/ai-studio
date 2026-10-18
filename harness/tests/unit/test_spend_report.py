"""Six named questions, one query each, folded into lines and a total, and an honest answer when the telemetry is away."""

from __future__ import annotations

import re

from decimal import Decimal

import pytest

from harness.adapters.logfire_query import QueryColumn, QueryResult, TelemetryUnreachable
from harness.features.running.spend_report import SpendReport, SpendReporter, SpendUnavailable, parse_scope, sql_for
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
            QueryColumn(name="cache_write_tokens", datatype="Int64", values=[0, None]),
        ]
    )


class TestSql:
    def test_run_narrows_on_the_run_id_and_keys_by_it(self) -> None:
        sql = sql_for("run", "run-1", 24)
        assert "attributes ? 'operation.cost'" in sql
        assert "interval '24 hours'" in sql
        assert "attributes->>'run_id' = 'run-1'" in sql
        assert "select attributes->>'run_id' as key" in sql

    def test_step_keys_by_the_step_baggage(self) -> None:
        assert "coalesce(attributes->>'step', 'unknown') as key" in sql_for("step", "run-1", 24)

    def test_workflow_narrows_on_the_workflow_and_lines_per_run(self) -> None:
        sql = sql_for("workflow", "wf.digest.v9", 168)
        assert "attributes->>'workflow' = 'wf.digest.v9'" in sql
        assert "select attributes->>'run_id' as key" in sql

    def test_session_reads_the_request_root_that_names_it(self) -> None:
        sql = sql_for("session", "9c34a9bd-c828-4e86-8025-bac6bfa796fd", 24)
        assert "attributes->>'http.target' like '/api/sessions/9c34a9bd-c828-4e86-8025-bac6bfa796fd/%'" in sql
        assert "coalesce(attributes->>'run_id', 'chat') as key" in sql

    def test_model_and_day_take_no_subject(self) -> None:
        assert "concat(attributes->>'gen_ai.provider.name', '/', attributes->>'gen_ai.response.model') as key" in sql_for("model", "", 24)
        day = sql_for("day", "", 72)
        assert "cast(day as varchar) as key" in day
        assert day.endswith("order by key")

    @pytest.mark.parametrize("subject", ["", "x'; drop table records; --", "a" * 81, "with space"])
    def test_a_subject_that_cannot_be_written_into_a_query_is_refused(self, subject: str) -> None:
        with pytest.raises(ValueError, match="run needs an id of letters, digits, dots, dashes, colons or underscores"):
            sql_for("run", subject, 24)

    @pytest.mark.parametrize("hours", [0, 2161])
    def test_a_window_outside_the_band_is_refused(self, hours: int) -> None:
        with pytest.raises(ValueError, match="since_hours must be between 1 and 2160"):
            sql_for("model", "", hours)

    @pytest.mark.parametrize("subject", ["a_b"])
    def test_a_session_subject_with_an_underscore_is_refused_as_a_wildcard(self, subject: str) -> None:
        with pytest.raises(ValueError, match="session needs an id of letters, digits, dots, dashes or colons"):
            sql_for("session", subject, 24)


class TestParseScope:
    def test_the_six_names(self) -> None:
        assert [parse_scope(name) for name in ("run", "workflow", "session", "step", "model", "day")] == [
            "run", "workflow", "session", "step", "model", "day"
        ]

    def test_another_name_is_refused_with_the_list(self) -> None:
        with pytest.raises(ValueError, match=re.escape("No scope 'runs', the scopes are day, model, run, session, step, workflow.")):
            parse_scope("runs")


class TestReport:
    async def test_lines_and_their_total(self) -> None:
        telemetry = FakeTelemetry(result=_result())

        answer = await SpendReporter(telemetry).report("step", "run-1", 24)

        assert isinstance(answer, SpendReport)
        assert answer.scope == "step"
        assert answer.subject == "run-1"
        assert [line.key for line in answer.lines] == ["draft", "relate"]
        assert answer.lines[0].cost_usd == Decimal("0.25")
        assert answer.lines[1].cache_write_tokens == 0
        assert answer.total.calls == 7
        assert answer.total.cost_usd == Decimal("0.30")
        assert answer.total.input_tokens == 1200
        assert answer.total.cache_read_tokens == 400
        assert answer.source == "telemetry"
        assert telemetry.asked == [sql_for("step", "run-1", 24)]

    async def test_nothing_priced_is_an_empty_report_not_a_failure(self) -> None:
        answer = await SpendReporter(FakeTelemetry()).report("run", "run-9", 24)

        assert isinstance(answer, SpendReport)
        assert answer.lines == ()
        assert answer.total.calls == 0
        assert answer.total.cost_usd == Decimal(0)

    async def test_unreachable_telemetry_is_said_and_names_the_ledger(self) -> None:
        failure = TelemetryUnreachable("telemetry rate limited, ask again in a minute")

        answer = await SpendReporter(FakeTelemetry(failure=failure)).report("model", "", 24)

        assert isinstance(answer, SpendUnavailable)
        assert answer.reason == "telemetry rate limited, ask again in a minute"
        assert answer.fallback == "The spend log on disk still answers per run, GET /api/runs/history/{run_id} carries its spend."

    async def test_a_bad_subject_is_the_callers_error(self) -> None:
        with pytest.raises(ValueError, match="run needs an id"):
            await SpendReporter(FakeTelemetry()).report("run", "", 24)
