"""What a workflow run spent, one row per model response after a header, priced when it is read.

The spend limits refuse on these figures, in process and before a request starts.
These records still answer per run when the telemetry is away. A report asks the telemetry through `spend_report` instead.
A row stores only what a reader can price again: the model, the provider, the usage counters, and the
provider's own figure when it billed one. Every read prices a row again, so a reread after a price
change or a catalog fix reprices history. A missing or torn line costs one row, never the fold.
"""

from __future__ import annotations

import asyncio

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, TypedDict

from core.observability import get_logger
from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import ModelResponse
from pydantic_ai.usage import RequestUsage

from harness.adapters.contracts import billed_cost, priced_cost

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pydantic_ai.messages import ModelMessage

    from harness.features.running.internal_contracts import SpendLogQueries

_logger = get_logger(__name__)


STORE_CARD_RUNS = 50
"""How many of a workflow's newest runs a card's one spend figure folds."""

SCHEDULE_WINDOW_BACKSTOP = 5_000
"""The most runs a schedule's day and week figures fold out of one seven-day window.

When more runs fall inside the window, only the newest are folded and the window reads as
incomplete rather than as a total.
"""


SCHEDULE_RUNNER_PREFIX = "app:schedule:"
"""What a schedule's runner starts with. The schedule's id follows it."""


class RunSpendHeader(BaseModel):
    """Who ran what: the first line of a run's spend."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    workflow_id: str
    runner: str
    """The launcher that started the run, such as a schedule's own `app:schedule:<id>`."""
    session_id: str
    """The session that launched the run, `""` for none."""
    started_at: str
    """When the run started, as ISO 8601 text."""


class SpendRow(BaseModel):
    """One model response, as priced inputs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: str
    """The step that produced the response."""
    at: str
    """When the response was produced, as ISO 8601 text."""
    model_name: str | None
    provider_name: str | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    cost: Decimal | None
    """The provider's own figure when it billed the response itself, `None` when a table prices it."""

    def priced(self) -> Decimal | None:
        """This row's price through the same ladder every session turn is priced through.

        Returns:
            The price, or `None` when no source can name a figure for it. `None` is a different
            fact from zero and is kept apart all the way to the screen.
        """
        usage = RequestUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_tokens=self.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens,
            cost=self.cost,
        )
        return priced_cost(
            ModelResponse(
                parts=[],
                model_name=self.model_name,
                provider_name=self.provider_name,
                usage=usage,
                timestamp=datetime.fromisoformat(self.at),
            )
        )


class SpendTotals(BaseModel):
    """Usage and price summed over some rows."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    unpriced: int = 0
    """How many of these rows no source could name a figure for. `cost_usd` is a floor while this is above zero."""

    def plus(self, row: SpendRow) -> SpendTotals:
        """These totals with `row` added.

        Args:
            row: The row to add. Its price is counted where it has one and tallied in `unpriced` where it has none.

        Returns:
            New totals. These are left unchanged.
        """
        price = row.priced()
        return SpendTotals(
            input_tokens=self.input_tokens + row.input_tokens,
            output_tokens=self.output_tokens + row.output_tokens,
            cache_read_tokens=self.cache_read_tokens + row.cache_read_tokens,
            cache_write_tokens=self.cache_write_tokens + row.cache_write_tokens,
            cost_usd=self.cost_usd + (price if price is not None else Decimal(0)),
            unpriced=self.unpriced + (1 if price is None else 0),
        )


class RunSpend(RunSpendHeader):
    """A run's spend, folded: its header, its totals, and each step's own."""

    totals: SpendTotals
    steps: dict[str, SpendTotals]
    """Each step's own totals, by step name."""


class SpendWindow(BaseModel):
    """The runs of one time window, and whether every run in it was read."""

    runs: list[RunSpend]
    complete: bool
    """False when more runs fell inside the window than the caller's backstop allowed to be folded."""


def rows_for(step: str, messages: Iterable[ModelMessage]) -> list[SpendRow]:
    """The spend rows `messages` yields for `step`, one per model response.

    Args:
        step: The step that produced the messages.
        messages: The step's messages. A request yields no row.

    Returns:
        One row per model response, in order.
    """
    return [
        SpendRow(
            step=step,
            at=message.timestamp.isoformat(),
            model_name=message.model_name,
            provider_name=message.provider_name,
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_tokens=message.usage.cache_read_tokens,
            cache_write_tokens=message.usage.cache_write_tokens,
            cost=billed_cost(message),
        )
        for message in messages
        if isinstance(message, ModelResponse)
    ]


class UsageView(TypedDict):
    """Token counters as the wire carries them."""

    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


class TotalsView(TypedDict):
    """One priced figure and its usage counters, as the wire carries them."""

    cost_usd: str
    unpriced: int
    """Responses no source could name a figure for. Above zero, `cost_usd` is a floor rather than the cost."""
    usage: UsageView


