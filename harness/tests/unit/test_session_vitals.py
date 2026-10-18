"""Cost is summed off priced responses at projection time; context is the live pool's own reading."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.compaction import ContextUsage, estimate_context_tokens
from pydantic_ai_harness.step_persistence import ContinuableSnapshot

from harness.adapters import models
from harness.adapters.model_tiers import tier_context_window
from harness.adapters.models import ModelRoute, response_cost
from harness.features.sessions.api_models import ContextVitals, SessionSummary, UsageTotals
from harness.features.sessions.controller import _summary
from harness.features.sessions.read_models import SessionRow
from harness.features.sessions.service import _fold_session_messages, _TurnUnderway
from harness.session import NoSessionLog, PoolComposition, Runtime, SessionContextUsage, SessionPool, restore_conversation
from tests.support.state import a_state
from tests.support.tiers import tiers_under

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from pydantic_ai_harness.step_persistence import RunRecord, StepEvent, ToolEffectRecord


def _response(model_name: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart("ok")], model_name=model_name, usage=RequestUsage(input_tokens=1_000, output_tokens=500))


def test_a_deepseek_response_prices_positive() -> None:
    assert response_cost(_response("deepseek-v4-pro")) > Decimal(0)


def test_an_unpriceable_response_counts_zero_never_raises() -> None:
    assert response_cost(_response("model-nobody-sells")) == Decimal(0)


def test_a_catalog_model_the_price_registry_does_not_know_counts_zero_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deepseek-vendor catalog entry the underlying price table has never heard of must still degrade to zero."""
    monkeypatch.setitem(models.CATALOG, "deepseek-catalog-only", ModelRoute(vendor="deepseek", api="chat"))
    assert response_cost(_response("deepseek-catalog-only")) == Decimal(0)


def test_a_providers_own_cost_figure_wins_over_every_table() -> None:
    """OpenRouter bills per response and reports the exact amount; that figure is the cost, untouched."""
    response = ModelResponse(
        parts=[TextPart("ok")],
        model_name="deepseek/deepseek-v4-pro",
        provider_name="openrouter",
        usage=RequestUsage(input_tokens=11_265, output_tokens=96, cost=Decimal("0.004983795")),
    )
    assert response_cost(response) == Decimal("0.004983795")


def test_an_openrouter_response_without_its_own_figure_is_priced_under_its_provider() -> None:
    """The registry is asked under the response's provider name, where the wire name is listed."""
    response = ModelResponse(
        parts=[TextPart("ok")],
        model_name="deepseek/deepseek-v4-pro",
        provider_name="openrouter",
        usage=RequestUsage(input_tokens=11_265, output_tokens=96),
    )
    assert response_cost(response) > Decimal(0)


async def test_the_context_reading_lands_on_the_answering_session(tmp_path: Path) -> None:
    runtime = Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_log=NoSessionLog(), state=a_state(tmp_path))
    tiers = tiers_under(tmp_path)
    agent = Agent(TestModel(), deps_type=Runtime, capabilities=[SessionContextUsage(tiers=tiers)])

    await agent.run("how full is the context?", deps=runtime)

    assert runtime.context_usage is not None
    assert runtime.context_usage.used_tokens > 0
    assert runtime.context_usage.window_tokens == tier_context_window(tiers, "orchestrator")
    assert runtime.context_usage.resolved is True


def _default_runtime(home: Path, session_id: str = "s-default") -> Runtime:
    return Runtime(registry=StepRegistry(), library=cast("Any", None), agent=cast("Any", None), session_id=session_id, session_log=NoSessionLog(), state=a_state(home))


def test_context_of_returns_none_for_a_session_this_process_does_not_hold(tmp_path: Path) -> None:
    pool = SessionPool(_default_runtime(tmp_path))
    assert pool.context_of("s-elsewhere") is None


def test_context_of_estimates_from_the_settled_history_when_held(tmp_path: Path) -> None:
    """`used_tokens` anchors on the reported usage and estimates only the tail after it.

    The same way `ReportContextUsage._measure` would over this same history.
    """
    runtime = _default_runtime(tmp_path)
    response = ModelResponse(parts=[TextPart("ok")], usage=RequestUsage(input_tokens=3_000, output_tokens=200))
    trailing = ModelRequest(parts=[UserPromptPart(content="one more turn, sized by the same estimator as the anchor")])
    runtime.conversation.history = [ModelRequest(parts=[UserPromptPart(content="first turn")]), response, trailing]
    tiers = tiers_under(tmp_path)
    pool = SessionPool(runtime, composition=PoolComposition(tiers=tiers))

    usage = pool.context_of("s-default")

    assert usage is not None
    expected_tail = estimate_context_tokens([trailing], model_request_parameters=None)
    assert usage.used_tokens == 3_200 + expected_tail
    # The window follows the orchestrator tier's model, read from the catalog rather than the wire name.
    assert usage.window_tokens == tier_context_window(tiers, "orchestrator")
    assert usage.resolved is True


def test_context_of_is_zero_of_the_window_for_an_empty_history(tmp_path: Path) -> None:
    pool = SessionPool(_default_runtime(tmp_path))

    usage = pool.context_of("s-default")

    assert usage is not None
    assert usage.used_tokens == 0
    assert usage.window_tokens > 0


def test_context_of_prefers_the_on_demand_reading_over_an_idle_stash(tmp_path: Path) -> None:
    """Idle, the stash is one request stale: the settled history wins even though a stash exists."""
    runtime = _default_runtime(tmp_path)
    trailing = ModelRequest(parts=[UserPromptPart(content="settled since the stash was taken")])
    runtime.conversation.history = [trailing]
    runtime.context_usage = ContextUsage(used_tokens=999_999, window_tokens=1, resolved=False)
    pool = SessionPool(runtime)

    usage = pool.context_of("s-default")

    assert usage is not None
    assert usage != runtime.context_usage
    assert usage.used_tokens == estimate_context_tokens([trailing], model_request_parameters=None)


