"""The session projection's fold, with no Postgres: `_project_rows` and `_fold_session_messages`.

`tests/integration/test_sessions_projection.py` proves the fold against a real database; this
file proves the fold itself -- what row a session log and a transcripts turn into, with a fake store
standing in so a turn still open, or a run whose snapshot cannot be read, exercises exactly the
path the live refresh takes without a container.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

from pydantic_ai.messages import ModelRequest, ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai.usage import RequestUsage
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord

from harness.adapters.models import response_cost
from harness.adapters.session_log import SessionLog
from harness.domain.events import ConversationForgotten, RunLaunched, SessionStarted, TurnAbandoned, TurnFailed, TurnSettled, TurnStarted
from harness.features.sessions import project_registry
from harness.features.sessions.infrastructure.projects_log import ProjectsLog
from harness.features.sessions.service import (
    _first_new_index,
    _fold_session_messages,
    _fold_turns_and_flags,
    _project_rows,
    project_id_of,
    sweep_projects,
)
from harness.workspace import open_workspace
from tests.support.fakes.pools import FakePool
from tests.support.fakes.step_store import FakeStepStore
from tests.support.state import a_session_log, a_transcript, recorded

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence
    from pathlib import Path

    from pydantic_ai_harness.step_persistence import StepStore

    from harness.domain.events import Recorded
    from harness.features.sessions.internal_contracts import ProjectionPool

SESSION_A = "11111111-1111-1111-1111-111111111111"
SESSION_B = "22222222-2222-2222-2222-222222222222"
PROJECT_ID = "test-project"


def _clock() -> datetime:
    return datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _message_index(message: tuple[object, ...]) -> int:
    """One projected message row's own position within its run, narrowed so a sort can order by it."""
    idx = message[1]
    assert isinstance(idx, int), "the fold writes each row's position as the integer it counted"
    return idx


def _snapshot(run_id: str, *, question: str, answer: str, input_tokens: int = 1, output_tokens: int = 1) -> ContinuableSnapshot:
    request = ModelRequest(parts=[UserPromptPart(content=question)])
    response = ModelResponse(
        parts=[TextPart(content=answer)], usage=RequestUsage(input_tokens=input_tokens, output_tokens=output_tokens)
    )
    return ContinuableSnapshot(run_id=run_id, step_index=0, messages=[request, response])


async def test_fold_session_messages_skips_a_run_whose_snapshot_read_fails(tmp_path: Path) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-good", started_seq=2))
    session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-broken", started_seq=4))
    events = session_log.events()

    store = FakeStepStore(snapshots={"run-good": _snapshot("run-good", question="hi", answer="hello")}, broken=frozenset({"run-broken"}))

    # `_fold_session_messages` wants the `_TurnUnderway` shapes `_fold_turns_and_flags` builds from the
    # same session log -- the pair `_project_rows` itself calls, exercised directly here.
    turns, _ = _fold_turns_and_flags(events)

    messages, excerpt = await _fold_session_messages(turns, store)

    assert excerpt == "hi", "the first user prompt still comes through -- it was run-good's, read before the broken one"
    run_ids_with_messages = {message[0] for message in messages}
    assert run_ids_with_messages == {"run-good"}, "run-broken's snapshot never loaded, so it contributes no message rows"
    broken_row = next(row for row in turns if row.run_id == "run-broken")
    assert (broken_row.input_tokens, broken_row.output_tokens) == (0, 0), "no usage was ever read off the run that failed"


