r"""`PydanticAiSummarizer`, the `Summarizer` that writes disclosure summaries through a pydantic-ai agent.

The model is passed in once. Each `summarize` call builds a fresh agent, so one instance serves a whole summary run.
A summary is plain prose.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai import create_azure_model
from pydantic_ai import Agent

if TYPE_CHECKING:
    from core.settings import AzureOpenAiSettings
    from pydantic_ai.models import Model

    from engram.config import IngestionSettings

__all__ = ["PydanticAiSummarizer"]

# The summarizer's instructions. The title and body of each unit travel in the run prompt.
_SUMMARY_INSTRUCTIONS = (
    "Write a concise, faithful prose summary of the given page or section for a "
    "progressive-disclosure index: two to four sentences, no headings, no bullet points, no "
    "meta-commentary about the summarization task itself. State what the unit covers and its key "
    "claims or findings, using only information present in its body."
)


class PydanticAiSummarizer:
    """The `Summarizer` that makes one pydantic-ai agent call per unit."""

    def __init__(self, model: Model, *, model_id: str) -> None:
        """Bind the summarizer to one model.

        Args:
            model: The model every summary runs on.
            model_id: The deployment name recorded on every `kb.unit_summaries` row this summarizer writes.
        """
        self._model = model
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        """The Azure deployment name recorded on every summary this adapter writes."""
        return self._model_id

    async def summarize(self, *, title: str, body: str) -> str:
        """Return a prose summary of `body`, with `title` as context.

        Args:
            title: The title of the page or section.
            body: The text to summarize.

        Returns:
            Two to four sentences of prose.
        """
        agent = Agent(self._model, name="summary-stage", instructions=_SUMMARY_INSTRUCTIONS)
        result = await agent.run(f"Title: {title}\n\n{body}")
        return result.output

    @classmethod
    def from_settings(cls, azure_openai: AzureOpenAiSettings, ingestion: IngestionSettings) -> PydanticAiSummarizer:
        """Build the summarizer over an Azure OpenAI model.

        Args:
            azure_openai: The Azure OpenAI resource the summary deployment runs on.
            ingestion: The ingestion settings naming the summary model.

        Returns:
            The summarizer.
        """
        model = create_azure_model(ingestion.summary_model, endpoint=azure_openai.endpoint)
        return cls(model, model_id=ingestion.summary_model)
