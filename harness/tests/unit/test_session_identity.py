"""Session identity at runtime: minting on the first turn, the Forget reset, session log-led resume.

Eight behaviors under test. First: `ensure_session_id` mints exactly once per session and the
session log hears it exactly once, however many turns ask. Second: the unscoped conversation route
never mints -- reading before a conversation exists answers a null id and records nothing. Third:
`POST /conversation` mints the session, and the read that follows names it. Fourth: a second
`POST /conversation` answers the same id and records no second `SessionStarted`. Fifth: the Forget
route clears the runtime's session id along with the conversation, so the next turn mints a fresh
one. Sixth: after Forget, the read answers null again. Seventh: `resume` given a session
log-remembered `run_id` continues that run directly, never consulting the Forget watermark -- the
watermark walk is a fallback for when there is no remembered run. Eighth: that walk reads only the
runs this home's own session log credits to it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast

from core.ai.workflows.contracts import StepRegistry
from pydantic_ai.messages import ModelRequest, ModelResponse, ToolCallPart, ToolReturnPart, UserPromptPart
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, InMemoryStepStore, RunRecord, is_provider_valid

from harness import session as session_state
from harness.domain.events import SessionStarted
from harness.features.authoring.service import ensure_session_id
from harness.session import Runtime, SessionPool
from tests.support.authoring import a_controller
from tests.support.runtime import a_runtime
from tests.support.serving import answered
from tests.support.state import a_session_log, a_state, a_transcript

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic_ai_harness.step_persistence import StepStore


async def _seed(store: StepStore, *, run_id: str, conversation_id: str, said: str) -> None:
    """One readable run of one conversation."""
    await store.register_run(RunRecord(run_id=run_id, conversation_id=conversation_id))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id=run_id,
            step_index=0,
            messages=[ModelRequest(parts=[UserPromptPart(content=said)])],
            conversation_id=conversation_id,
        )
    )


def _bare_session(home: Path) -> Runtime:
    """A `Runtime` with no real agent or store -- fine for tests that never run a turn."""
    return Runtime(
        registry=StepRegistry(),
        library=cast("Any", None),
        agent=cast("Any", None),
        session_log=a_session_log(home),
        store=a_transcript(home),
        state=a_state(home),
    )


def test_ensure_session_id_mints_once_and_returns_the_same_id(tmp_path: Path) -> None:
    session = _bare_session(tmp_path)

    first = ensure_session_id(session)
    second = ensure_session_id(session)

    assert first == second
    assert session.session_id == first
    started = [record.event for record in session.session_log.events() if isinstance(record.event, SessionStarted)]
    assert len(started) == 1, "a second call must not mint a second session"
    assert started[0].session_id == first


async def test_conversation_answers_null_before_a_session_starts(tmp_path: Path) -> None:
    session = _bare_session(tmp_path)
    controller = a_controller(SessionPool(session), state=tmp_path)

    panel = answered(await controller.conversation())

    assert panel["session_id"] is None
    started = [record.event for record in session.session_log.events() if isinstance(record.event, SessionStarted)]
    assert started == [], "a read must never mint a session"


async def test_the_unscoped_conversation_names_the_session_the_next_turn_would_use(tmp_path: Path) -> None:
    session = _bare_session(tmp_path)
    controller = a_controller(SessionPool(session), state=tmp_path)

    minted = answered(await controller.start_conversation())
    panel = answered(await controller.conversation())

    assert panel["session_id"] == minted["session_id"]
    assert panel["session_id"] == session.session_id
    started = [record.event for record in session.session_log.events() if isinstance(record.event, SessionStarted)]
    assert len(started) == 1


async def test_start_conversation_twice_mints_once(tmp_path: Path) -> None:
    session = _bare_session(tmp_path)
    controller = a_controller(SessionPool(session), state=tmp_path)

    first = answered(await controller.start_conversation())
    second = answered(await controller.start_conversation())

    assert first["session_id"] == second["session_id"]
    started = [record.event for record in session.session_log.events() if isinstance(record.event, SessionStarted)]
    assert len(started) == 1


async def test_forget_resets_the_runtime_session_id(tmp_path: Path) -> None:
    session = _bare_session(tmp_path)
    session.session_id = "an-existing-session"
    controller = a_controller(SessionPool(session), state=tmp_path)

    response = await controller.forget()

    assert response.status_code == 200
    assert session.session_id is None, "forgetting ends the session; the next turn mints a fresh one"


async def test_forget_then_conversation_answers_null_again(tmp_path: Path) -> None:
    session = _bare_session(tmp_path)
    controller = a_controller(SessionPool(session), state=tmp_path)
    await controller.start_conversation()

    await controller.forget()
    panel = answered(await controller.conversation())

    assert panel["session_id"] is None


async def test_resume_prefers_the_given_run_id_over_the_watermark_walk(tmp_path: Path) -> None:
    store = InMemoryStepStore()
    now = datetime.now(tz=UTC)
    await store.register_run(RunRecord(run_id="r1", started_at=now - timedelta(minutes=10)))
    await store.register_run(RunRecord(run_id="r2", started_at=now - timedelta(minutes=5)))
    await store.save_snapshot(
        ContinuableSnapshot(run_id="r1", step_index=0, messages=[ModelRequest(parts=[UserPromptPart(content="first run")])])
    )
    await store.save_snapshot(
        ContinuableSnapshot(run_id="r2", step_index=0, messages=[ModelRequest(parts=[UserPromptPart(content="second run")])])
    )
    session = _bare_session(tmp_path)
    # A watermark after both runs: the newest-after-watermark walk would find nothing, so a
    # successful resume here can only have come from the direct `run_id` path.
    watermark = now + timedelta(hours=1)

    resumed = await session_state.resume(session, store, run_id="r2", forgotten_after=watermark)

    assert resumed == "r2"
    part = session.conversation.history[-1].parts[0]
    assert isinstance(part, UserPromptPart)
    assert part.content == "second run"


async def test_resume_closes_an_open_call_left_by_an_interrupted_snapshot(tmp_path: Path) -> None:
    """A boot cannot know why a run stopped, so it rescues with the cut-off sentence.

    Only an `interrupted` snapshot exists for this run -- the shape a crash mid-tool-cycle
    steps -- so a boot that skipped it (the default `latest_snapshot` read) would resume this
    session holding nothing at all.
    """
    store = InMemoryStepStore()
    await store.register_run(RunRecord(run_id="r1"))
    await store.save_snapshot(
        ContinuableSnapshot(
            run_id="r1",
            step_index=0,
            messages=[
                ModelRequest(parts=[UserPromptPart(content="do it")]),
                ModelResponse(parts=[ToolCallPart(tool_name="poke", args={}, tool_call_id="call-1")]),
            ],
            state="interrupted",
        )
    )
    session = _bare_session(tmp_path)

    resumed = await session_state.resume(session, store, run_id="r1", forgotten_after=None)

    assert resumed == "r1"
    closing = session.conversation.history[-1]
    assert isinstance(closing, ModelRequest)
    part = closing.parts[0]
    assert isinstance(part, ToolReturnPart)
    assert part.tool_call_id == "call-1"
    content = part.content
    assert isinstance(content, str)
    assert "the turn was cut off here" in content
    assert is_provider_valid(session.conversation.history)


async def test_resume_never_reaches_a_run_this_home_did_not_log() -> None:
    """One record holds every session's runs, so boot resumes only what this timeline names."""
    store = InMemoryStepStore()
    await _seed(store, run_id="mine", conversation_id="s-1", said="my own last words")
    await _seed(store, run_id="anothers", conversation_id="s-2", said="another session's words")
    session = a_runtime(store=store)

    resumed = await session_state.resume(session, store, run_ids=("mine",))

    assert resumed == "mine"
    assert "another session's words" not in str(session.conversation.history)
