"""An in-memory :class:`~core.ai.retrieval.contracts.SqlGateway` for unit tests.

:class:`FakeSqlGateway` records every ``rows`` call (the SQL, the bound `params` and the row
`cap`) and every `api_objects` call with its `names` filter, then answers canned results.

Assign `result` to set what `rows` answers and `schema` to set what `api_objects` answers. A
`names` filter narrows `schema.objects` the way the real gateway does, matched against each
object's name with its `api.` prefix stripped. `rows_calls`, `api_objects_calls` and
`api_objects_names_calls` are public recorders. To answer different queries differently, script
`routed`: the first key found in the SQL picks that entry's result, and anything unrouted answers
`result`.

`for_collection` records the collection on `collection` and answers the same instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from core.ai.retrieval.contracts import SchemaDescription, SqlResult

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass(frozen=True)
class RecordedQuery:
    """One captured :meth:`FakeSqlGateway.rows` invocation."""

    sql: str
    params: Mapping[str, object]
    cap: int


@dataclass
class FakeSqlGateway:
    """Records SQL and params and answers the scripted :class:`SqlResult` and :class:`SchemaDescription`."""

    result: SqlResult = field(default_factory=lambda: SqlResult(rows=(), columns=()))
    schema: SchemaDescription = field(default_factory=lambda: SchemaDescription(objects=()))
    routed: dict[str, SqlResult] = field(default_factory=dict)
    rows_calls: list[RecordedQuery] = field(default_factory=list)
    api_objects_calls: int = 0
    api_objects_names_calls: list[tuple[str, ...]] = field(default_factory=list)
    collection: str | None = None

    def for_collection(self, collection: str) -> Self:
        """Record the collection the caller reads as, and answer this same fake.

        Args:
            collection: The collection, recorded on `collection`.

        Returns:
            This fake, so every later call is recorded on the instance the test holds.
        """
        self.collection = collection
        return self

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        """Record the query and answer the scripted result.

        Args:
            sql: The SQL, recorded.
            params: The bound parameters, recorded as an empty mapping when absent.
            cap: The row cap, recorded.

        Returns:
            The `routed` result whose key appears first in the SQL, else `result`.
        """
        self.rows_calls.append(RecordedQuery(sql=sql, params=dict(params or {}), cap=cap))
        for needle, scripted in self.routed.items():
            if needle in sql:
                return scripted
        return self.result

    async def api_objects(self, names: tuple[str, ...] = ()) -> SchemaDescription:
        """Record the call and answer the scripted schema.

        Args:
            names: Object names without the `api.` prefix to narrow to. Empty answers every object.

        Returns:
            `schema`, narrowed to `names` when given.
        """
        self.api_objects_calls += 1
        self.api_objects_names_calls.append(names)
        if not names:
            return self.schema
        wanted = set(names)
        return SchemaDescription(objects=tuple(obj for obj in self.schema.objects if obj.name.removeprefix("api.") in wanted))

    @property
    def last(self) -> RecordedQuery:
        """The most recent recorded `rows` call."""
        return self.rows_calls[-1]
