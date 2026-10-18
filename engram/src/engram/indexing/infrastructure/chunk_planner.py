"""`PydanticAiChunkPlanner`, the `ChunkPlanner` that asks a pydantic-ai model where a text changes topic.

The model is passed in once and runs at a low thinking level, which the planner's identity names. Each call builds a fresh agent that is shown the
paragraphs numbered from 1 and answers paragraph numbers in a structured output, never text.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai import create_azure_model
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.settings import ModelSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.settings import AzureOpenAiSettings
    from core.thinking import Thinking
    from pydantic_ai.models import Model

    from engram.config import IngestionSettings

__all__ = ["PydanticAiChunkPlanner"]

# You must raise it whenever the instructions, the way paragraphs are shown or the answer's shape change, so stored cut plans are chosen again.
_PROMPT_VERSION = 1

# The thinking level every call runs at. It is part of the planner's identity, so changing it chooses stored cut plans again.
_THINKING: Thinking = "low"

_INSTRUCTIONS = (
    "You find where a document changes topic, so that a search index can store each topic as its own chunk. "
    "The document is shown as numbered paragraphs, each opening with its number in square brackets. "
    "Answer the numbers of the paragraphs that open a new topic, in increasing order. "
    "Paragraph 1 always opens the first topic, so leave it out. "
    "Keep paragraphs about one subject together, and answer an empty list when the whole document keeps to one subject. "
    "Answer paragraph numbers only, never text."
)

_MODEL_SETTINGS = ModelSettings(thinking=_THINKING)


class _TopicStarts(BaseModel):
    """The paragraphs that open a new topic."""

    starts: list[int] = Field(description="The numbers of the paragraphs that open a new topic, in increasing order, paragraph 1 left out.")


class PydanticAiChunkPlanner:
    """The `ChunkPlanner` that makes one pydantic-ai agent call per text."""

    def __init__(self, model: Model, *, model_id: str) -> None:
        """Bind the planner to one model.

        Args:
            model: The model every call runs on.
            model_id: The deployment name the planner's identity names.
        """
        self._model = model
        self._model_id = model_id

    @property
    def identity(self) -> str:
        """The deployment name, prompt version and thinking level, such as `gpt-6-luna/prompt-1/thinking-low`."""
        return self.identity_for(self._model_id)

    @staticmethod
    def identity_for(model_id: str) -> str:
        """The identity a planner over one deployment has, known without building a model.

        Args:
            model_id: The deployment name.

        Returns:
            The deployment name, prompt version and thinking level.
        """
        return f"{model_id}/prompt-{_PROMPT_VERSION}/thinking-{_THINKING}"

    async def chunk_starts(self, paragraphs: Sequence[str]) -> Sequence[int]:
        """Ask the model which paragraphs open a new topic, showing each paragraph after its number in square brackets.

        Args:
            paragraphs: The text's paragraphs in reading order, numbered from 1.

        Returns:
            The paragraph numbers the model answered, unchecked.
        """
        agent = Agent(self._model, name="chunk-planner", instructions=_INSTRUCTIONS, output_type=_TopicStarts, model_settings=_MODEL_SETTINGS)
        result = await agent.run("\n\n".join(f"[{number}] {paragraph}" for number, paragraph in enumerate(paragraphs, start=1)))
        return result.output.starts

    @classmethod
    def from_settings(cls, azure_openai: AzureOpenAiSettings, ingestion: IngestionSettings) -> PydanticAiChunkPlanner:
        """Build the planner over the Azure OpenAI deployment the ingestion settings name.

        `ingestion.chunk_planner_model` must name a deployment.

        Args:
            azure_openai: The Azure OpenAI resource the deployment runs on.
            ingestion: The ingestion settings naming the chunk planner's deployment.

        Returns:
            The planner.
        """
        model = create_azure_model(ingestion.chunk_planner_model, endpoint=azure_openai.endpoint)
        return cls(model, model_id=ingestion.chunk_planner_model)
