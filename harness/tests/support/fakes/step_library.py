"""A folder library that also keeps reusable steps, scriptable as a mapping from step id to step."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import LibraryError
from core.ai.workflows.infrastructure.folder import FolderLibrary

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from core.ai.workflows.contracts import InlineStep


class FakeStepLibrary(FolderLibrary):
    """Workflows in a folder, and the reusable steps in `steps`, read back by id.

    `fetch_step` answers `steps[step_id]` and raises `LibraryError` for any other id, as a library
    with no page under that id does. `fetched` records every id asked for, in order.
    """

    def __init__(self, root: Path, steps: Mapping[str, InlineStep]) -> None:
        super().__init__(root)
        self.steps = dict(steps)
        self.fetched: list[str] = []

    async def fetch_step(self, step_id: str) -> InlineStep:
        """The step kept under `step_id`."""
        self.fetched.append(step_id)
        if step_id not in self.steps:
            msg = f"no reusable step has the id {step_id!r}"
            raise LibraryError(msg)
        return self.steps[step_id]
