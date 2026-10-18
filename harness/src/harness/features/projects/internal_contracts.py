"""The port of the projects slice: the skill directories a project's configuration names."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Sequence


class SkillDirectories(Protocol):
    """A project's configuration, as far as the skill catalog reads it."""

    @property
    def skills(self) -> Sequence[str]:
        """Each configured skill directory, relative to the project root."""
        ...
