"""What one run of a rewrite reports: what it rewrote and what it had to leave alone."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MigrationReport:
    """What one run of a rewrite did, in the order it did it."""

    files: list[str] = field(default_factory=list)
    """One line per file or folder renamed or rewritten."""
    refused: list[str] = field(default_factory=list)
    """One line per thing left alone, with the reason."""

    @property
    def clean(self) -> bool:
        """Whether everything this run met came through. A refusal is reported, never raised."""
        return not self.refused


@dataclass
class LibraryReport(MigrationReport):
    """What one run of a rewrite over the library did, its pages and the evidence file included."""

    pages: list[str] = field(default_factory=list)
    """The id of each library page rewritten."""
    unlinked: list[str] = field(default_factory=list)
    """The id of each superseded page left alone because its successor cannot be named."""
    evidence_rekeyed: int = 0
    """How many evidence records were moved onto a new content address."""
