"""The conversation's earlier runs against a real Postgres: newest first, with the first prompt and the last event."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio

from pydantic_ai_harness.step_persistence import RunRecord, StepEvent

from core.ai.history.contracts import PostgresStepStore
from core.ai.transcripts.contracts import RecordRunsOfConversation
from tests.unit.history.conftest import snapshot, turn_messages

AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def three_runs(database) -> tuple[str, str, str]:
    """Three runs of one conversation an hour apart, each with a step and a terminal event, plus one run elsewhere."""
    store = PostgresStepStore(database)
    conversation_id = str(uuid4())
    runs: list[str] = []
    for index in range(3):
        run = f"run-{index}-{conversation_id}"
        await store.register_run(
            RunRecord(run_id=run, conversation_id=conversation_id, agent_name=f"agent-{index}", started_at=AT + timedelta(hours=index))
        )
        await store.save_snapshot(snapshot(run, 1, turn_messages(), conversation_id=conversation_id))
        await store.append_event(StepEvent(run_id=run, kind="run_completed", step_index=1, timestamp=AT + timedelta(hours=index)))
        runs.append(run)
    elsewhere = str(uuid4())
    await store.register_run(RunRecord(run_id=f"run-elsewhere-{elsewhere}", conversation_id=elsewhere, started_at=AT))
    first, second, third = runs
    return first, second, third


async def test_only_this_conversations_earlier_runs_answer_newest_first(database, three_runs) -> None:
    first, second, third = three_runs

    summaries = await RecordRunsOfConversation(database).before(f"record:{third}")

    assert [summary.source for summary in summaries] == [f"record:{second}", f"record:{first}"]
    assert [summary.agent for summary in summaries] == ["agent-1", "agent-0"]
    assert summaries[0].first_prompt == "What is the weather in Paris?"
    assert summaries[0].last_event_type == "run_completed"
    assert summaries[0].started_at == AT + timedelta(hours=1)


async def test_the_oldest_run_has_nothing_before_it_and_an_unreadable_source_answers_nothing(database, three_runs) -> None:
    first, _, _ = three_runs
    conversation = RecordRunsOfConversation(database)

    assert list(await conversation.before(f"record:{first}")) == []
    assert list(await conversation.before("record:nobody-wrote-this")) == []
    assert list(await conversation.before("claude-code:abcdef")) == []
