"""Every model response a run produced, kept as priced inputs and priced whenever it is read."""

from __future__ import annotations

import asyncio
import os

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from harness.adapters.models import CATALOG, openrouter_wire_name, priced_cost
from harness.features.running.infrastructure.spend_log import FileSpendLog, _listed
from harness.features.running.spend import (
    SCHEDULE_WINDOW_BACKSTOP,
    STORE_CARD_RUNS,
    RunSpendHeader,
    SpendRow,
    rows_for,
    schedule_runner,
    schedule_spend,
    view_of,
    workflow_spend,
)

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


_EPOCH_BASE = 1_700_000_000
"""The first modification time `_ledger_with` stamps, so which ledger is newest never depends on how fast the test ran."""

_QUARTER_HOURLY_WEEK = 672
"""Fires a schedule running every fifteen minutes puts in one seven-day window."""

_FIRE_COST = Decimal("0.004")
"""What one fire of `_fires` cost, reported by the provider itself so the arithmetic is exact."""

_NOON_ANCHOR = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)
"""A fixed instant well inside both a day and a week, so a fold's day/week windows never depend on
the wall clock the test happens to run at -- spacing fires back a few hours from `datetime.now(UTC)`
can cross a calendar-day boundary near midnight UTC and break a today-equals-week assertion."""


def _ledger(home: Path) -> FileSpendLog:
    """This test's own ledger, under its own state home."""
    return FileSpendLog(home / "runs" / "spend")


def _response(model: str = "deepseek-v4-pro-0813", *, cost: Decimal | None = None, provider: str | None = None) -> ModelResponse:
    usage = RequestUsage(input_tokens=1_000, output_tokens=500, cost=cost)
    return ModelResponse(parts=[TextPart("ok")], model_name=model, provider_name=provider, usage=usage)


def _header(run_id: str = "run-1", workflow_id: str = "wf.digest.v3", runner: str = "app:harness") -> RunSpendHeader:
    return RunSpendHeader(run_id=run_id, workflow_id=workflow_id, runner=runner, session_id="s-1", started_at=datetime.now(UTC).isoformat())


def test_a_row_keeps_the_figure_the_host_billed() -> None:
    billed = ModelResponse(
        parts=[TextPart("ok")],
        model_name="z-ai/glm-5.3-flash",
        provider_name="openrouter",
        usage=RequestUsage(input_tokens=19, output_tokens=32),
        provider_details={"cost": 6.35e-06, "downstream_provider": "Io Net"},
    )

    (row,) = rows_for("draft", [billed])

    assert row.cost == Decimal("0.00000635")
    assert row.priced() == Decimal("0.00000635")


def test_rows_for_keeps_only_model_responses_with_their_priced_inputs() -> None:
    rows = rows_for("draft", [ModelRequest(parts=[UserPromptPart(content="go")]), _response(cost=Decimal("0.004"), provider="openrouter")])
    assert len(rows) == 1
    assert rows[0].step == "draft"
    assert rows[0].model_name == "deepseek-v4-pro-0813"
    assert rows[0].provider_name == "openrouter"
    assert (rows[0].input_tokens, rows[0].output_tokens) == (1_000, 500)
    assert rows[0].cost == Decimal("0.004")


