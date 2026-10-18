"""A model listing whose answers are scripted, for tests that must not touch the network."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.adapters.model_list import ModelEntry


@dataclass
class FakeModelListing:
    """Answers `fetch` with `entries`, counting the calls, and raises `failure` when one is set."""

    entries: list[ModelEntry] = field(default_factory=list)
    failure: Exception | None = None
    fetches: int = 0

    async def fetch(self) -> list[ModelEntry]:
        """The scripted entries, or the scripted failure."""
        self.fetches += 1
        if self.failure is not None:
            raise self.failure
        return list(self.entries)
