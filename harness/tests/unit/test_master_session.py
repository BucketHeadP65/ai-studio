"""The master resolver mints once and answers the same id forever; the scoped forget clears, never re-identifies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.compaction import ContextUsage

from harness.domain.events import ConversationForgotten, SessionArchived, SessionDeleted, SessionStarted
from harness.session import Runtime, SessionPool
from tests.support.authoring import a_controller
from tests.support.runtime import a_runtime
from tests.support.serving import answered
from tests.support.state import a_session_log, a_state, a_transcript

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

    from harness.features.authoring.controller import AuthoringController

UNKNOWN = "00000000-0000-0000-0000-000000000000"


def _agent() -> Agent[Runtime, str]:
    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del messages, info
        return ModelResponse(parts=[TextPart("ok")])

    return Agent(FunctionModel(function=function), deps_type=Runtime)


def _pooled(tmp_path: Path) -> tuple[AuthoringController, SessionPool]:
    """The controller over a real pool, driven directly: every case here is about which session answers.

    The pool's sessions narrate into this suite's redirected checkout session log, which is the same
    timeline the resolver reads back to find a master it minted earlier.
    """
    pool = SessionPool(a_runtime(agent=_agent(), session_log=a_session_log(tmp_path), store=a_transcript(tmp_path), state=a_state(tmp_path)))
    return a_controller(pool, state=tmp_path, session_log=a_session_log(tmp_path)), pool


async def test_the_resolver_mints_once_and_is_idempotent(tmp_path: Path) -> None:
    controller, _pool = _pooled(tmp_path)

    first = answered(await controller.master_session())["session_id"]
    second = answered(await controller.master_session())["session_id"]

    assert first == second
    starts = [r.event for r in a_session_log(tmp_path).events() if isinstance(r.event, SessionStarted) and r.event.session_kind == "master"]
    assert len(starts) == 1


async def test_a_deleted_master_is_replaced_on_the_next_resolve(tmp_path: Path) -> None:
    """A deleted master is replaced by a fresh one on the next resolve.

    Journals the deletion directly rather than through `DELETE /api/sessions/{id}`: that route is the
    sessions slice's, and it reads a projection this case does not stand up.
    """
    controller, pool = _pooled(tmp_path)

    first = answered(await controller.master_session())["session_id"]
    runtime = await pool.get(first)
    runtime.session_log.record(SessionDeleted(session_id=first))
    second = answered(await controller.master_session())["session_id"]

    assert second != first


async def test_an_archived_master_is_replaced_on_the_next_resolve(tmp_path: Path) -> None:
    """An archived master is replaced by a fresh one on the next resolve, the deleted case's twin."""
    controller, pool = _pooled(tmp_path)

    first = answered(await controller.master_session())["session_id"]
    runtime = await pool.get(first)
    runtime.session_log.record(SessionArchived(session_id=first, archived=True))
    second = answered(await controller.master_session())["session_id"]

    assert second != first


async def test_the_scoped_forget_clears_history_and_keeps_the_identity(tmp_path: Path) -> None:
    controller, pool = _pooled(tmp_path)
    master_id = answered(await controller.master_session())["session_id"]
    runtime = await pool.get(master_id)
    runtime.conversation.history = [cast("Any", object())]
    runtime.context_usage = ContextUsage(used_tokens=8_000, window_tokens=128_000, resolved=True)

    response = await controller.forget_session(master_id)

    assert response.status_code == 200
    assert runtime.conversation.history == []
    assert runtime.context_usage is None, "a stale context reading must not survive a forget"
    assert runtime.session_id == master_id, "forget clears the conversation, never the session's identity"
    forgotten = [r.event for r in a_session_log(tmp_path).events() if isinstance(r.event, ConversationForgotten)]
    assert any(event.session_id == master_id for event in forgotten)


async def test_the_scoped_forget_answers_404_for_an_unknown_id(tmp_path: Path) -> None:
    controller, _pool = _pooled(tmp_path)

    response = await controller.forget_session(UNKNOWN)

    assert response.status_code == 404


async def test_the_resolver_holds_the_master_it_answers(tmp_path: Path) -> None:
    """A resolve loads the master into this process, so its context reading is answerable at once."""
    controller, pool = _pooled(tmp_path)

    first = answered(await controller.master_session())["session_id"]
    assert pool.drop(first)
    assert pool.context_of(first) is None
    again = answered(await controller.master_session())["session_id"]

    assert again == first
    assert pool.context_of(first) is not None
