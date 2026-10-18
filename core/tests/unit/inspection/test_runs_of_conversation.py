"""The port for a conversation's earlier runs: what a caller gets, and what a fake stands in with."""

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from core.ai.transcripts.contracts import RunsOfConversation, RunSummary
from tests.support.fakes.inspection import FakeRunsOfConversation

AT = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)
EARLIER = RunSummary(
    source="record:run-0", agent="harness-author", started_at=AT, first_prompt="index the notes", last_event_type="run_completed"
)


async def test_the_fake_answers_the_runs_it_holds_and_nothing_for_another_source() -> None:
    conversation: RunsOfConversation = FakeRunsOfConversation(by_source={"record:run-1": [EARLIER]})

    assert list(await conversation.before("record:run-1")) == [EARLIER]
    assert list(await conversation.before("record:run-9")) == []


def test_a_summary_is_frozen_and_carries_the_five_things_a_reader_needs() -> None:
    assert (EARLIER.source, EARLIER.agent, EARLIER.started_at, EARLIER.first_prompt, EARLIER.last_event_type) == (
        "record:run-0",
        "harness-author",
        AT,
        "index the notes",
        "run_completed",
    )
    field = "source"
    with pytest.raises(FrozenInstanceError):
        setattr(EARLIER, field, "record:run-9")
