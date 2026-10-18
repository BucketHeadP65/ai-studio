"""The content cache kept in files, so an entry outlives a process restart within the same run."""

from __future__ import annotations

import tempfile

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai_harness.tool_output_limits import LocalFileStore

if TYPE_CHECKING:
    from datetime import timedelta


def _default_dir() -> Path:
    """A stable per-machine default, so a restarted process finds the same cache."""
    return Path(tempfile.gettempdir()) / "pyai_harness_format_maps"


@dataclass
class FileContentCache:
    """A content cache in files, whose entries survive a process restart within the same run.

    It writes through pydantic-ai-harness's `LocalFileStore`, which keeps an owner-only root and checks every read stays
    inside it. Keys are run-scoped, so a stale entry belongs to a finished run and is never a wrong answer.
    """

    base_dir: Path = field(default_factory=_default_dir)
    """The folder the entries live in."""

    cleanup_after: timedelta | None = None
    """How old an entry may grow before it is pruned. `None` keeps every entry until something else removes it."""

    _store: LocalFileStore = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._store = LocalFileStore(self.base_dir, cleanup_after=self.cleanup_after)

    async def put(self, key: str, text: str) -> None:
        """Write `text` as UTF-8 to the entry's file.

        Args:
            key: The run-scoped key of one call's content.
            text: The content to keep.
        """
        await self._store.write(key, text.encode("utf-8"))

    async def get(self, key: str) -> str | None:
        """Read the entry's file, taking a missing or unreadable file as a miss.

        Args:
            key: The run-scoped key of one call's content.

        Returns:
            The stored text, or `None` when the entry's file is gone or unreadable.
        """
        try:
            data = await self._store.read(key)
        except OSError:
            return None
        return data.decode("utf-8", errors="replace")
