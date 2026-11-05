"""The errors the browsing slice raises when a workflow's card cannot be rewritten or a step lock file cannot be written."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from harness.domain.contracts import DraftReadiness


class CardRefusedError(Exception):
    """A card rewrite was refused because a field it carries fails the readiness check.

    The message names what was refused. `readiness` is the reading of the merged card.
    """

    def __init__(self, message: str, *, readiness: DraftReadiness) -> None:
        """Name what was refused and keep the reading it was refused on.

        Args:
            message: What was refused, in words the person can act on.
            readiness: The readiness reading of the merged card.
        """
        super().__init__(message)
        self.readiness = readiness


class StepLockRefusedError(Exception):
    """A step lock file was not written because a step file changed while its version did not rise.

    The message names each such step and says to raise its version. `steps` holds their names.
    """

    def __init__(self, folder: Path, changed: Mapping[str, int]) -> None:
        """Name each changed step and its version, and say how to record the change.

        Args:
            folder: The folder whose `steps.lock` was not written.
            changed: Each changed step's name and the version its step file still declares.
        """
        named = ", ".join(f"`{name}` (version {version})" for name, version in changed.items())
        super().__init__(
            f"{folder / 'steps.lock'} was not written, because these steps changed while their version did not rise: {named}. "
            f"Raise `version` in each one's step file, then run `harness steps lock {folder}` again."
        )
        self.steps = tuple(changed)
        """The names of the steps that changed at an unchanged version."""