async def test_context_of_answers_the_stash_while_a_turn_is_in_flight(tmp_path: Path) -> None:
    """Mid-turn the history is not yet settled, so the pre-request stash is the fresher fact."""
    runtime = _default_runtime(tmp_path)
    runtime.conversation.history = [ModelRequest(parts=[UserPromptPart(content="mid turn, history not yet settled")])]
    stash = ContextUsage(used_tokens=42, window_tokens=100, resolved=True)
    runtime.context_usage = stash
    pool = SessionPool(runtime)
    hold = await pool.begin_turn("s-default")

    usage = pool.context_of("s-default")

    assert usage == stash
    hold.release()


def test_a_hydrated_session_has_a_context_reading(tmp_path: Path) -> None:
    """Hydration only fills `history` -- the same on-demand path answers for it too."""
    runtime = _default_runtime(tmp_path)
    messages = [ModelRequest(parts=[UserPromptPart(content="resumed from a snapshot")])]
    restore_conversation(runtime.conversation, messages, runtime.registry, draft_file=a_state(tmp_path).draft)
    pool = SessionPool(runtime)

    usage = pool.context_of("s-default")

    assert usage is not None
    assert usage.used_tokens == estimate_context_tokens(messages, model_request_parameters=None)


class _FakeStepStore:
    """A `StepStore` stand-in holding one snapshot; every method but `latest_snapshot` is unused here."""

    def __init__(self, snapshot: ContinuableSnapshot) -> None:
        self._snapshot = snapshot

    async def register_run(self, record: RunRecord) -> None:
        pass

    async def get_run(self, *, run_id: str) -> RunRecord | None:
        return None

    async def list_runs(self, *, parent_run_id: str | None = None, conversation_id: str | None = None) -> list[RunRecord]:
        return []

    async def append_event(self, event: StepEvent) -> None:
        pass

    async def list_events(self, *, run_id: str) -> list[StepEvent]:
        return []

    async def save_snapshot(self, snapshot: ContinuableSnapshot) -> None:
        pass

    async def latest_snapshot(self, *, run_id: str, include_interrupted: bool = False) -> ContinuableSnapshot | None:
        return self._snapshot if run_id == self._snapshot.run_id else None

    async def record_tool_effect(self, record: ToolEffectRecord) -> None:
        pass

    async def get_tool_effect(self, *, run_id: str, tool_call_id: str) -> ToolEffectRecord | None:
        return None

    async def list_unresolved_tool_effects(self, *, run_id: str) -> list[ToolEffectRecord]:
        return []


async def test_the_projection_fold_prices_a_turns_row_off_its_settled_response() -> None:
    response = _response("deepseek-v4-pro")
    snapshot = ContinuableSnapshot(run_id="run-1", step_index=0, messages=[response])
    row = _TurnUnderway(session_id="s-1", seq=0, entry="spa", started_at=datetime(2026, 8, 26, tzinfo=UTC), run_id="run-1")

    await _fold_session_messages([row], _FakeStepStore(snapshot))

    assert row.cost > Decimal(0)


def _session_row(*, cost: Decimal, unpriced: int = 0) -> SessionRow:
    """One projected `sessions` row, built the way the projection validates one."""
    stamp = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
    return SessionRow(
        id=uuid4(),
        project_id=uuid4(),
        title="t",
        started_at=stamp,
        last_activity_at=stamp,
        archived=False,
        turn_count=3,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=10,
        cache_write_tokens=5,
        cost=cost,
        unpriced=unpriced,
        session_kind="chat",
    )


def test_summary_carries_the_priced_cost_and_no_context_when_the_pool_holds_no_gauge() -> None:
    summary = _summary(_session_row(cost=Decimal("1.50")), answering=False, context=None)

    assert summary.cost_usd == Decimal("1.50")
    assert summary.context is None
    assert summary.usage == UsageTotals(input_tokens=100, output_tokens=50, cache_read_tokens=10, cache_write_tokens=5)


def test_summary_reports_the_live_context_gauge_as_a_fraction() -> None:
    reading = ContextUsage(used_tokens=250, window_tokens=1_000, resolved=True)

    summary = _summary(_session_row(cost=Decimal(0)), answering=True, context=reading)

    assert summary.context == ContextVitals(used_tokens=250, window_tokens=1_000, fraction=0.25, resolved=True)


def test_session_summary_serialises_cost_as_a_decimal_string_on_the_wire() -> None:
    stamp = datetime(2026, 8, 26, tzinfo=UTC)
    summary = SessionSummary(
        id="s",
        project_id="p",
        title="t",
        started_at=stamp,
        last_activity_at=stamp,
        archived=False,
        answering=False,
        turn_count=0,
        usage=UsageTotals(),
        kind="chat",
        cost_usd=Decimal("2.75"),
        context=None,
    )
    assert summary.model_dump(mode="json")["cost_usd"] == "2.75"


def test_a_summary_says_how_many_of_its_responses_no_source_could_price() -> None:
    """A conversation on a model with no published price reads as unknown, never as nothing spent."""
    summary = _summary(_session_row(cost=Decimal(0), unpriced=7), answering=False, context=None)

    assert summary.cost_usd == Decimal(0)
    assert summary.unpriced == 7, "zero here means unknown, and this count is the only thing that says so"
