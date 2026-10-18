"""Fake SummaryUnitOfWork: records every summary upsert, `pending_units` returns the scripted units."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from engram.indexing.contracts import SummaryUnit, SummaryUpsertRow


class FakeSummaryUow:
    """Records every summary upsert. `pending_units` returns the scripted `units` untouched."""

    def __init__(self, units: list[SummaryUnit]) -> None:
        """Script the pending units.

        Args:
            units: The units `pending_units` returns.
        """
        self.units = units
        self.upserts: list[SummaryUpsertRow] = []
        self.committed = False

    async def pending_units(self, collection: str) -> list[SummaryUnit]:
        """Return the scripted `units` for any collection.

        Args:
            collection: The collection, which is ignored.

        Returns:
            The scripted units.
        """
        del collection
        return self.units

    async def upsert_summary(self, row: SummaryUpsertRow) -> None:
        """Record `row` in call order.

        Args:
            row: The summary row to record.
        """
        self.upserts.append(row)

    async def commit(self) -> None:
        """Record that the run committed."""
        self.committed = True
