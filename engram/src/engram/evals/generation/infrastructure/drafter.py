"""The pydantic-ai drafter that writes question, answer and quotes from a seed."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent

from engram.evals.generation.domain.drafting import SeedCaseDrafts

if TYPE_CHECKING:
    from pydantic_ai.models import Model

    from engram.evals.generation.domain.drafting import SeedCaseDraft
    from engram.evals.shared.contracts import Seed

_GROUNDING_RULES = (
    "You write evaluation questions for a retrieval system over a knowledge base. Given a set of source "
    "passages, write self-contained question/answer pairs answerable only from those sources. For "
    "each case set support_quotes to one or more SHORT snippets copied VERBATIM (character for "
    "character) from the provided sources. Never invent quotes. Do NOT pick source ids or "
    "difficulty: those are fixed by the seed."
)
"""The rules every drafted case follows, whatever its archetype."""


def build_drafter_agent(model: Model | str, *, instructions: str) -> Agent[None, SeedCaseDrafts]:
    """Build the gold-from-seed drafting agent (structured output, no retrieval tools).

    The grounding rules always come first. `instructions` adds what one archetype asks for and never replaces them.

    Args:
        model: The model that drafts cases, Azure OpenAI in production, a network-free
            `TestModel` / `FunctionModel` in tests.
        instructions: The archetype's own drafting prompt.

    Returns:
        An `Agent` whose output is a validated `SeedCaseDrafts`.
    """
    return Agent(model, instructions=[_GROUNDING_RULES, instructions], output_type=SeedCaseDrafts)


def _seed_prompt(seed: Seed, target: int) -> str:
    """The user prompt for a seed: its numbered source passages and the case target."""
    passages = "\n\n".join(f"[source {index}]\n{body}" for index, body in enumerate(seed.source_bodies))
    return (
        f"Write up to {target} grounded question/answer cases answerable only from the source "
        f"passages below. Each support_quote must be copied verbatim from these passages.\n\n"
        f"--- sources ---\n{passages}\n--- end sources ---"
    )


class PydanticAiDrafter:
    """A `CaseDrafter` backed by a pydantic-ai agent.

    It builds a new agent on each call, so every call may pass its own `instructions`.
    """

    def __init__(self, model: Model | str) -> None:
        """Hold the model each drafting agent is built from.

        Args:
            model: The model, or its name, that drafts cases.
        """
        self._model = model

    async def draft(self, seed: Seed, *, instructions: str, target: int) -> list[SeedCaseDraft]:
        """Draft cases with a pydantic-ai agent built for this call, returning the drafts unchecked.

        Args:
            seed: The structural seed whose `source_bodies` ground the drafts.
            instructions: The drafting system prompt for this seed's archetype.
            target: How many cases to aim for (the model may return fewer).

        Returns:
            The drafted `SeedCaseDraft`s, in the order the model emitted them.
        """
        agent = build_drafter_agent(self._model, instructions=instructions)
        result = await agent.run(_seed_prompt(seed, target))
        return result.output.cases
