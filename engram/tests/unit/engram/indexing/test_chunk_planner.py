"""Unit tests for the pydantic-ai chunk planner, driven by scripted models with no network call."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.settings import AzureOpenAiSettings
from pydantic_ai import ModelResponse, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.profiles import ModelProfile

from engram.config import IngestionSettings
from engram.indexing.contracts import ChunkPlanner
from engram.indexing.infrastructure.chunk_planner import PydanticAiChunkPlanner

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

models_block = pytest.importorskip("pydantic_ai.models")
models_block.ALLOW_MODEL_REQUESTS = False

_PARAGRAPHS = ["The ferry left at noon.", "It reached the island by evening.", "The market opened the next morning."]


def _answering(starts: list[int], captured: dict[str, object]) -> FunctionModel:
    def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        captured["user_text"] = "\n".join(
            part.content for message in messages for part in message.parts if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )
        captured["instructions"] = info.instructions
        captured["thinking"] = info.model_request_parameters.thinking
        return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args={"starts": starts})])

    return FunctionModel(script, profile=ModelProfile(supports_thinking=True))


def test_satisfies_the_chunk_planner_port() -> None:
    assert isinstance(PydanticAiChunkPlanner(TestModel(), model_id="example-deployment"), ChunkPlanner)


def test_the_identity_names_the_deployment_the_prompt_version_and_the_thinking_level() -> None:
    planner = PydanticAiChunkPlanner(TestModel(model_name="other"), model_id="example-deployment")

    assert planner.identity == "example-deployment/prompt-1/thinking-low"


def test_the_identity_of_a_deployment_is_known_with_no_model() -> None:
    assert PydanticAiChunkPlanner.identity_for("example-deployment") == PydanticAiChunkPlanner(TestModel(), model_id="example-deployment").identity


async def test_the_answer_is_the_paragraph_numbers_of_the_structured_output() -> None:
    planner = PydanticAiChunkPlanner(_answering([3], {}), model_id="example-deployment")

    assert list(await planner.chunk_starts(_PARAGRAPHS)) == [3]


async def test_the_paragraphs_are_shown_numbered_from_one_in_reading_order() -> None:
    captured: dict[str, object] = {}
    planner = PydanticAiChunkPlanner(_answering([3], captured), model_id="example-deployment")

    await planner.chunk_starts(_PARAGRAPHS)

    assert captured["user_text"] == "[1] The ferry left at noon.\n\n[2] It reached the island by evening.\n\n[3] The market opened the next morning."


async def test_the_instructions_ask_for_topic_changes_as_paragraph_numbers_only() -> None:
    captured: dict[str, object] = {}
    planner = PydanticAiChunkPlanner(_answering([], captured), model_id="example-deployment")

    await planner.chunk_starts(_PARAGRAPHS)

    instructions = captured["instructions"]
    assert isinstance(instructions, str)
    assert "topic" in instructions
    assert "paragraph numbers only" in instructions
    assert "ferry" not in instructions


async def test_the_model_runs_at_a_low_thinking_level() -> None:
    captured: dict[str, object] = {}
    planner = PydanticAiChunkPlanner(_answering([], captured), model_id="example-deployment")

    await planner.chunk_starts(_PARAGRAPHS)

    assert captured["thinking"] == "low"


def test_from_settings_builds_over_the_named_deployment() -> None:
    planner = PydanticAiChunkPlanner.from_settings(
        AzureOpenAiSettings(endpoint="https://example.openai.azure.com"), IngestionSettings(chunk_planner_model="test-model")
    )

    assert planner.identity == "test-model/prompt-1/thinking-low"


async def test_the_test_model_shortcut_answers_without_network() -> None:
    planner = PydanticAiChunkPlanner.from_settings(
        AzureOpenAiSettings(endpoint="https://example.openai.azure.com"), IngestionSettings(chunk_planner_model="test-model")
    )

    answer = await planner.chunk_starts(_PARAGRAPHS)

    assert all(isinstance(number, int) for number in answer)
