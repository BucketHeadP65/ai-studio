"""The ports the registry of knowledge bases reads and writes its registered knowledge bases through."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pathlib import Path

    from studio.knowledge_base.settings import KnowledgeBaseEntry, Schedule


class RegisteredKnowledgeBases(Protocol):
    """The registered knowledge bases, changed one targeted write at a time.

    Every write keeps every other registered knowledge base and every other settings key as it is.
    A write that would leave settings the application cannot load again is refused, and nothing is written.
    There is one writer, and the last write wins.
    """

    def register(self, name: str, entry: KnowledgeBaseEntry) -> None:
        """Write `entry` under `name`, replacing any registered knowledge base of that name.

        The written entry carries `entry.schedule` when it has one, and marks it private only when `entry.private` is set.

        Args:
            name: The registered name.
            entry: The folder, description, schedule and privacy to write.

        Raises:
            KnowledgeBaseRefusedError: `REGISTRY_WRITE_REFUSED` when the settings would not load after the write.
        """
        ...

    def unregister(self, name: str) -> tuple[Path, str]:
        """Remove the registered knowledge base `name`, one that fails validation included.

        Args:
            name: The registered name.

        Returns:
            The removed entry's folder and description, `Path()` and `""` when the entry held none.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE` when nothing is registered under `name`,
                and `REGISTRY_WRITE_REFUSED` when the settings would not load after the write.
        """
        ...

    def write_schedule(self, name: str, schedule: Schedule | None) -> None:
        """Set or clear the schedule of the registered knowledge base `name`.

        A registered knowledge base that fails validation can still take a schedule, which applies once it is fixed.

        Args:
            name: The registered name.
            schedule: The schedule to set, or `None` to clear it.

        Raises:
            KnowledgeBaseRefusedError: `UNKNOWN_KNOWLEDGE_BASE` when nothing is registered under `name`,
                and `REGISTRY_WRITE_REFUSED` when the settings would not load after the write.
        """
        ...


class RegisteredKnowledgeBaseQueries(Protocol):
    """Reads of the registered knowledge bases that change nothing, each one read afresh."""

    def registered(self) -> dict[str, KnowledgeBaseEntry]:
        """Every registered knowledge base that passes validation.

        Returns:
            Each valid entry by its registered name.
        """
        ...

    def registered_and_broken(self) -> tuple[dict[str, KnowledgeBaseEntry], dict[str, str | None]]:
        """Every registered knowledge base, split into those that pass validation and those that fail it.

        Returns:
            The valid entries by name, and each failing name with its unvalidated folder, `None` when it names none.
        """
        ...
