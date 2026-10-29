"""Writes proposed disclosure summaries at index time through a `Summarizer` port.

`SummaryStage` writes a `status='pending'` row into `kb.unit_summaries` for every page and section of a collection
whose `body_hash` has no `active` or `pending` summary yet.
Nothing it writes is disclosed until `studio summaries approve` promotes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from engram.indexing.internal_contracts import Summarizer, SummaryUnitOfWork


class SummaryStage:
    """Writes `pending` disclosure summaries for a collection's units that lack a current one."""

    def __init__(self, uow_factory: Callable[[], SummaryUnitOfWork], summarizer: Summarizer) -> None:
        """Create the stage over its unit of work factory and its summarizer.

        Args:
            uow_factory: Builds the one `SummaryUnitOfWork` a run reads, writes and commits through.
            summarizer: Generates each unit's summary text and names the model recorded on the row.
        """
        self._uow_factory = uow_factory
        self._summarizer = summarizer

    async def run(self, collection: str) -> int:
        """Summarize every unit `collection` has pending and write each as a `status='pending'` row.

        It never promotes a summary to `active`. The run commits once, at the end, and closes its unit of work whatever happens.

        Args:
            collection: The knowledge base whose units are summarized.

        Returns:
            The number of summaries written.
        """
        uow = self._uow_factory()
        try:
            units = await uow.pending_units(collection)
            written = 0
            for unit in units:
                summary = await self._summarizer.summarize(title=unit.title, body=unit.body)
                await uow.upsert_summary(
                    {
                        "unit_id": unit.unit_id,
                        "unit_kind": unit.unit_type,
                        "collection": collection,
                        "summary": summary,
                        "status": "pending",
                        "model": self._summarizer.model_id,
                        "source_hash": unit.source_hash,
                    }
                )
                written += 1
            await uow.commit()
        finally:
            await uow.close()
        return written


