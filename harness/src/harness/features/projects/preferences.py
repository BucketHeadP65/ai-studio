"""Preferences: the owner's standing voice, read fresh from disk on every request.

Two files: `global_path` holds everywhere and `project_path` holds only in this project. Neither is cached at
construction. Each request reads both again, so an edit to either file reaches the very next turn.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import AgentDepsT

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic_ai._instructions import AgentInstructions
    from pydantic_ai.tools import RunContext

_HEADING = "## Owner preferences"
_PROJECT_HEADING = "### This project"


def _read(path: Path) -> str:
    """`path`'s stripped text, or `''` when it is missing, unreadable, or empty."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace").strip()


def _render(global_path: Path, project_path: Path) -> str:
    """The `## Owner preferences` block, global text first and the project's under `### This project`, or `''` when both are empty."""
    global_text = _read(global_path)
    project_text = _read(project_path)
    if not global_text and not project_text:
        return ""
    sections = [_HEADING]
    if global_text:
        sections.append(global_text)
    if project_text:
        sections.append(_PROJECT_HEADING)
        sections.append(project_text)
    return "\n\n".join(sections)


class Preferences(AbstractCapability[AgentDepsT]):
    """The owner's standing preferences, read fresh each run: the global file, then the project's.

    The owner writes them and they prescribe, so they speak as instructions, unlike memory, which the agent writes and
    which rides as user-role context. A missing, unreadable or empty file contributes nothing.
    """

    def __init__(self, *, global_path: Path, project_path: Path) -> None:
        """Name the two preference files, read again on every request.

        Args:
            global_path: The file whose preferences hold in every project.
            project_path: The file whose preferences hold only in this project.
        """
        self.global_path = global_path
        self.project_path = project_path

    def get_instructions(self) -> AgentInstructions[AgentDepsT] | None:
        """A per-request read of both preference files.

        Returns:
            A function that renders the preferences block for one request.
        """

        def compose(ctx: RunContext[AgentDepsT]) -> str:
            del ctx
            return _render(self.global_path, self.project_path)

        return compose
