"""Page context on the chat wire: composed into the prompt, absent means byte-identical, renderers strip."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from harness.domain.page_context import PAGE_CONTEXT_OPEN, PageContext, compose_prompt
from harness.features.sessions.service import _first_user_text
from harness.session import Runtime, SessionPool
from tests.support.authoring import a_chat, a_controller, a_turn, one_session
from tests.support.runtime import a_runtime
from tests.support.serving import answered
from tests.support.streaming import streaming_function_model, submit

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

    from harness.features.authoring.service import ChatService

CONTEXT = {"url": "/workflows/r-1?run=r-1", "page": "run-inspector", "ids": {"run": "r-1"}, "tab": "", "trail": [], "extras": {}}


def _capturing_agent(seen: list[str], reply: str) -> Agent[Runtime, str]:
    def function(messages: Sequence[ModelMessage], info: AgentInfo) -> ModelResponse:
        del info
        request = messages[0]
        assert isinstance(request, ModelRequest)
        part = request.parts[0]
        assert isinstance(part, UserPromptPart)
        assert isinstance(part.content, str)
        seen.append(part.content)
        return ModelResponse(parts=[TextPart(reply)])

    return Agent(streaming_function_model(function), deps_type=Runtime)


def _talking_to(agent: Agent[Runtime, str], *, session_kind: str = "chat") -> tuple[ChatService, SessionPool, str]:
    """The chat service over one session of `session_kind`: every case here is about the prompt."""
    pool = SessionPool(a_runtime(agent=agent, session_kind=session_kind))
    return a_chat(pool), pool, one_session(pool)


async def test_a_turn_with_context_prepends_the_fence_to_the_prompt() -> None:
    seen: list[str] = []
    chat, _pool, session_id = _talking_to(_capturing_agent(seen, "ok"))

    await a_turn(chat, session_id, submit("what failed?", context=CONTEXT))

    assert seen[0].startswith(PAGE_CONTEXT_OPEN)
    assert "run-inspector" in seen[0]
    assert seen[0].endswith("what failed?")


async def test_a_master_session_gets_the_role_line() -> None:
    seen: list[str] = []
    chat, _pool, session_id = _talking_to(_capturing_agent(seen, "ok"), session_kind="master")

    await a_turn(chat, session_id, submit("hi", context=CONTEXT))

    assert "follow-me companion" in seen[0]


async def test_a_turn_without_context_is_the_bare_message() -> None:
    seen: list[str] = []
    chat, _pool, session_id = _talking_to(_capturing_agent(seen, "ok"))

    await a_turn(chat, session_id, submit("hello"))

    assert seen[0] == "hello"


async def test_the_conversation_panel_strips_the_fence(tmp_path: Path) -> None:
    seen: list[str] = []
    chat, pool, session_id = _talking_to(_capturing_agent(seen, "ok"))
    controller = a_controller(pool, state=tmp_path)

    await a_turn(chat, session_id, submit("what failed?", context=CONTEXT))
    panel = answered(await controller.conversation())

    yours = [turn["text"] for turn in panel["turns"] if turn["who"] == "you"]
    assert yours == ["what failed?"]


def test_the_title_excerpt_strips_the_fence() -> None:
    context = PageContext.model_validate(CONTEXT)
    prompt = compose_prompt("name me", context)
    request = ModelRequest(parts=[UserPromptPart(content=prompt)])
    assert _first_user_text([request]) == "name me"