async def test_a_second_turns_snapshot_that_dropped_two_old_messages_before_its_tail_still_yields_every_one_of_its_new_messages_and_none_of_the_old_ones(
    tmp_path: Path,
) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-1", started_seq=2))
    session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-2", started_seq=4))
    events = session_log.events()

    request_0 = ModelRequest(parts=[UserPromptPart(content="find the file")])
    tool_call = ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={}, tool_call_id="call-1")])
    retried = ModelRequest(parts=[RetryPromptPart(content="bad arguments", tool_name="read_file", tool_call_id="call-1")])
    tool_return = ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="file contents", tool_call_id="call-1")])
    response_0 = ModelResponse(parts=[TextPart(content="here is the file")])
    snapshot_1 = ContinuableSnapshot(run_id="run-1", step_index=0, messages=[request_0, tool_call, retried, tool_return, response_0])

    request_1 = ModelRequest(parts=[UserPromptPart(content="now summarize it")])
    tool_call_2 = ModelResponse(parts=[ToolCallPart(tool_name="summarize", args={}, tool_call_id="call-2")])
    tool_return_2 = ModelRequest(parts=[ToolReturnPart(tool_name="summarize", content="a summary", tool_call_id="call-2")])
    response_1 = ModelResponse(parts=[TextPart(content="it is about X")])
    # `tool_call` and `retried` (two of turn 1's own messages) are gone from turn 2's snapshot -- a
    # compaction pass dropped them before the tail -- while `request_0`, `tool_return`, and
    # `response_0` (turn 1's own last message) come through unchanged.
    snapshot_2 = ContinuableSnapshot(
        run_id="run-2", step_index=0, messages=[request_0, tool_return, response_0, request_1, tool_call_2, tool_return_2, response_1]
    )

    store = FakeStepStore(snapshots={"run-1": snapshot_1, "run-2": snapshot_2})
    turns, _ = _fold_turns_and_flags(events)

    messages, _excerpt = await _fold_session_messages(turns, store)

    run_2_messages = sorted((message for message in messages if message[0] == "run-2"), key=_message_index)
    assert len(run_2_messages) == 4, "all four of turn 2's own new messages are projected, not the two old ones still ahead of them"
    assert [message[2] for message in run_2_messages] == ["request", "response", "request", "response"]
    texts = {message[4] for message in run_2_messages}
    assert texts == {"now summarize it", "", "a summary", "it is about X"}
    assert "find the file" not in texts
    assert "here is the file" not in texts


async def test_a_second_turns_snapshot_shorter_than_the_first_turns_still_yields_its_new_messages(tmp_path: Path) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-1", started_seq=2))
    session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-2", started_seq=4))
    events = session_log.events()

    request_0 = ModelRequest(parts=[UserPromptPart(content="what does file a say")])
    tool_call = ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={}, tool_call_id="call-1")])
    tool_return = ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="a very long file body", tool_call_id="call-1")])
    response_0 = ModelResponse(parts=[TextPart(content="file a says hello")])
    snapshot_1 = ContinuableSnapshot(run_id="run-1", step_index=0, messages=[request_0, tool_call, tool_return, response_0])

    # A summarizing compaction pass replaced turn 1's whole history with one shorter message before
    # turn 2 ran -- `response_0`, turn 1's own last message, is gone, so nothing in turn 2's snapshot
    # matches it.
    summary = ModelRequest(parts=[UserPromptPart(content="Summary of the conversation so far: read file a, it says hello.")])
    request_1 = ModelRequest(parts=[UserPromptPart(content="now check file b")])
    response_1 = ModelResponse(parts=[TextPart(content="file b says goodbye")])
    snapshot_2 = ContinuableSnapshot(run_id="run-2", step_index=0, messages=[summary, request_1, response_1])

    store = FakeStepStore(snapshots={"run-1": snapshot_1, "run-2": snapshot_2})
    turns, _ = _fold_turns_and_flags(events)

    messages, _excerpt = await _fold_session_messages(turns, store)

    run_2_messages = [message for message in messages if message[0] == "run-2"]
    assert len(run_2_messages) == 3, "with no message shared between the two snapshots, the whole shorter snapshot is projected as new"
    assert len(snapshot_2.messages) < len(snapshot_1.messages), "the fixture must actually shrink, or this proves nothing"


