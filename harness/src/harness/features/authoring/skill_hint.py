"""The chat agent's skill hint: the skill a decision model picks for each turn's message, named on its first request."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.decision_models import DecisionModelQuestions, decision_model
from core.ai.skill_hint import SkillHint
from core.observability import get_logger
from pydantic_ai_harness.system_reminders import SystemReminders

from harness.domain.contracts import ORCHESTRATOR_SKILLS, strip_page_context
from harness.features.projects.contracts import catalog
from harness.features.selfauthoring.contracts import selected_profile_skills
from harness.session import ANSWERABLE, Runtime
from harness.settings import CHECKOUT

if TYPE_CHECKING:
    from core.settings import OpenRouterSettings
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.tools import RunContext

    from harness.features.sessions.contracts import ProjectConfig
    from harness.settings import SkillHintSettings, StateSettings

_logger = get_logger(__name__)


def skill_hint_capability(
    settings: SkillHintSettings,
    openrouter: OpenRouterSettings,
    *,
    state: StateSettings,
    project: ProjectConfig,
    spend: AbstractCapability[object] | None,
) -> AbstractCapability[Runtime] | None:
    """The skill hint as a dynamic reminder, or `None` when its settings turn it off or OpenRouter has no key.

    The skills offered are the ones the chat agent mounts for the selected profile. The text judged is the
    turn's message with its page context removed. The hint is offered only on a turn someone attends. A missing key logs one warning naming the setting.

    Args:
        settings: The `skill_hint` section.
        openrouter: The OpenRouter section of the infrastructure settings.
        state: The process state the selected profile is read from.
        project: The skill directories the catalog reads.
        spend: The process's spend limits, which every pick is charged to.

    Returns:
        The capability to mount, or `None`.
    """
    if not settings.enabled:
        return None
    if not openrouter.api_key:
        _logger.warning("the skill hint is on, and infrastructure.openrouter.api_key is empty, so turns run without it")
        return None
    questions = DecisionModelQuestions(decision_model(settings.model, openrouter), capabilities=(spend,) if spend is not None else ())

    def mounted_skills(_ctx: RunContext[Runtime]) -> dict[str, str]:
        names = selected_profile_skills(state)
        include = frozenset(names) if names is not None else frozenset(ORCHESTRATOR_SKILLS)
        return {card.name: card.description for card in catalog(CHECKOUT, project) if card.valid and card.name in include}

    def message(ctx: RunContext[Runtime]) -> str | None:
        return strip_page_context(ctx.prompt) if ANSWERABLE.get() and isinstance(ctx.prompt, str) else None

    hint = SkillHint[Runtime](
        questions=questions, skills=mounted_skills, text=message, threshold=settings.threshold, deadline_seconds=settings.deadline_seconds
    )
    return SystemReminders[Runtime](dynamic_reminders=[hint])
