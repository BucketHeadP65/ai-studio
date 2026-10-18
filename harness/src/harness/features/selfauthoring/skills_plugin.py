"""`list_skills`: the authoring agent's read plugin over the registry and the project's own libraries.

Reads the same cards `catalog` builds for the projects station, so an agent sees the
identical names that screen shows. `registry_skill_names` and `known_skill_names` are the name
space `write_custom_agent` and `write_custom_profile` refuse an unknown skill name against.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import FunctionToolset

from harness.features.projects.contracts import BUILTIN_SKILL_DIRECTORY, catalog
from harness.features.sessions.contracts import ProjectConfig
from harness.settings import SKILLS

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence
    from pathlib import Path

    from pydantic_ai.toolsets import AgentToolset


def known_skill_names(root: Path, config: ProjectConfig) -> frozenset[str]:
    """Every valid skill name across the registry and `config`'s own configured libraries.

    Args:
        root: The checkout the registry and the project's libraries are read from.
        config: The project configuration naming its own skill libraries.

    Returns:
        The names.
    """
    return frozenset(card.name for card in catalog(root, config) if card.valid)


def registry_skill_names() -> frozenset[str]:
    """Every valid skill name in the registry alone, with no project library configured.

    Returns:
        The names.
    """
    return known_skill_names(SKILLS, ProjectConfig())


def unknown_skill_names(names: Sequence[str], known: Collection[str]) -> list[str]:
    """Every name in `names` absent from `known`, sorted.

    Args:
        names: The skill names a document declares.
        known: The skill names on file.

    Returns:
        The unknown names, sorted and deduplicated.
    """
    return sorted({name for name in names if name not in known})


def skill_refusal(unknown: Sequence[str], known: Collection[str]) -> str:
    """The refusal sentence: the unknown name(s), and every name on file, sorted.

    Args:
        unknown: The skill names that are not on file.
        known: The skill names on file.

    Returns:
        The sentence.
    """
    names = ", ".join(repr(name) for name in unknown)
    catalog_names = ", ".join(sorted(known)) or "none"
    return f"skill {names} is not on file. The registry holds: {catalog_names}."


def list_skills_lines(root: Path, config: ProjectConfig) -> str:
    """One line per skill, `name: description`, sorted by name, registry first then the project's own libraries.

    Args:
        root: The checkout the registry and the project's libraries are read from.
        config: The project configuration naming its own skill libraries.

    Returns:
        The lines, or `No skills on file.` when there are none.
    """
    cards = sorted(
        (card for card in catalog(root, config) if card.valid),
        key=lambda card: (card.library != BUILTIN_SKILL_DIRECTORY, card.name),
    )
    if not cards:
        return "No skills on file."
    return "\n".join(f"{card.name}: {card.description}" for card in cards)


class SkillsPluginToolset(FunctionToolset[AgentDepsT]):
    """Exposes `list_skills`."""

    def __init__(self, root: Path, config: ProjectConfig) -> None:
        """Register the tool over the catalog of `root` and `config`.

        Args:
            root: The checkout the registry and the project's libraries are read from.
            config: The project configuration naming its own skill libraries.
        """
        super().__init__()
        self._root = root
        self._config = config
        self.add_function(self.list_skills, name="list_skills")

    async def list_skills(self) -> str:
        """List every skill on file: the registry plus the project's own libraries, name and description, sorted by name."""
        return list_skills_lines(self._root, self._config)


@dataclass
class SkillsPlugin(AbstractCapability[AgentDepsT]):
    """The authoring agent's read plugin over the skills catalog: `list_skills`.

    Read fresh on every call: a skill scaffolded, edited, or removed since the session opened
    reaches the very next call, with no rebuild.
    """

    root: Path
    config: ProjectConfig

    def get_toolset(self) -> AgentToolset[AgentDepsT] | None:
        """The toolset providing `list_skills` over this plugin's catalog.

        Returns:
            The toolset.
        """
        return SkillsPluginToolset[AgentDepsT](self.root, self.config)

    @classmethod
    def get_serialization_name(cls) -> str | None:
        """Not spec-serializable: the capability holds a live project root and config.

        Returns:
            Always `None`.
        """
        return None
