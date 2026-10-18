"""The embed stage both indexing paths run: it embeds only the rows that have no vector yet.

`pending()` reads the four `embedding IS NULL` queues in one short read transaction and returns plain ids.
The stage does no I/O of its own. It reads and writes through the `EmbedUnitOfWork` factory and the `BatchEmbedder` it is given.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from core.ai.embedding import BatchEmbedder

    from engram.indexing.internal_contracts import EmbedUnitOfWork

# How many embedding inputs go in one embed transaction. Each batch gets its own unit of work, and one batch
# spans several embedding requests so the embedder can run them concurrently.
EMBED_BATCH = 256

# Which table a queued unit belongs to, which picks the repository write its vector goes to.
type EmbedAggregate = Literal["pages", "sections", "claims", "chunks"]


@dataclass(frozen=True, slots=True)
class EmbedUnit:
    """One unit queued for embedding: which table it lives in, its id, and the text.

    `aggregate` picks the repository its vector is written to. `id` is the row's primary key.
    `text` is the exact embed input the load stage built for that row.
    """

    aggregate: EmbedAggregate
    id: str
    text: str


@dataclass(frozen=True, slots=True)
class PendingEmbeds:
    """The ids of the `embedding IS NULL` rows, read in one transaction.

    The three id tuples are the page, section and claim rows needing a vector, in read order.
    `chunks` holds each raw chunk as a `(row id, source_id)` pair.
    """

    page_ids: tuple[str, ...]
    section_ids: tuple[str, ...]
    claim_ids: tuple[str, ...]
    chunks: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class EmbedOutcome:
    """What one embed stage run did: rows given a new vector, inputs that needed none, and the tokens it cost."""

    embedded: int
    reused: int
    input_tokens: int
    """The provider's input token count for the new vectors."""