def test_the_previous_turns_last_message_restamped_with_a_different_timestamp_run_id_and_instructions_but_equal_parts_is_still_recognized_as_shared() -> (
    None
):
    # The part itself (`UserPromptPart`, its own `timestamp` included) is what pydantic-ai carries
    # over untouched between snapshots -- only the containing `ModelRequest`'s own `timestamp`,
    # `run_id`, and rendered `instructions` are re-stamped when it re-sends a historical message.
    prompt = UserPromptPart(content="please continue", timestamp=_clock())
    original_last = ModelRequest(parts=[prompt], timestamp=_clock(), instructions="you are a helpful assistant", run_id="run-1")
    restamped = ModelRequest(
        parts=[prompt], timestamp=_clock() + timedelta(minutes=5), instructions="you are a helpful assistant, updated", run_id="run-2"
    )
    new_response = ModelResponse(parts=[TextPart(content="continuing")], usage=RequestUsage(input_tokens=1, output_tokens=1))

    index = _first_new_index([restamped, new_response], original_last)

    assert index == 1, "the restamped copy of the same request is still recognized as the shared message despite differing metadata"


async def test_a_first_turn_yields_every_message(tmp_path: Path) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-first", started_seq=2))
    events = session_log.events()

    store = FakeStepStore(snapshots={"run-first": _snapshot("run-first", question="hi", answer="hello")})
    turns, _ = _fold_turns_and_flags(events)

    messages, excerpt = await _fold_session_messages(turns, store)

    assert len(messages) == 2, "with no previous turn, every message in the snapshot is new"
    assert excerpt == "hi"


