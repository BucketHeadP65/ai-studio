"""The retrieval switch stage: measures a collection's signals, decides its switches and stores them.

`SwitchStage` decides the embeddings, SQL and graph switches only for a `mode: "auto"` retrieval spec.
A `mode: "fixed"` spec keeps its stored switches exactly as they are.
On both modes the stage stores the schema's `retrieval.private` flag on the fingerprint.
It writes nothing when the stored flag already agrees. Given `ReaderScopes`, it then gives a private knowledge base
its own reader role, or takes the scope of a shared one away.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.indexing.domain.switches import decide_switches

if TYPE_CHECKING:
    from collections.abc import Callable

    from engram.indexing.domain.switches import RetrievalSwitches
    from engram.indexing.internal_contracts import ReaderScopes, SwitchUnitOfWork
    from engram.kb.contracts import KnowledgeBaseSchema


class SwitchStage:
    """Measures a collection's signals, decides its retrieval switches and stores them on the fingerprint.

    You must run it only after rows were written, by a real index or a page write, never on a dry run.
    """

    def __init__(self, uow_factory: Callable[[], SwitchUnitOfWork], reader_scopes: ReaderScopes | None = None) -> None:
        """Create the stage over its unit of work factory.

        Args:
            uow_factory: Builds a fresh unit of work for each read and each write.
            reader_scopes: Gives a private knowledge base its reader role after each run, or None to leave roles alone.
        """
        self._uow_factory = uow_factory
        self._reader_scopes = reader_scopes

    async def run(self, *, collection: str, spec: KnowledgeBaseSchema) -> RetrievalSwitches | None:
        """Decide and store `collection`'s switches when `spec` is in auto mode.

        The decision honours `spec.retrieval`. Its `floor` forces the named switches on, and its `ceiling`,
        when given, forces every other switch off.
        A fixed-mode spec decides nothing but still stores its `retrieval.private` flag when the stored flag differs.

        Args:
            collection: The knowledge base whose switches are decided.
            spec: The knowledge base's schema, whose `retrieval` section sets the mode and the pins.

        Returns:
            The switches stored, or `None` for a fixed-mode spec.
        """
        if spec.retrieval.mode != "auto":
            await self._persist_privacy_flag(collection=collection, spec=spec)
            await self._scope_reader(collection=collection, spec=spec)
            return None
        async with self._uow_factory() as uow:
            current = await uow.fingerprint.get_switches(collection)
            signals = await uow.signals.measure(collection)
        decided = decide_switches(
            signals,
            current,
            floor=frozenset(spec.retrieval.floor),
            ceiling=frozenset(spec.retrieval.ceiling) if spec.retrieval.ceiling is not None else None,
        ).model_copy(update={"private": spec.retrieval.private})
        async with self._uow_factory() as uow:
            await uow.fingerprint.set_switches(collection=collection, switches=decided)
            await uow.commit()
        await self._scope_reader(collection=collection, spec=spec)
        return decided

    async def _scope_reader(self, *, collection: str, spec: KnowledgeBaseSchema) -> None:
        """Give a private knowledge base its reader role, or take a shared one's scope away."""
        if self._reader_scopes is not None:
            await self._reader_scopes.scope(collection, private=spec.retrieval.private)

    async def _persist_privacy_flag(self, *, collection: str, spec: KnowledgeBaseSchema) -> None:
        """Store `spec.retrieval.private` on the switches, writing nothing when it already matches."""
        async with self._uow_factory() as uow:
            current = await uow.fingerprint.get_switches(collection)
        if current.private == spec.retrieval.private:
            return
        async with self._uow_factory() as uow:
            await uow.fingerprint.set_switches(collection=collection, switches=current.model_copy(update={"private": spec.retrieval.private}))
            await uow.commit()
