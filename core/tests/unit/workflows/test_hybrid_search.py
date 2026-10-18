"""The store ranks with vectors when it is given an embedder, and lexically when it is not.

The pages this store writes are embedded on the way in -- engram's ingest embeds by default and
this app never asks it not to -- so the vectors exist and the only question is whether the query
carries one. A search that never sends a query vector is asking a hybrid index a lexical question
and paying for the half it does not use.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import LibraryCollection
from core.ai.workflows.infrastructure.library_collection import SEARCH_SQL, halfvec_literal

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


class FakeRows:
    """Records every statement and its arguments; answers with rows a test scripted."""

    def __init__(self, *, rows: Sequence[Mapping[str, Any]] = ()) -> None:
        self.rows = rows
        self.sent: list[tuple[str, tuple[object, ...]]] = []

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        self.sent.append((sql, args))
        return self.rows

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        self.sent.append((sql, args))
        return None


async def _never_ingest(payload: Mapping[str, Any], *, collection: str) -> None:
    """Search never writes; a store still needs a write side to exist."""
    raise AssertionError((payload, collection))


class FakeEmbedder:
    """One recorded call, one scripted vector."""

    def __init__(self, vector: Sequence[float] = (0.5, -0.25)) -> None:
        self.vector = vector
        self.asked: list[str] = []

    async def __call__(self, text: str) -> Sequence[float]:
        self.asked.append(text)
        return self.vector


def _vector_argument(sent: tuple[str, tuple[object, ...]]) -> object:
    """The `query_vec` slot of a recorded `api.search` call."""
    _, args = sent
    return args[2]


async def test_without_an_embedder_the_query_vector_is_null() -> None:
    """No embedder is a configuration, not a fault: `query_vec IS NULL` is the BM25-only leg."""
    rows = FakeRows()
    store = LibraryCollection(rows, _never_ingest, collection="workflows")

    await store.search("audit a claim")

    assert _vector_argument(rows.sent[0]) is None, "a store with no embedder must ask the lexical question"


async def test_an_embedder_makes_the_query_carry_its_vector() -> None:
    """The stored vectors are only reachable if the query brings one of its own."""
    rows = FakeRows()
    embedder = FakeEmbedder([0.5, -0.25])
    store = LibraryCollection(rows, _never_ingest, collection="workflows", embed=embedder)

    await store.search("audit a claim")

    assert embedder.asked == ["audit a claim"], "the query is embedded once, exactly as asked"
    assert _vector_argument(rows.sent[0]) == "[0.5,-0.25]", "the vector travels as a halfvec text literal"


async def test_the_vector_is_cast_through_text() -> None:
    """The intermediate text cast pins the driver's parameter type.

    Binding straight to `halfvec` makes asyncpg infer that type for the parameter and hand the
    string to a codec it has no encoder for; casting through `text` lets Postgres parse the
    literal instead, and leaves NULL meaning NULL.
    """
    assert "cast($3 as text)::halfvec" in SEARCH_SQL.lower(), "the query_vec bind must reach Postgres as text"


async def test_a_broken_embedder_is_raised_not_swallowed() -> None:
    """A silently lexical store looks like a store with bad recall, and stays that way.

    Degrading quietly would hide a broken embedder behind rankings nobody can tell are worse, so
    the failure travels. Choosing lexical is done by passing no embedder, never by one failing.
    """

    async def _broken(text: str) -> Sequence[float]:
        del text
        msg = "the embedding deployment is unreachable"
        raise RuntimeError(msg)

    store = LibraryCollection(FakeRows(), _never_ingest, collection="workflows", embed=_broken)

    with pytest.raises(RuntimeError, match="unreachable"):
        await store.search("audit a claim")


def test_the_literal_round_trips_full_precision() -> None:
    """A truncated component is a different vector; pgvector reads the text we print."""
    assert halfvec_literal([0.1, 2.0, -3.5e-07]) == "[0.1,2.0,-3.5e-07]"