def test_a_run_folds_per_step_and_in_total(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.begin(_header())
    ledger.record("run-1", "wf.digest.v3", rows_for("draft", [_response(), _response()]))
    ledger.record("run-1", "wf.digest.v3", rows_for("review", [_response("model-nobody-sells")]))
    spend = ledger.read("run-1", "wf.digest.v3")
    assert spend is not None
    assert spend.runner == "app:harness"
    assert set(spend.steps) == {"draft", "review"}
    assert spend.steps["draft"].input_tokens == 2_000
    assert spend.steps["draft"].cost_usd > Decimal(0)
    assert spend.steps["review"].cost_usd == Decimal(0), "an unpriceable response never fails the fold"
    assert spend.steps["review"].unpriced == 1, "and it is counted as unknown rather than passed off as free"
    assert spend.steps["draft"].unpriced == 0
    assert spend.totals.cost_usd == spend.steps["draft"].cost_usd
    assert spend.totals.unpriced == 1, "the run knows one of its responses has no figure"
    assert spend.totals.input_tokens == 3_000


def test_a_row_with_an_unparseable_timestamp_costs_only_that_row(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.begin(_header())
    bad = SpendRow(
        step="draft",
        at="not-a-real-timestamp",
        model_name="deepseek-v4-pro-0813",
        provider_name=None,
        input_tokens=10_000,
        output_tokens=10_000,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cost=None,
    )
    ledger.record("run-1", "wf.digest.v3", [bad, *rows_for("draft", [_response()])])
    spend = ledger.read("run-1", "wf.digest.v3")
    assert spend is not None, "a syntactically valid row with a bad timestamp must not crash the fold"
    assert spend.steps["draft"].input_tokens == 1_000, "the bad row is dropped whole, the good row still counts"
    assert spend.totals.input_tokens == 1_000


def test_a_byte_corrupted_line_costs_only_that_row(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.begin(_header())
    ledger.record("run-1", "wf.digest.v3", rows_for("draft", [_response()]))
    path = tmp_path / "runs" / "spend" / "wf.digest.v3" / "run-1.jsonl"
    with path.open("ab") as handle:
        handle.write(b"\xff\xfe not valid utf-8\n")
    spend = ledger.read("run-1", "wf.digest.v3")
    assert spend is not None, "a byte-corrupted line must not crash the read"
    assert spend.steps["draft"].input_tokens == 1_000, "the corrupted line is dropped whole, the good row still counts"
    assert spend.totals.input_tokens == 1_000


def test_a_providers_own_figure_is_the_price_on_read(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.begin(_header())
    ledger.record("run-1", "wf.digest.v3", rows_for("draft", [_response(cost=Decimal("0.0123"), provider="openrouter")]))
    spend = ledger.read("run-1", "wf.digest.v3")
    assert spend is not None
    assert spend.totals.cost_usd == Decimal("0.0123")


def test_rows_before_a_header_and_a_missing_run_read_as_none(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.record("orphan", "wf.x", rows_for("draft", [_response()]))
    assert ledger.read("orphan", "wf.x") is None
    assert ledger.read("nothing", "wf.x") is None
    assert ledger.find("nothing") is None


def test_for_workflow_lists_every_run_newest_first_and_find_locates_by_id(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.begin(_header("run-a", runner="app:schedule:sched-1"))
    ledger.begin(_header("run-b"))
    ledger.record("run-b", "wf.digest.v3", rows_for("draft", [_response()]))
    runs = ledger.for_workflow("wf.digest.v3", 10)
    assert [run.run_id for run in runs] == ["run-b", "run-a"]
    assert runs[1].runner == "app:schedule:sched-1"
    assert ledger.find("run-a") is not None
    assert ledger.for_workflow("wf.none", 10) == []


def test_the_ledger_lives_under_the_run_state_home(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    assert isinstance(ledger, FileSpendLog)
    ledger.begin(_header())
    assert (tmp_path / "runs" / "spend" / "wf.digest.v3" / "run-1.jsonl").is_file()


def test_a_run_whose_every_response_is_unpriceable_reports_a_zero_floor_and_says_so(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.begin(_header())
    ledger.record("run-1", "wf.digest.v3", rows_for("draft", [_response("model-nobody-sells"), _response("model-nobody-sells")]))
    spend = ledger.read("run-1", "wf.digest.v3")
    assert spend is not None
    assert spend.totals.cost_usd == Decimal(0)
    assert spend.totals.unpriced == 2, "a zero here means unknown, and the count is the only thing that says so"
    view = view_of(spend)
    assert view is not None
    assert view["unpriced"] == 2, "the wire carries it, because the screen decides what to say"


def test_view_of_renders_decimals_as_strings_and_none_as_none(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path)
    ledger.begin(_header())
    ledger.record("run-1", "wf.digest.v3", rows_for("draft", [_response()]))
    view = view_of(ledger.read("run-1", "wf.digest.v3"))
    assert view is not None
    assert isinstance(view["cost_usd"], str)
    assert view["usage"]["input_tokens"] == 1_000
    assert set(view["steps"]) == {"draft"}
    assert view_of(None) is None


def _ledger_with(runs: int, *, workflow_id: str = "wf.digest.v3", runner: str = "app:harness", tmp_path: Path) -> list[str]:
    """`runs` one-response ledgers for `workflow_id`, oldest first, each written a second after the last."""
    ledger = _ledger(tmp_path)
    run_ids: list[str] = []
    folder = tmp_path / "runs" / "spend" / workflow_id
    for index in range(runs):
        run_id = f"run-{index:03d}"
        ledger.begin(_header(run_id, workflow_id=workflow_id, runner=runner))
        ledger.record(run_id, workflow_id, rows_for("draft", [_response()]))
        written = _EPOCH_BASE + index
        os.utime(folder / f"{run_id}.jsonl", (written, written))
        run_ids.append(run_id)
    return run_ids


def _fires(count: int, *, runner: str, now: datetime, spacing: timedelta, prefix: str = "fire", workflow_id: str = "wf.digest.v3", tmp_path: Path) -> None:
    """`count` one-response fires of `workflow_id`, the newest at `now` and each older by `spacing` again.

    Each ledger's last write is stamped at the fire's own start, which is what the window selects on.
    """
    ledger = _ledger(tmp_path)
    folder = tmp_path / "runs" / "spend" / workflow_id
    for index in range(count):
        started = now - spacing * index
        run_id = f"{prefix}-{index:05d}"
        ledger.begin(RunSpendHeader(run_id=run_id, workflow_id=workflow_id, runner=runner, session_id="s", started_at=started.isoformat()))
        ledger.record(run_id, workflow_id, rows_for("draft", [_response(cost=_FIRE_COST, provider="openrouter")]))
        written = started.timestamp()
        os.utime(folder / f"{run_id}.jsonl", (written, written))


def test_for_workflow_folds_no_more_ledgers_than_the_limit_asks_for(tmp_path: Path) -> None:
    _ledger_with(5, tmp_path=tmp_path)

    folded = _ledger(tmp_path).for_workflow("wf.digest.v3", 2)

    assert [run.run_id for run in folded] == ["run-004", "run-003"], "the limit bounds the fold to the newest ledgers"


def test_the_limit_keeps_the_most_recently_written_ledgers(tmp_path: Path) -> None:
    _ledger_with(5, tmp_path=tmp_path)
    folder = tmp_path / "runs" / "spend" / "wf.digest.v3"
    revived = _EPOCH_BASE + 100
    os.utime(folder / "run-000.jsonl", (revived, revived))

    folded = _ledger(tmp_path).for_workflow("wf.digest.v3", 1)

    assert [run.run_id for run in folded] == ["run-000"], "the ledger written last is the one a limit of one keeps"


def test_a_file_that_is_not_a_ledger_is_not_folded_and_does_not_use_up_the_limit(tmp_path: Path) -> None:
    _ledger_with(2, tmp_path=tmp_path)
    (tmp_path / "runs" / "spend" / "wf.digest.v3" / "notes.txt").write_text("not a ledger", encoding="utf-8")

    folded = _ledger(tmp_path).for_workflow("wf.digest.v3", 2)

    assert [run.run_id for run in folded] == ["run-001", "run-000"]


async def test_a_cards_spend_folds_the_newest_runs_up_to_its_own_cap(tmp_path: Path) -> None:
    _ledger_with(STORE_CARD_RUNS + 2, tmp_path=tmp_path)

    spend = await workflow_spend(_ledger(tmp_path), "wf.digest.v3")

    assert spend["runs"] == STORE_CARD_RUNS, "a card names its cap in its own signature and folds no more than that"


async def test_a_caller_may_ask_a_card_for_fewer_runs_than_the_cap(tmp_path: Path) -> None:
    _ledger_with(4, tmp_path=tmp_path)

    spend = await workflow_spend(_ledger(tmp_path), "wf.digest.v3", limit=2)

    assert spend["runs"] == 2


async def test_the_workflow_fold_leaves_the_loop_free_while_it_reads(tmp_path: Path) -> None:
    _ledger_with(3, tmp_path=tmp_path)
    ran_meanwhile = asyncio.Event()

    async def other_work() -> None:
        ran_meanwhile.set()

    waiting = asyncio.create_task(other_work())
    spend = await workflow_spend(_ledger(tmp_path), "wf.digest.v3")
    ran_before_the_fold_returned = ran_meanwhile.is_set()
    await waiting

    assert ran_before_the_fold_returned, "the fold must run off the loop so other work proceeds while it reads"
    assert spend["runs"] == 3


async def test_the_schedule_fold_leaves_the_loop_free_while_it_reads(tmp_path: Path) -> None:
    now = _NOON_ANCHOR
    _fires(3, runner="app:schedule:sched-1", now=now, spacing=timedelta(hours=1), tmp_path=tmp_path)
    ran_meanwhile = asyncio.Event()

    async def other_work() -> None:
        ran_meanwhile.set()

    waiting = asyncio.create_task(other_work())
    spend = await schedule_spend(_ledger(tmp_path), "wf.digest.v3", "sched-1", now=now)
    ran_before_the_fold_returned = ran_meanwhile.is_set()
    await waiting

    assert ran_before_the_fold_returned, "the fold must run off the loop so other work proceeds while it reads"
    assert spend.today > Decimal(0)
    assert spend.week == spend.today


async def test_a_schedules_spend_counts_only_the_fires_recorded_under_its_own_runner(tmp_path: Path) -> None:
    now = _NOON_ANCHOR
    _fires(2, runner=schedule_runner("sched-1"), now=now, spacing=timedelta(hours=1), tmp_path=tmp_path)
    _fires(1, runner=schedule_runner("sched-2"), now=now, spacing=timedelta(hours=1), prefix="other", tmp_path=tmp_path)

    spend = await schedule_spend(_ledger(tmp_path), "wf.digest.v3", "sched-1", now=now)

    assert spend.week == _FIRE_COST * 2, "another schedule's fire of the same workflow is not this one's"


async def test_a_whole_week_of_quarter_hourly_fires_is_counted_whole(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _fires(_QUARTER_HOURLY_WEEK, runner="app:schedule:sched-1", now=now, spacing=timedelta(minutes=15), tmp_path=tmp_path)

    spend = await schedule_spend(_ledger(tmp_path), "wf.digest.v3", "sched-1", now=now)

    assert spend.week == _FIRE_COST * _QUARTER_HOURLY_WEEK, "a seven-day figure counts every fire in the seven days, however many that is"
    assert spend.today < spend.week, "the day figure counts only the fires of today"


async def test_a_ledger_written_before_the_window_is_never_folded(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _fires(2, runner="app:schedule:sched-1", now=now, spacing=timedelta(hours=1), tmp_path=tmp_path)
    _fires(40, runner="app:schedule:sched-1", now=now - timedelta(days=30), spacing=timedelta(hours=1), prefix="old", tmp_path=tmp_path)

    window = _ledger(tmp_path).in_window("wf.digest.v3", now - timedelta(days=7), SCHEDULE_WINDOW_BACKSTOP)

    assert [run.run_id for run in window.runs] == ["fire-00000", "fire-00001"], "the window bounds the fold, not the history"
    assert window.complete


async def test_the_window_says_so_when_the_backstop_cuts_it_short(tmp_path: Path) -> None:
    now = _NOON_ANCHOR
    _fires(4, runner="app:schedule:sched-1", now=now, spacing=timedelta(hours=1), tmp_path=tmp_path)

    window = _ledger(tmp_path).in_window("wf.digest.v3", now - timedelta(days=7), 2)
    spend = await schedule_spend(_ledger(tmp_path), "wf.digest.v3", "sched-1", now=now, limit=2)

    assert window.complete is False, "a window the backstop cut short never reads as a complete one"
    assert [run.run_id for run in window.runs] == ["fire-00000", "fire-00001"], "the newest fires are the ones kept"
    assert spend.week == _FIRE_COST * 2
    assert spend.today == spend.week


def test_the_selection_hands_the_fold_no_more_files_than_the_limit(tmp_path: Path) -> None:
    _ledger_with(5, tmp_path=tmp_path)

    listed = _listed(tmp_path / "runs" / "spend" / "wf.digest.v3")

    assert [name for _, name in listed[:2]] == ["run-004.jsonl", "run-003.jsonl"], "selection orders by last write and the caller slices it"


async def test_a_ledger_that_cannot_be_read_costs_only_itself(tmp_path: Path) -> None:
    _ledger_with(3, tmp_path=tmp_path)
    (tmp_path / "runs" / "spend" / "wf.digest.v3" / "run-001.jsonl").write_bytes(b"\xff\xfe not a header\n")

    spend = await workflow_spend(_ledger(tmp_path), "wf.digest.v3")

    assert spend["runs"] == 2, "an unreadable ledger reads as nothing and never raises into a route"


async def test_both_figures_stay_decimal_all_the_way_to_the_wire(tmp_path: Path) -> None:
    _fires(2, runner="app:schedule:sched-1", now=datetime.now(UTC), spacing=timedelta(hours=1), tmp_path=tmp_path)
    folded = _ledger(tmp_path).for_workflow("wf.digest.v3", 2)

    card = await workflow_spend(_ledger(tmp_path), "wf.digest.v3")
    spend = await schedule_spend(_ledger(tmp_path), "wf.digest.v3", "sched-1", now=datetime.now(UTC))

    assert card["cost_usd"] == str(sum((run.totals.cost_usd for run in folded), Decimal(0)))
    assert isinstance(spend.today, Decimal)
    assert isinstance(spend.week, Decimal)


def test_every_model_this_deployment_can_run_has_a_price_of_its_own() -> None:
    """The ledger's floor, asked of the real catalog rather than of a fixture that resembles it.

    Most responses carry the figure their provider billed, but a deployment cannot rest on that: the
    moment one arrives without it, a model no table carries reads as unknown and a run's cost stops
    being sayable. Every model this deployment can actually run is checked here, under the name it is
    actually sent by, so adding one without a price fails here rather than on somebody's screen.
    """
    usage = RequestUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    for model_id in CATALOG:
        wire = openrouter_wire_name(model_id)
        names = [(model_id, "openai")] if wire is None else [(wire, "openrouter"), (model_id, "openai")]
        priced = [
            name
            for name, provider in names
            if priced_cost(
                ModelResponse(parts=[], usage=usage, model_name=name, provider_name=provider, timestamp=datetime.now(UTC))
            )
            is not None
        ]
        assert priced, f"{model_id} is in the catalog but no source can price it, so every run of it would read as unknown"


def test_rows_for_a_run_with_no_header_are_dropped_and_the_loss_is_reported(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    """Money a run really spent must never leave the ledger quietly.

    A row with no header cannot be filed, and inventing a header would put a runner and a session on
    the record that nobody reported. So the drop is said out loud instead, because a total quietly
    missing it reads as a cheaper run rather than an incomplete one.
    """
    ledger = _ledger(tmp_path)
    with caplog.at_level("WARNING", logger="harness.features.running.infrastructure.spend_log"):
        ledger.record("never-opened", "wf.digest.v3", rows_for("draft", [_response(), _response()]))

    assert ledger.read("never-opened", "wf.digest.v3") is None
    [said] = [record for record in caplog.records if record.name == "harness.features.running.infrastructure.spend_log"]
    assert said.__dict__["run"] == "never-opened", "the run that lost the rows is named"
    assert said.__dict__["workflow"] == "wf.digest.v3", "and so is the workflow it belongs to"
    assert said.__dict__["count"] == 2, "and how many rows went with it"