async def test_project_rows_still_writes_the_session_row_when_one_run_cannot_be_read(tmp_path: Path) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-good", started_seq=2))
    session_log.record(TurnStarted(entry="spa", chars=5, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(TurnSettled(run_id="run-broken", started_seq=4))
    events = session_log.events()

    store = FakeStepStore(snapshots={"run-good": _snapshot("run-good", question="hi", answer="hello")}, broken=frozenset({"run-broken"}))

    sessions, turns, _messages, _flags = await _project_rows(events, store, project_id=PROJECT_ID, only_session_id=SESSION_A)

    assert len(sessions) == 1, "the session's own row must exist even though one of its two runs could not be read"
    session_row = sessions[0]
    assert session_row[0] == SESSION_A
    assert session_row[6] == 2, "both turns are still counted -- turn_count comes from the session log, not the store"
    assert len(turns) == 2
    assert {row[6] for row in turns} == {"settled"}, "outcome is a session log fact; a store read failure never demotes it"


async def test_project_rows_produces_a_row_for_a_session_with_an_open_turn_and_a_live_run(tmp_path: Path) -> None:
    """`session_started`, `turn_started`, a run launched inside the turn, no settle yet.

    The exact shape a chat turn steps while its `run_workflow` tool call is still waiting at a gate:
    the live refresh a settle would normally schedule has not happened, so a caller asking for this
    session's row right now must still get one -- title from the one user prompt on the session log, the
    open turn counted, no usage yet since no run ever wrote a message here.
    """
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    session_log.record(TurnStarted(entry="spa", chars=12, prompt_excerpt=None, session_id=SESSION_A))
    session_log.record(RunLaunched(workflow_id="wf.gate-check.v1", run_id="run-gate-check"))
    events = session_log.events()

    store = FakeStepStore()
    sessions, turns, messages, _flags = await _project_rows(events, store, project_id=PROJECT_ID, only_session_id=SESSION_A)

    assert len(sessions) == 1, "an open turn still yields a session row -- projection cannot wait for a settle that has not happened"
    session_row = sessions[0]
    assert session_row[0] == SESSION_A
    assert session_row[6] == 1, "the open turn is counted"
    assert len(turns) == 1
    assert turns[0][6] == "open", "no terminal event has closed it yet"
    assert messages == [], "no run id exists on an open turn, so there is nothing to read from the store"


async def test_one_sessions_broken_run_does_not_stop_another_session_from_projecting(tmp_path: Path) -> None:
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    started_a = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-broken", started_seq=started_a.seq))
    session_log.record(SessionStarted(session_id=SESSION_B, session_kind="chat"))
    started_b = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_B, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-clean", started_seq=started_b.seq))
    events = session_log.events()

    store = FakeStepStore(snapshots={"run-clean": _snapshot("run-clean", question="hey", answer="yo")}, broken=frozenset({"run-broken"}))

    # `only_session_id=None` is the all-sessions pass `rebuild` (and `sessions rebuild`) run.
    sessions, _turns, messages, _flags = await _project_rows(events, store, project_id=PROJECT_ID, only_session_id=None)

    ids = {row[0] for row in sessions}
    assert ids == {SESSION_A, SESSION_B}, "session A's unreadable run must not keep session B out of the same pass"
    session_b_row = next(row for row in sessions if row[0] == SESSION_B)
    assert session_b_row[6] == 1
    assert {message[0] for message in messages} == {"run-clean"}


async def test_forget_watermark_scopes_turn_count_usage_and_cost_to_the_conversation_since_then(tmp_path: Path) -> None:
    """Two turns, a Forget, then one more turn: only the turn after the watermark counts.

    Mirrors the owner's Clear gesture -- the projected row, its turns, and its messages describe
    the conversation since the last Forget, while the session log and the transcripts still hold the
    two runs before it untouched.
    """
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    started_1 = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-before-1", started_seq=started_1.seq))
    started_2 = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-before-2", started_seq=started_2.seq))
    session_log.record(ConversationForgotten(session_id=SESSION_A))
    started_3 = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-after", started_seq=started_3.seq))
    events = session_log.events()

    after_response = ModelResponse(
        parts=[TextPart(content="reply three")], usage=RequestUsage(input_tokens=3, output_tokens=2), model_name="deepseek-v4-pro"
    )
    after_snapshot = ContinuableSnapshot(
        run_id="run-after", step_index=0, messages=[ModelRequest(parts=[UserPromptPart(content="third")]), after_response]
    )
    store = FakeStepStore(
        snapshots={
            "run-before-1": _snapshot("run-before-1", question="first", answer="reply one", input_tokens=10, output_tokens=5),
            "run-before-2": _snapshot("run-before-2", question="second", answer="reply two", input_tokens=20, output_tokens=8),
            "run-after": after_snapshot,
        }
    )

    sessions, turns, messages, _flags = await _project_rows(events, store, project_id=PROJECT_ID, only_session_id=SESSION_A)

    assert len(sessions) == 1
    session_row = sessions[0]
    assert session_row[6] == 1, "turn_count counts only the turn started after the Forget"
    assert (session_row[7], session_row[8]) == (3, 2), "usage sums only the turn after the Forget, not the two before it"
    expected_cost = response_cost(after_response)
    assert expected_cost > 0, "the fixture must price to something non-zero, or this assertion proves nothing"
    assert session_row[11] == expected_cost, "cost sums only the turn after the Forget"
    assert session_row[2] == "third", "a derived title follows the conversation since the Forget"
    assert len(turns) == 1, "the detail plugin's own turns list holds only the turn after the Forget"
    assert {message[0] for message in messages} == {"run-after"}, "the transcript plugin's own rows hold only the post-Forget run"


async def test_forget_watermark_ignores_a_forget_stamped_for_a_different_session(tmp_path: Path) -> None:
    """A Forget names its own session; it must never watermark a session it was not stamped for."""
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    started_a = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnSettled(run_id="run-a", started_seq=started_a.seq))
    session_log.record(SessionStarted(session_id=SESSION_B, session_kind="chat"))
    session_log.record(ConversationForgotten(session_id=SESSION_B))
    events = session_log.events()

    store = FakeStepStore(snapshots={"run-a": _snapshot("run-a", question="hi", answer="hello")})

    sessions, _turns, _messages, _flags = await _project_rows(events, store, project_id=PROJECT_ID, only_session_id=SESSION_A)

    assert len(sessions) == 1
    assert sessions[0][6] == 1, "session B's own Forget must not clear session A's turn"


SESSION_CHECKOUT = "33333333-3333-3333-3333-333333333333"
SESSION_C = "44444444-4444-4444-4444-444444444444"


class _RefreshSpy:
    """A `refresh_live` stand-in: records which session, off which root, each call named."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []
        self.pools: list[ProjectionPool] = []

    async def __call__(
        self, pool: ProjectionPool, events: Sequence[Recorded], store: StepStore, *, project_root: Path, only_session_id: str | None = None
    ) -> None:
        del events, store
        self.pools.append(pool)
        self.calls.append((str(project_root), only_session_id))


@pytest.fixture(autouse=True)
def _fresh_registry(tmp_path: Path) -> Iterator[None]:
    """Every test gets its own registry file -- never the real checkout's."""
    project_registry.install(ProjectsLog(tmp_path / "registry.jsonl"))
    yield
    project_registry._REGISTRY = None  # the same reset the integration suite uses between tests


async def test_sweep_projects_visits_every_session_in_every_registered_project(tmp_path: Path) -> None:
    """The boot sweep must cover exactly what `sessions rebuild` covers: no session left un-projected.

    One session lives on the checkout's own state home; two more live on a separately registered
    project's own workspace session log. `sweep_projects` must visit all three -- a boot that only refreshed the checkout's
    session, or only the registered project's, would reproduce the dropped-rows symptom this
    guards against.
    """
    checkout = tmp_path / "harness-checkout"
    other_root = tmp_path / "other-project"
    other_root.mkdir()
    checkout_log = a_session_log(tmp_path / "checkout-state")
    checkout_store = a_transcript(tmp_path / "checkout-state")

    checkout_log.record(SessionStarted(session_id=SESSION_CHECKOUT, session_kind="chat"))
    checkout_log.record(TurnStarted(entry="spa", chars=3, prompt_excerpt=None, session_id=SESSION_CHECKOUT))
    checkout_log.record(TurnSettled(run_id="run-checkout", started_seq=2))

    workspace = open_workspace(other_root)
    workspace.session_log.record(SessionStarted(session_id=SESSION_B, session_kind="chat"))
    workspace.session_log.record(TurnStarted(entry="spa", chars=3, session_id=SESSION_B, prompt_excerpt=None))
    workspace.session_log.record(TurnSettled(run_id="run-b", started_seq=2))
    workspace.session_log.record(SessionStarted(session_id=SESSION_C, session_kind="chat"))
    workspace.session_log.record(TurnStarted(entry="spa", chars=3, session_id=SESSION_C, prompt_excerpt=None))
    workspace.session_log.record(TurnSettled(run_id="run-c", started_seq=5))

    project_registry.boot(checkout)
    project_registry.register(other_root, project_id=project_id_of(other_root))

    spy = _RefreshSpy()
    pool = FakePool()
    await sweep_projects(pool, checkout, session_log=checkout_log, store=checkout_store, refresh=spy)

    assert spy.pools == [pool, pool, pool], "every refresh writes through the one pool the sweep was given"
    swept_session_ids = {session_id for _root, session_id in spy.calls}
    assert swept_session_ids == {SESSION_CHECKOUT, SESSION_B, SESSION_C}, "a boot must never leave fewer rows than the session logs hold"

    checkout_call = next(call for call in spy.calls if call[1] == SESSION_CHECKOUT)
    assert checkout_call[0] == str(checkout.resolve()), "the checkout's own session is swept off the checkout's own root, not a workspace"
    other_roots = {root for root, session_id in spy.calls if session_id in {SESSION_B, SESSION_C}}
    assert other_roots == {str(other_root.resolve())}


def _incrementing_clock() -> Callable[[], datetime]:
    """A clock that advances one minute per call, so distinct sessions get distinct timestamps."""
    state = {"n": 0}

    def clock() -> datetime:
        state["n"] += 1
        return datetime(2026, 8, 25, 8, 0, tzinfo=UTC) + timedelta(minutes=state["n"])

    return clock


async def test_last_activity_at_is_each_sessions_own_last_turn_not_the_sweeps_moment(tmp_path: Path) -> None:
    """Every one of these three sessions answers through the pool -- none ever positionally current.

    Each session's turns are stamped with their own `session_id` directly, and none is ever named
    by a `session_started` that would let the positional walk close its span --
    ordinary for a server answering several sessions at once, and why every one of them has an
    "open" span in `session_spans`. The old fallback stamped every open session with the session log's
    one newest line (the sweep's own moment); the fix must give each its own newest turn instead.
    """
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_incrementing_clock(), topics=None)
    started_a = recorded(session_log, TurnStarted(entry="spa", chars=5, session_id=SESSION_A, prompt_excerpt=None))
    settled_a = recorded(session_log, TurnSettled(run_id="run-a", started_seq=started_a.seq))
    started_b = recorded(session_log, TurnStarted(entry="spa", chars=4, session_id=SESSION_B, prompt_excerpt=None))
    settled_b = recorded(session_log, TurnSettled(run_id="run-b", started_seq=started_b.seq))
    started_c = recorded(session_log, TurnStarted(entry="spa", chars=3, session_id=SESSION_CHECKOUT, prompt_excerpt=None))
    settled_c = recorded(session_log, TurnSettled(run_id="run-c", started_seq=started_c.seq))
    events = session_log.events()

    store = FakeStepStore()
    sessions, _turns, _messages, _flags = await _project_rows(events, store, project_id=PROJECT_ID, only_session_id=None)

    by_id = {row[0]: row for row in sessions}
    assert by_id[SESSION_A][4] == settled_a.at, "A's last activity is A's own settle"
    assert by_id[SESSION_B][4] == settled_b.at, "B's last activity is B's own settle"
    assert by_id[SESSION_CHECKOUT][4] == settled_c.at, "the last-opened session's own settle, incidentally also the session log's newest line"
    assert len({by_id[SESSION_A][4], by_id[SESSION_B][4], by_id[SESSION_CHECKOUT][4]}) == 3, (
        "the bug stamped every open session with the sweep's one newest timestamp -- these three must differ"
    )


