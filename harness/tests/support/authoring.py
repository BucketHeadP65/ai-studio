"""The authoring controller a test drives, and the chat turn behind it, over sessions that test holds open."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from harness.features.authoring.api_models import ChatRequest
from harness.features.authoring.controller import AuthoringController
from harness.features.authoring.service import ChatService, ensure_session_id
from harness.features.authoring.wire import messages_of, streaming_response
from harness.features.authoring.wiring import ArchivedRuns
from harness.settings import SelfAuthoringSettings, StateSettings
from tests.support.fakes.history import FakeConversationHistory
from tests.support.fakes.session_log import FakeSessionLog
from tests.support.serving import streamed
from tests.support.state import records_under

if TYPE_CHECKING:
    from pathlib import Path

    from harness.domain.ports import ConversationHistory, SessionEvents
    from harness.features.authoring.internal_contracts import Chat
    from harness.features.running.contracts import RunArchive
    from harness.session import Runtime, SessionPool, SessionRefresh


async def narrates_nowhere(runtime: Runtime) -> None:
    """A `SessionRefresh` that projects nothing: what a test with no projected rows to read hands in."""
    del runtime


def a_controller(
    sessions: SessionPool,
    *,
    state: Path,
    session_log: SessionEvents | None = None,
    refresh: SessionRefresh = narrates_nowhere,
    archive: RunArchive | None = None,
) -> AuthoringController:
    """The authoring controller over `sessions`, keeping its draft and its records under `state`.

    Args:
        sessions: The sessions this controller answers for, its default the process's own.
        state: The state home the kept draft is written under.
        session_log: Where the unscoped forget records, in memory unless the test wants its own.
        refresh: How a settled turn narrates itself, projecting nowhere unless the test watches it.
        archive: The ended runs an adaptation may cite, an empty archive under `state` unless the
            test puts a record in one of its own.

    Returns:
        A controller a test calls the way a request would.
    """
    return AuthoringController(
        sessions=sessions,
        refresh=refresh,
        session_log=session_log if session_log is not None else FakeSessionLog(),
        state=StateSettings(home=state),
        self_authoring=SelfAuthoringSettings(custom_root=state / "custom"),
        ended_runs=ArchivedRuns(archive if archive is not None else records_under(state).archive),
    )


def a_chat(sessions: SessionPool, *, history: ConversationHistory | None = None, refresh: SessionRefresh = narrates_nowhere) -> ChatService:
    """The chat service over `sessions`, reaching a record that holds nothing unless the test fills one.

    A turn on a session this process is holding is given that session's own live conversation, so a
    case that drives several turns in a row needs no record behind it at all. Only a turn on a
    session holding nothing reads the record, which is what a test scripting `history` is about.

    Args:
        sessions: The sessions this service answers turns in.
        history: What a turn on a session holding nothing is given, an empty record by default.
        refresh: How a settled turn narrates itself, projecting nowhere unless the test watches it.

    Returns:
        A service a test asks for turns the way the route does.
    """
    return ChatService(sessions, history if history is not None else FakeConversationHistory(), refresh)


def one_session(sessions: SessionPool) -> str:
    """The id of the pool's own default session, minted the way the first turn in it mints one."""
    return ensure_session_id(sessions.default)


async def a_turn(chat: Chat, session_id: str, body: dict[str, object]) -> list[dict[str, Any]]:
    """One turn in `session_id`, encoded on the wire the route encodes it on, as the chunks it wrote.

    `body` is the submit-message body a browser posts (`tests.support.streaming.submit` builds one).
    Drawing the response is what runs the turn, exactly as it is under the server.
    """
    request = ChatRequest.model_validate(body)
    turn = await chat.send_message(
        session_id, messages_of(request), context=request.context, tool_results=request.tool_results, design_only=request.design_only
    )
    return await streamed(streaming_response(request, None, turn))
