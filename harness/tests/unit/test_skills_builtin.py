"""The builtin skill set: every SKILL.md under `settings.SKILLS` loads, and the delegation skill is among them."""

from __future__ import annotations

from harness.features.projects.contracts import BUILTIN_SKILL_DIRECTORY, catalog
from harness.features.sessions.project_config import ProjectConfig
from harness.settings import CHECKOUT


def test_every_builtin_skill_loads() -> None:
    builtin = [card for card in catalog(CHECKOUT, ProjectConfig()) if card.library == BUILTIN_SKILL_DIRECTORY]

    assert builtin, "no builtin skills found"
    assert all(card.valid for card in builtin), [card.error for card in builtin if not card.valid]


def test_the_registry_holds_every_skill_by_name() -> None:
    names = {card.name for card in catalog(CHECKOUT, ProjectConfig()) if card.library == BUILTIN_SKILL_DIRECTORY}

    assert names == {
        "agent-ux",
        "context-engineering",
        "delegating-work",
        "designing-workflows",
        "orchestrating-a-run",
        "saving-a-workflow",
        "placing-knowledge",
        "writing-agents",
        "writing-skills",
    }