async def test_title_falls_back_to_a_failed_turns_own_prompt_excerpt_when_no_run_ever_settled(tmp_path: Path) -> None:
    """A failed first turn still shows the person's words, not a short-id fallback.

    A session whose first turn crashes before any run writes a snapshot has no settled excerpt to
    read a title from; `turn_failed`'s own `prompt_excerpt` is the next best source. The turn
    itself is still counted, since it exists on the session log whatever its outcome.
    """
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    started = recorded(session_log, TurnStarted(entry="spa", chars=67, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnFailed(error="Exceeded maximum output retries (1)", started_seq=started.seq, prompt_excerpt="write me a summary"))
    events = session_log.events()

    store = FakeStepStore()
    sessions, turns, _messages, _flags = await _project_rows(events, store, project_id=PROJECT_ID, only_session_id=SESSION_A)

    assert len(sessions) == 1
    session_row = sessions[0]
    assert session_row[2] == "write me a summary", "the title reads the crashed turn's own prompt excerpt, not the short-id fallback"
    assert session_row[6] == 1, "the failed turn is still counted"
    assert len(turns) == 1
    assert turns[0][6] == "failed"


async def test_a_failed_turns_run_is_claimed_back_from_the_step_log(tmp_path: Path) -> None:
    """A turn that closed without settling still shows what happened in it.

    The blank-pane bug: only a settle records a run id, so a session whose every turn failed or
    was abandoned projected turn rows joining no messages -- the person watched a whole
    conversation happen and the pane rendered empty, listed under a bare hash title. The transcript
    kept the run all along; the fold now matches it back by the turn's own time window.
    """
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    started = recorded(session_log, TurnStarted(entry="spa", chars=20, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnFailed(error="The next request would exceed the request_limit of 32.", started_seq=started.seq, prompt_excerpt=None))
    abandoned = recorded(session_log, TurnStarted(entry="spa", chars=9, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnAbandoned(started_seq=abandoned.seq))
    events = session_log.events()

    first = _snapshot("run-budget", question="adapt the workflow", answer="working on it")
    followup = _snapshot("run-cut", question="you are looping", answer="still at it")
    # Snapshots are cumulative within one session's lineage: the later run carries the earlier
    # run's messages, and the fold projects only its new tail.
    cut = ContinuableSnapshot(run_id="run-cut", step_index=0, messages=[*first.messages, *followup.messages])
    store = FakeStepStore(
        snapshots={"run-budget": first, "run-cut": cut},
        runs=[
            RunRecord(run_id="run-budget", conversation_id=SESSION_A, started_at=_clock()),
            RunRecord(run_id="run-cut", conversation_id=SESSION_A, started_at=_clock()),
            RunRecord(run_id="run-other", conversation_id=SESSION_B, started_at=_clock()),
        ],
    )
    sessions, turns, messages, _flags = await _project_rows(events, store, project_id=PROJECT_ID, only_session_id=SESSION_A)

    assert [row[2] for row in turns] == ["run-budget", "run-cut"], "each closed-without-settle turn claims its own run, in order"
    assert [row[6] for row in turns] == ["failed", "abandoned"], "the outcome still says how the turn ended"
    assert {message[0] for message in messages} == {"run-budget", "run-cut"}, "the transcript join now has rows to find"
    assert sessions[0][2] == "adapt the workflow", "the title reads the run's own first prompt, not the short-id fallback"


async def test_a_turn_that_never_registered_a_run_still_projects_with_none(tmp_path: Path) -> None:
    """A turn that died before its first request has no run to claim, and another conversation's run is never taken."""
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    started = recorded(session_log, TurnStarted(entry="spa", chars=20, session_id=SESSION_A, prompt_excerpt=None))
    session_log.record(TurnFailed(error="boom before any request", started_seq=started.seq, prompt_excerpt=None))
    events = session_log.events()

    store = FakeStepStore(runs=[RunRecord(run_id="run-other", conversation_id=SESSION_B, started_at=_clock())])
    sessions, turns, messages, _flags = await _project_rows(events, store, project_id=PROJECT_ID, only_session_id=SESSION_A)

    assert turns[0][2] is None, "nothing to claim: the other conversation's run stays its own"
    assert messages == []
    assert sessions[0][2] == f"session {SESSION_A[:8]}", "no snapshot and no excerpt steps the short-id fallback"


async def test_a_turn_cut_off_before_any_run_still_titles_the_session_with_its_own_words(tmp_path: Path) -> None:
    """The opening event carries the prompt's first words, so a session of only dead turns is not a bare hash.

    Lived: a person re-pasted a whole adaptation order into a fresh session, the turn was cut off
    before the first model request, and the session sat in the list as `session af822c11` forever --
    the session log had kept only the prompt's length.
    """
    session_log = SessionLog(tmp_path / "session-log.jsonl", clock=_clock, topics=None)
    session_log.record(SessionStarted(session_id=SESSION_A, session_kind="chat"))
    started = recorded(session_log, TurnStarted(entry="spa", chars=1731, session_id=SESSION_A, prompt_excerpt="adapt wf.research-compound"))
    session_log.record(TurnAbandoned(started_seq=started.seq))
    events = session_log.events()

    sessions, _turns, _messages, _flags = await _project_rows(events, FakeStepStore(), project_id=PROJECT_ID, only_session_id=SESSION_A)

    assert sessions[0][2] == "adapt wf.research-compound", "the person's own words title the session, however the turn ended"
