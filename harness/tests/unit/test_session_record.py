"""What a session carries: the one record every turn of it writes through, and what recall reads back out of it."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, InMemoryStepStore, RunRecord

from harness.adapters.record import RecordHistorySource
from tests.support.runtime import a_runtime

AT = datetime(2026, 9, 7, 12, 0, tzinfo=UTC)
"""One fixed instant every message here is stamped from, so a key is never a clock reading."""

SUMMARY_PREFIX = "Summary of previous conversation:\n\n"
"""What a summarizing compaction writes in front of the summary it inserts, spelled as the artifact carries it."""

CONVERSATION = "a-session"
RUN = "harness-author-1"


def _asked(said: str, *, minute: int) -> ModelRequest:
    """One prompt of the conversation, distinct from every other by the moment its part carries."""
    return ModelRequest(parts=[UserPromptPart(content=said, timestamp=AT.replace(minute=minute))])


def _answered(said: str, *, minute: int) -> ModelResponse:
    """One answer of the conversation, distinct from every other by the moment it carries."""
    return ModelResponse(parts=[TextPart(content=said)], timestamp=AT.replace(minute=minute))


def _summary(of: str, *, minute: int) -> ModelRequest:
    """What compaction writes in place of the messages it replaced: the artifact, in its own step."""
    return ModelRequest(parts=[SystemPromptPart(content=f"{SUMMARY_PREFIX}{of}", timestamp=AT.replace(minute=minute))])


async def _a_run_of(*steps: list[ModelMessage]) -> RecordHistorySource:
    """One run whose steps hold `steps` in order, and the source that reads its conversation back."""
    store = InMemoryStepStore()
    await store.register_run(RunRecord(run_id=RUN, conversation_id=CONVERSATION))
    for index, messages in enumerate(steps):
        await store.save_snapshot(ContinuableSnapshot(run_id=RUN, step_index=index, messages=messages, conversation_id=CONVERSATION))
    return RecordHistorySource(store, conversation_id=CONVERSATION)


def test_a_runtime_carries_the_store_its_turns_write_through() -> None:
    """The record is a field of the session, not a module function anything can reach around it."""
    store = InMemoryStepStore()

    assert a_runtime(store=store).store is store


def test_a_runtime_nobody_gave_a_record_keeps_its_transcript_in_memory() -> None:
    """A session built with no store still answers turns. Its history dies with the process."""
    assert isinstance(a_runtime().store, InMemoryStepStore)


async def test_a_compacted_run_offers_the_originals_and_the_summary_that_replaced_them() -> None:
    """Recall reads every step: the messages compaction dropped from the live context, then what it put there instead."""
    asked, answered = _asked("what is the ledger for?", minute=1), _answered("it holds the entries.", minute=2)
    summary, next_ask = _summary("the ledger question", minute=3), _asked("and who reconciles it?", minute=4)
    source = await _a_run_of([asked, answered], [summary, next_ask])

    history = await source.run_history(run_id=RUN)

    assert history == [asked, answered, summary, next_ask]


async def test_a_run_of_one_step_offers_that_steps_messages_once() -> None:
    """The whole read is that step: nothing is dropped for having no earlier step to be new against."""
    asked, answered = _asked("what is the ledger for?", minute=1), _answered("it holds the entries.", minute=2)
    source = await _a_run_of([asked, answered])

    history = await source.run_history(run_id=RUN)

    assert history == [asked, answered]


async def test_a_message_re_stamped_between_steps_is_still_the_one_message() -> None:
    """pydantic-ai re-stamps a message it re-sends. Recall counts the message, never the stamps it arrived under."""
    asked = _asked("what is the ledger for?", minute=1)
    re_sent = ModelRequest(
        parts=list(asked.parts), timestamp=AT, instructions="Answer in one sentence.", run_id=RUN, conversation_id=CONVERSATION
    )
    source = await _a_run_of([asked], [re_sent])

    history = await source.run_history(run_id=RUN)

    assert history == [asked], "one message, whatever a re-send stamped on it"
