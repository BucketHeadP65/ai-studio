"""The errors the browsing slice raises when a card cannot be rewritten, a step lock file cannot be written, an import refuses a workflow file, or a maintenance flag page cannot be read."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import LibraryError

from harness.adapters.contracts import STORE_DB

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
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


class FlagUnreadableError(LibraryError):
    """A maintenance flag page in the library does not parse, so no flag was read or written.

    The message is the library's, which names the page, followed by what to do about the page.
    """

    def __init__(self, refusal: LibraryError) -> None:
        """Keep the library's refusal on one line and add what to do about the page.

        Args:
            refusal: What the library raised reading the page.
        """
        read = " ".join(str(refusal).split()).rstrip(".")
        super().__init__(f"{read}. Fix or delete that flag page in the library's database ({STORE_DB}, collection workflows), then try again.")


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


class WorkflowVersionSupersededError(LibraryError):
    """The library moved past a workflow file's version, and someone other than the import's `moved_past_by` saved the newer one.

    The message names the file, its version, the newest live version and who saved it, and says how to bring the file up.
    """

    def __init__(self, file: Path, workflow_id: str, *, newest_id: str, newest_version: int, saved_by: str) -> None:
        """Name the file, its version and the newest live version, and say how to import the file.

        Args:
            file: The workflow file.
            workflow_id: The library id of the version the file names.
            newest_id: The library id of the newest live version of the workflow.
            newest_version: The newest live version's number.
            saved_by: Who saved the newest live version.
        """
        super().__init__(
            f"{file} holds {workflow_id}, which the library has moved past: its newest live version is {newest_id}, saved by "
            f"{saved_by}. Bring the file up to {newest_id}, or raise `version` in it above {newest_version} to save its content "
            f"as a new version, then import again. Nothing was written."
        )
        self.workflow_id = workflow_id
        """The version the file names."""
        self.newest_id = newest_id
        """The newest live version of the workflow."""


class WorkflowCardChangedError(LibraryError):
    """A workflow file holds the workflow its saved version holds under other words, so the version would change in place.

    The message names the file, the version and each word that differs, and says to raise the version.
    """

    def __init__(self, file: Path, workflow_id: str, fields: Sequence[str]) -> None:
        """Name the file, the version and each word that differs, and say how to save the new words.

        Args:
            file: The workflow file.
            workflow_id: The library id of the version the file names.
            fields: Each differing word, as the file names it: `title`, `description`, `body` or `presentation`.
        """
        super().__init__(
            f"{file} holds {workflow_id} with the workflow the library holds and a different {', '.join(fields)}. The library "
            f"keeps one card per version. Raise `version` in the workflow file and import again. Nothing was written."
        )
        self.workflow_id = workflow_id
        """The version the file names."""
        self.fields = tuple(fields)
        """Each differing word, as the file names it."""


class WorkflowVersionChangedError(LibraryError):
    """A workflow file holds other content under a version the library holds, so the version would change in place.

    The message names the file and the version, and says to bring the file up to the library's version or to raise it.
    """

    def __init__(self, file: Path, workflow_id: str) -> None:
        """Name the file and the version, and say how to keep the library's content or save the file's.

        Args:
            file: The workflow file.
            workflow_id: The library id of the version the file names.
        """
        super().__init__(
            f"{file} holds {workflow_id}, and the library holds that version with other content. The library keeps one "
            f"workflow per version. If the library's {workflow_id} is the one to keep, bring the file up to it, its content "
            f"and not only its version number. If the file's content is new, raise `version` in it above every version the "
            f"library holds of the workflow. Then import again. Nothing was written."
        )
        self.workflow_id = workflow_id
        """The version the file names."""


class WorkflowNotLiveError(LibraryError):
    """A workflow file names a version the library superseded, and no newer version of its workflow is live.

    The message names the file and the version and says how to import the file.
    """

    def __init__(self, file: Path, workflow_id: str, *, highest_version: int) -> None:
        """Name the file and the version, and say how to import the file.

        Args:
            file: The workflow file.
            workflow_id: The library id of the version the file names.
            highest_version: The highest version of the workflow the library holds, live, superseded or archived.
        """
        super().__init__(
            f"{file} holds {workflow_id}, which the library has superseded, and no newer version of it is live because "
            f"the newer ones are archived. Restore the newer version in the library, or raise `version` in the file above "
            f"{highest_version} to save its content as a new version, then import again. Nothing was written."
        )
        self.workflow_id = workflow_id
        """The version the file names."""


class WorkflowNameRepeatedError(LibraryError):
    """Two workflow files of one folder hold the same workflow, so the import cannot tell which one to keep.

    The message names both files and says to keep one.
    """

    def __init__(self, earlier: Path, later: Path, name: str) -> None:
        """Name both files and the workflow, and say how to import the folder.

        Args:
            earlier: The file that comes first in sorted file order.
            later: The file that comes after it.
            name: The workflow both files hold.
        """
        super().__init__(
            f"{earlier} and {later} both hold the workflow {name!r}. A folder holds one file per workflow. Keep one of "
            f"them, then import again. Nothing was written."
        )
        self.files = (earlier, later)
        """Both files, in sorted file order."""


class WorkflowVersionUnreadableError(LibraryError):
    """The library's copy of a workflow file's version no longer reads as a workflow, so the import cannot compare them.

    The message names the file, the version and why the copy does not read, and says to raise the version.
    """

    def __init__(self, file: Path, workflow_id: str, reason: str) -> None:
        """Name the file, the version and why its copy does not read, and say how to import the file.

        Args:
            file: The workflow file.
            workflow_id: The library id of the version the file names.
            reason: Why the library's copy does not read, on one line.
        """
        super().__init__(
            f"{file} holds {workflow_id}, and the library's copy of that version no longer reads as a workflow ({reason}). "
            f"Raise `version` in the workflow file above every version the library holds of it to save its content as a "
            f"new version, then import again. Nothing was written."
        )
        self.workflow_id = workflow_id
        """The version the file names."""
