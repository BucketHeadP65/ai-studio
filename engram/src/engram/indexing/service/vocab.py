"""Writes a knowledge base's vocabulary into `kb.vocab`.

`load_vocab` is the one public entry point. Its upsert is idempotent, so calling it again is safe.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, Self

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from core.ai.retrieval.contracts import Row

    from engram.kb.contracts import VocabularyIndex


class _SupportsVocabUpsert(Protocol):
    """The vocab repository methods `load_vocab` calls."""

    async def upsert_many(self, rows: Sequence[Row]) -> None: ...


class _VocabUnitOfWork(Protocol):
    """The unit of work `load_vocab` opens: a vocab repository, a commit and an async context."""

    @property
    def vocab(self) -> _SupportsVocabUpsert: ...

    async def commit(self) -> None: ...

    async def __aenter__(self) -> Self: ...

    async def __aexit__(self, *exc: object) -> None: ...


if TYPE_CHECKING:
    type _UoWFactory = Callable[[], _VocabUnitOfWork]


def vocab_rows(vocabulary: VocabularyIndex, collection: str) -> Iterable[Row]:
    """The `kb.vocab` rows of `vocabulary`, one per value, tagged by axis.

    Args:
        vocabulary: The vocabulary index to read.
        collection: The knowledge base the rows belong to.

    Yields:
        One row for each entity type, relation, facet value and claim type.
    """
    for value, definition in vocabulary.entity_types.items():
        yield {"collection": collection, "vocab": "entity_type", "value": value, "definition": definition, "meta": {}}
    for key, spec in vocabulary.relations.items():
        yield {
            "collection": collection,
            "vocab": "relation",
            "value": key,
            "definition": spec.description,
            "meta": {"domain": spec.domain, "range": spec.range},
        }
    for facet, values in vocabulary.facets.items():
        for value, definition in values.items():
            yield {"collection": collection, "vocab": f"facet:{facet}", "value": value, "definition": definition, "meta": {}}
    for value, definition in vocabulary.claim_types.items():
        yield {"collection": collection, "vocab": "kind", "value": value, "definition": definition, "meta": {}}


async def load_vocab(uow_factory: _UoWFactory, vocabulary: VocabularyIndex, collection: str) -> int:
    """Upsert every row of `vocabulary` into `kb.vocab` in one transaction.

    It is idempotent: a row already stored is updated in place.

    Args:
        uow_factory: A zero-argument callable returning an async unit of work with `vocab.upsert_many` and `commit`.
        vocabulary: The vocabulary index whose rows are written.
        collection: The knowledge base the rows belong to.

    Returns:
        The number of rows sent to the upsert, before the database merges duplicates.
    """
    rows = list(vocab_rows(vocabulary, collection))
    async with uow_factory() as uow:
        await uow.vocab.upsert_many(rows)
        await uow.commit()
    return len(rows)