class EmbedStage:
    """The embed stage both indexing paths run."""

    def __init__(self, uow_factory: Callable[[], EmbedUnitOfWork], embedder: BatchEmbedder) -> None:
        """Build the stage over its unit of work factory and its embedder.

        Args:
            uow_factory: Builds a fresh `EmbedUnitOfWork` per transaction, one for the `pending()`
                read and one per embed batch.
            embedder: Embeds batches of text and names the model recorded in `kb.embed_meta`.
        """
        self._uow_factory = uow_factory
        self._embedder = embedder

    async def pending(self, *, collection: str) -> PendingEmbeds:
        """Read `collection`'s four `embedding IS NULL` queues in one short read transaction.

        Args:
            collection: The collection to read.

        Returns:
            The pending row ids, and each chunk's `source_id`.
        """
        async with self._uow_factory() as uow:
            page_ids = tuple(row.id for row in await uow.pages.rows_needing_embedding(collection=collection))
            section_ids = tuple(row.id for row in await uow.sections.rows_needing_embedding(collection=collection))
            claim_ids = tuple(row.id for row in await uow.claims.rows_needing_embedding(collection=collection))
            chunks = tuple((row.id, row.source_id) for row in await uow.sources.chunks_needing_embedding(collection=collection))
        return PendingEmbeds(page_ids=page_ids, section_ids=section_ids, claim_ids=claim_ids, chunks=chunks)

    async def run(self, embed_inputs: Mapping[str, str], *, collection: str) -> EmbedOutcome:
        """Embed every `collection` row whose `embedding IS NULL`, batch by batch.

        Each batch of `EMBED_BATCH` commits in its own transaction. The embed meta row is written last.

        Args:
            embed_inputs: The embed input text for each row id the load built.
            collection: The collection to embed.

        Returns:
            What the run did, where `reused` counts the inputs that needed no new vector.

        Raises:
            ValueError: When a pending row has no embed input or an empty one.
        """
        return await self._embed(_pending_units(await self.pending(collection=collection), embed_inputs), len(embed_inputs), collection=collection)

    async def run_scoped(self, embed_inputs: Mapping[str, str], *, collection: str) -> EmbedOutcome:
        """Embed only the pending rows `embed_inputs` names, leaving every other pending row alone.

        Use it for a single page write, whose inputs cover only that page's rows.

        Args:
            embed_inputs: The embed input text for each row id this write built.
            collection: The collection to embed.

        Returns:
            What the run did, where `reused` counts the inputs that needed no new vector.
        """
        pending = await self.pending(collection=collection)
        mine = PendingEmbeds(
            page_ids=tuple(unit_id for unit_id in pending.page_ids if unit_id in embed_inputs),
            section_ids=tuple(unit_id for unit_id in pending.section_ids if unit_id in embed_inputs),
            claim_ids=tuple(unit_id for unit_id in pending.claim_ids if unit_id in embed_inputs),
            chunks=tuple((unit_id, key) for unit_id, key in pending.chunks if unit_id in embed_inputs),
        )
        return await self._embed(_pending_units(mine, embed_inputs), len(embed_inputs), collection=collection)

    async def _embed(self, units: Sequence[EmbedUnit], total_loaded: int, *, collection: str) -> EmbedOutcome:
        """Embed `units` batch by batch, write the model contract row last, and say what it did."""
        if not units:
            await self._record_embed_meta()
            return EmbedOutcome(embedded=0, reused=total_loaded, input_tokens=0)

        input_tokens = 0
        for batch in chunked(units, EMBED_BATCH):
            embeddings = await self._embedder.embed_batch([unit.text for unit in batch])
            input_tokens += embeddings.input_tokens
            await self._persist_batch(batch, embeddings.vectors, collection=collection)

        await self._record_embed_meta()
        return EmbedOutcome(embedded=len(units), reused=total_loaded - len(units), input_tokens=input_tokens)

    async def _persist_batch(self, batch: Sequence[EmbedUnit], vectors: Sequence[Sequence[float]], *, collection: str) -> None:
        """Write one batch's vectors, grouped by table, in one short transaction."""
        by_aggregate: dict[EmbedAggregate, dict[str, Sequence[float]]] = {}
        for unit, vector in zip(batch, vectors, strict=True):
            by_aggregate.setdefault(unit.aggregate, {})[unit.id] = vector
        async with self._uow_factory() as uow:
            if pages := by_aggregate.get("pages"):
                await uow.pages.write_embeddings(pages, collection=collection)
            if sections := by_aggregate.get("sections"):
                await uow.sections.write_embeddings(sections, collection=collection)
            if claims := by_aggregate.get("claims"):
                await uow.claims.write_embeddings(claims, collection=collection)
            if chunks := by_aggregate.get("chunks"):
                await uow.sources.write_chunk_embeddings(chunks, collection=collection)
            await uow.commit()

    async def _record_embed_meta(self) -> None:
        """Write the embed meta row (model and dimensions) in its own short transaction."""
        async with self._uow_factory() as uow:
            await uow.meta.set_embed_meta(model=self._embedder.model_id, dim=self._embedder.dimensions)
            await uow.commit()


def _pending_units(pending: PendingEmbeds, embed_inputs: Mapping[str, str]) -> list[EmbedUnit]:
    """Build the queued units: pages, then sections, then claims, then chunks, each in read order."""
    units = [embed_unit("pages", page_id, embed_inputs) for page_id in pending.page_ids]
    units += [embed_unit("sections", section_id, embed_inputs) for section_id in pending.section_ids]
    units += [embed_unit("claims", claim_id, embed_inputs) for claim_id in pending.claim_ids]
    units += [embed_unit("chunks", chunk_id, embed_inputs) for chunk_id, _key in pending.chunks]
    return units


def embed_unit(aggregate: EmbedAggregate, unit_id: str, embed_inputs: Mapping[str, str]) -> EmbedUnit:
    """Build one queued `EmbedUnit`, refusing a missing or empty embed input.

    Args:
        aggregate: The table the row lives in.
        unit_id: The row id.
        embed_inputs: The embed input text for each row id.

    Returns:
        The queued unit.

    Raises:
        ValueError: When `unit_id` has no embed input or an empty one.
    """
    text = embed_inputs.get(unit_id, "")
    if not text:
        msg = f"no embed input for {aggregate} row {unit_id!r}: refusing to embed an empty string"
        raise ValueError(msg)
    return EmbedUnit(aggregate, unit_id, text)


def chunked[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    """Yield `items` in contiguous slices of at most `size`.

    Args:
        items: The items to slice.
        size: The largest slice length.

    Yields:
        Each slice, in order.
    """
    for start in range(0, len(items), size):
        yield items[start : start + size]
