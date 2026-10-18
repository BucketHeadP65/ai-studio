"""A project index answering from a fixed mapping, instead of the session projection."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path


class FakeProjectIndex:
    """Every project id and root a test wants known, held in memory."""

    def __init__(self, roots: dict[str, Path], sessions: dict[str, Path] | None = None) -> None:
        self._roots = dict(roots)
        self._sessions = dict(sessions or {})

    def id_of(self, root: Path) -> str:
        """The project id `root` was given."""
        for project_id, known_root in self._roots.items():
            if known_root == root:
                return project_id
        msg = f"no project rooted at {root!r}"
        raise KeyError(msg)

    async def root_of(self, project_id: str) -> Path | None:
        """`project_id`'s root, or `None` when it was never given one."""
        return self._roots.get(project_id)

    async def root_of_session(self, session_id: str) -> Path | None:
        """The root `sessions` gave `session_id`, or `None` when it gave none."""
        return self._sessions.get(session_id)
