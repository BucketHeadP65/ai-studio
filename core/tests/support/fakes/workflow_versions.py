"""Every saved version of a workflow name, as a library that keeps versions answers it."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeWorkflowVersions:
    """`WorkflowVersionQueries` over a scriptable mapping: each name's `(version, id)` pairs, lowest first."""

    by_name: dict[str, tuple[tuple[int, str], ...]] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)
    """Every name `versions` was asked about, in order."""

    async def versions(self, name: str) -> tuple[tuple[int, str], ...]:
        """The pairs stocked under `name`, or none."""
        self.asked.append(name)
        return self.by_name.get(name, ())
