"""The sandbox's dialect names the function list by the heading `CodeMode` renders for each of the harness's mounts.

`SandboxDiscipline` and its own tests live in core. This test stays here because it runs `CodeMode`, whose sandbox
core does not install.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from core.ai.retrieval.contracts import RUN_CODE
from core.ai.sandbox import SandboxDiscipline
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.toolsets import FunctionToolset
from pydantic_ai_harness import CodeMode


def _dialect() -> str:
    """The instructions `SandboxDiscipline` adds to the agent."""
    return SandboxDiscipline[Any]().get_instructions()(cast("Any", None))


_CATALOG_HEADING = "available inside the sandbox"
"""The words of `CodeMode`'s heading over the sandbox's functions, by which the dialect names that list."""

_CATALOG_BLOCK = '<context id="toolset:code_mode:catalog">'


async def _what_the_model_reads(*, dynamic_catalog: bool) -> tuple[str, str]:
    """`run_code`'s description and the catalog instruction block, as a model under a one-tool `CodeMode` receives them."""
    seen: dict[str, str] = {}

    async def ping() -> str:
        """Answer pong."""
        return "pong"

    def answer(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["description"] = next(tool.description or "" for tool in info.function_tools if tool.name == RUN_CODE)
        last = messages[-1]
        seen["instructions"] = (last.instructions or "") if isinstance(last, ModelRequest) else ""
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(FunctionModel(answer), toolsets=[FunctionToolset([ping])], capabilities=[CodeMode(dynamic_catalog=dynamic_catalog)])
    await agent.run("hi")
    instructions = seen["instructions"]
    catalog = instructions.split(_CATALOG_BLOCK, 1)[1].split("</context>", 1)[0] if _CATALOG_BLOCK in instructions else ""
    return seen["description"], catalog


@pytest.mark.parametrize("dynamic_catalog", [False, True])
async def test_codemode_heads_the_sandboxs_functions_with_the_words_the_dialect_names_them_by(dynamic_catalog: bool) -> None:
    """The dialect names the function list by `CodeMode`'s heading, so an upstream rewording of it must fail here first."""
    description, catalog = await _what_the_model_reads(dynamic_catalog=dynamic_catalog)

    assert _CATALOG_HEADING in _dialect()
    if dynamic_catalog:
        assert _CATALOG_HEADING in catalog, "the chat agent's mount carries the list in the catalog instruction"
    else:
        assert _CATALOG_HEADING in description, "a briefing's mount carries the list in `run_code`'s description"
