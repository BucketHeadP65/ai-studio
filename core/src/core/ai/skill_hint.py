"""A dynamic reminder that names the skill a decision model picks for a run's prompt."""

from __future__ import annotations

import asyncio

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pydantic_ai.tools import RunContext

    from core.ai.decision_models import DecisionModelQuestions

NO_SKILL = "(none)"
"""The option a pick takes when no listed skill fits. No skill can carry this name, since a skill's name is kebab case."""

_NO_SKILL_MEANING = "No listed skill fits this message."
_QUESTION = "Which skill's instructions does this message need?"
_PERMUTATIONS = 3


@dataclass(frozen=True)
class SkillHint[DepsT]:
    """Names the skill a decision model picks for a run's prompt, on the run's first model request only.

    Mount it in pydantic-ai-harness's `SystemReminders` as a dynamic reminder. It answers nothing on a later
    request, for an empty or missing text, when every skill is loaded, when the pick is none of them or falls
    below `threshold`, and when the pick fails.
    """

    questions: DecisionModelQuestions
    """Who picks the skill."""
    skills: Callable[[RunContext[DepsT]], Mapping[str, str]]
    """The skills the run may load, each name mapped to its description. It runs in a worker thread, so it may read files."""
    text: Callable[[RunContext[DepsT]], str | None]
    """The text the pick is judged on, or `None` to give the run no hint."""
    threshold: float
    """The least averaged probability a pick needs before it is named."""
    deadline_seconds: float
    """How long the pick may take before the request goes on without a hint."""

    async def __call__(self, ctx: RunContext[DepsT]) -> str | None:
        """The hint for the run's first model request, or `None`.

        Args:
            ctx: The run, read for its step and its loaded skills, and handed to `skills` and `text`.

        Returns:
            One line naming the skill, or `None` when there is nothing to name.
        """
        if ctx.run_step != 1:
            return None
        text = self.text(ctx)
        if text is None or not text.strip():
            return None
        skills = await asyncio.to_thread(self.skills, ctx)
        candidates = {name: meaning for name, meaning in skills.items() if name not in ctx.loaded_capability_ids}
        if not candidates:
            return None
        picked = await self.questions.pick(
            text,
            {**candidates, NO_SKILL: _NO_SKILL_MEANING},
            question=_QUESTION,
            permutations=_PERMUTATIONS,
            deadline_seconds=self.deadline_seconds,
        )
        if picked is None or picked[0] == NO_SKILL or picked[1] < self.threshold:
            return None
        return f"<system-reminder>The {picked[0]} skill likely applies. Load it with load_capability if it does.</system-reminder>"
