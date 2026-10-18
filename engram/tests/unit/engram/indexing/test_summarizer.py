r"""Unit tests for the production `Summarizer` adapter (`src/engram/indexing/infrastructure/summarizer.py`).

Mirrors `tests/unit/evals/test_proposer.py` / `tests/unit/ask/test_judge.py`: the model is a
constructor dependency, scripted per test via `TestModel` (output-shape assertions) or a
`FunctionModel` (prompt-assembly assertions, its non-streaming callback captures `(messages,
info)`) — no network, no monkeypatching. Unlike those two, `PydanticAiSummarizer.summarize` has no
structured output tool: it returns pydantic-ai's default plain-text output, so the `FunctionModel`
script returns a bare `TextPart`, not a `ToolCallPart`.

Pinned:

* `summarize(title=..., body=...)` returns the model's emitted text verbatim;
* the assembled user prompt carries both the title and the body;
* the summarizer's own instructions (not the per-call title/body) are the durable `Agent`
  instructions, not folded into the user prompt;
* `model_id` is the deployment name passed at construction, not read off the `Model` (so it stays
  a real Azure deployment name even when the injected model is `TestModel`/`FunctionModel`);
* `from_settings` builds a working adapter over the `"test-model"` shortcut (network-free) whose
  `model_id` is the configured `IngestionSettings.summary_model`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.settings import AzureOpenAiSettings
from pydantic_ai.messages import ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from engram.config import IngestionSettings
from engram.indexing.contracts import Summarizer
from engram.indexing.infrastructure.summarizer import PydanticAiSummarizer

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

models_block = pytest.importorskip("pydantic_ai.models")
models_block.ALLOW_MODEL_REQUESTS = False  # hard-block stray real-provider calls; the doubles stay exempt


class TestPydanticAiSummarizer:
    def test_satisfies_the_summarizer_port(self):
        summarizer = PydanticAiSummarizer(TestModel(), model_id="gpt-5-mini")

        assert isinstance(summarizer, Summarizer)

    def test_model_id_is_the_configured_deployment_not_the_models_own_name(self):
        summarizer = PydanticAiSummarizer(TestModel(model_name="test"), model_id="gpt-5-mini")

        assert summarizer.model_id == "gpt-5-mini"

    async def test_summarize_returns_the_models_emitted_text(self):
        model = TestModel(custom_output_text="LightRAG indexes a knowledge base as a graph and retrieves at two levels.")
        summarizer = PydanticAiSummarizer(model, model_id="gpt-5-mini")

        summary = await summarizer.summarize(title="LightRAG", body="Body text about LightRAG.")

        assert summary == "LightRAG indexes a knowledge base as a graph and retrieves at two levels."

    async def test_prompt_carries_the_title_and_body(self):
        captured: dict[str, object] = {}

        def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            del info
            captured["user_text"] = "\n".join(
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart) and isinstance(part.content, str)
            )
            return ModelResponse(parts=[TextPart(content="A summary.")])

        summarizer = PydanticAiSummarizer(FunctionModel(script), model_id="gpt-5-mini")

        await summarizer.summarize(title="LightRAG", body="LightRAG indexes a knowledge base as a graph.")

        user_text = captured["user_text"]
        assert isinstance(user_text, str)
        assert "LightRAG" in user_text
        assert "LightRAG indexes a knowledge base as a graph." in user_text

    async def test_instructions_are_the_stable_summarizer_contract_not_per_call_context(self):
        captured: dict[str, object] = {}

        def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            del messages
            captured["instructions"] = info.instructions
            return ModelResponse(parts=[TextPart(content="A summary.")])

        summarizer = PydanticAiSummarizer(FunctionModel(script), model_id="gpt-5-mini")

        await summarizer.summarize(title="X", body="Y")

        instructions = captured["instructions"]
        assert isinstance(instructions, str)
        assert "summary" in instructions.lower()
        # The per-call title/body must not have leaked into the durable instructions.
        assert "X" not in instructions
        assert "Y" not in instructions

    async def test_each_call_builds_a_fresh_agent_no_shared_state_between_calls(self):
        model = TestModel(custom_output_text="A summary.")
        summarizer = PydanticAiSummarizer(model, model_id="gpt-5-mini")

        first = await summarizer.summarize(title="A", body="Body A.")
        second = await summarizer.summarize(title="B", body="Body B.")

        assert first == second == "A summary."


class TestFromSettings:
    def test_builds_over_the_test_model_shortcut_network_free(self):
        azure_openai = AzureOpenAiSettings(endpoint="https://example.openai.azure.com")
        ingestion = IngestionSettings(summary_model="test-model")

        summarizer = PydanticAiSummarizer.from_settings(azure_openai, ingestion)

        assert summarizer.model_id == "test-model"

    async def test_the_test_model_shortcut_summarizes_without_network(self):
        azure_openai = AzureOpenAiSettings(endpoint="https://example.openai.azure.com")
        ingestion = IngestionSettings(summary_model="test-model")
        summarizer = PydanticAiSummarizer.from_settings(azure_openai, ingestion)

        summary = await summarizer.summarize(title="X", body="Y")

        assert isinstance(summary, str)
        assert summary