class SpendView(TotalsView):
    """A run's spend as the wire carries it: the whole run's totals, plus each step's own."""

    steps: dict[str, TotalsView]


def _totals_view(totals: SpendTotals) -> TotalsView:
    return TotalsView(
        cost_usd=str(totals.cost_usd),
        unpriced=totals.unpriced,
        usage=UsageView(
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            cache_read_tokens=totals.cache_read_tokens,
            cache_write_tokens=totals.cache_write_tokens,
        ),
    )


def view_of(spend: RunSpend | None) -> SpendView | None:
    """A run's spend as the wire carries it, decimals as strings.

    Args:
        spend: The folded spend, or `None`.

    Returns:
        The wire view, or `None` when `spend` is `None`.
    """
    if spend is None:
        return None
    totals = _totals_view(spend.totals)
    return SpendView(
        cost_usd=totals["cost_usd"],
        unpriced=totals["unpriced"],
        usage=totals["usage"],
        steps={name: _totals_view(step_totals) for name, step_totals in spend.steps.items()},
    )


def _recent_runs(spend_log: SpendLogQueries, workflow_id: str, limit: int) -> list[RunSpend]:
    """`workflow_id`'s newest `limit` runs, folded in one blocking call a thread can carry."""
    return spend_log.for_workflow(workflow_id, limit)


def _window_runs(spend_log: SpendLogQueries, workflow_id: str, since: datetime, limit: int) -> SpendWindow:
    """`workflow_id`'s runs written since `since`, folded in one blocking call a thread can carry."""
    return spend_log.in_window(workflow_id, since, limit)


class ScheduleSpend(BaseModel):
    """One schedule's own spend over two windows, each with the responses nothing could price beside it."""

    today: Decimal
    week: Decimal
    today_unpriced: int
    week_unpriced: int


async def workflow_spend(spend_log: SpendLogQueries, workflow_id: str, *, limit: int = STORE_CARD_RUNS) -> dict[str, object]:
    """What `workflow_id`'s newest `limit` runs cost together, and how many runs that is.

    The price and the count always describe the same runs. The runs are folded off the event loop.

    Args:
        spend_log: The spend log to read.
        workflow_id: The workflow.
        limit: The most runs folded.

    Returns:
        `cost_usd` as a decimal string, `unpriced` as the count of responses nothing could price,
        and `runs` as the count of runs read.
    """
    runs = await asyncio.to_thread(_recent_runs, spend_log, workflow_id, limit)
    return {
        "cost_usd": str(sum((run.totals.cost_usd for run in runs), Decimal(0))),
        "unpriced": sum(run.totals.unpriced for run in runs),
        "runs": len(runs),
    }


def schedule_runner(schedule_id: str) -> str:
    """The runner a schedule's fires record, which its spend is read back under.

    Args:
        schedule_id: The schedule.

    Returns:
        The runner name, `app:schedule:<id>`.
    """
    return f"{SCHEDULE_RUNNER_PREFIX}{schedule_id}"


async def schedule_spend(
    spend_log: SpendLogQueries, workflow_id: str, schedule_id: str, *, now: datetime, limit: int = SCHEDULE_WINDOW_BACKSTOP
) -> ScheduleSpend:
    """What `schedule_id`'s own fires of `workflow_id` cost today and over the last seven days.

    A fire is matched by the runner it recorded. Only runs written inside the seven-day window are
    read, and a window that `limit` cuts short is logged as a warning. The runs are folded off the
    event loop.

    Args:
        spend_log: The spend log to read.
        workflow_id: The workflow the schedule fires.
        schedule_id: The schedule.
        now: The current time. Today starts at midnight UTC.
        limit: The most runs folded out of the window.

    Returns:
        Today's cost and the week's, each with the count of responses nothing could price.
    """
    runner = schedule_runner(schedule_id)
    day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = now - timedelta(days=7)
    today = week = Decimal(0)
    today_unpriced = week_unpriced = 0
    window = await asyncio.to_thread(_window_runs, spend_log, workflow_id, week_start, limit)
    if not window.complete:
        _logger.warning(
            "more than {limit} runs of {workflow} fall in the last seven days, so schedule {schedule} is priced on the newest {limit}",
            limit=limit,
            workflow=workflow_id,
            schedule=schedule_id,
        )
    for run in window.runs:
        if run.runner != runner:
            continue
        try:
            started = datetime.fromisoformat(run.started_at)
        except ValueError:
            continue
        if started >= week_start:
            week += run.totals.cost_usd
            week_unpriced += run.totals.unpriced
        if started >= day_start:
            today += run.totals.cost_usd
            today_unpriced += run.totals.unpriced
    return ScheduleSpend(today=today, week=week, today_unpriced=today_unpriced, week_unpriced=week_unpriced)
