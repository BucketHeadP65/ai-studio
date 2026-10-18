"""A gateway whose `api.about` rows are scripted, for tests that must not touch a database."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

from core.ai.retrieval.contracts import SqlResult

if TYPE_CHECKING:
    from collections.abc import Mapping


@dataclass
class FakeDomainReaderGateway:
    """Answers `rows` with one scripted `api.about` body per collection, and no row for any other.

    Structurally a `DomainReaderGateway`: one capped read is the whole shape `knowledge_base_capability`
    asks of it. Doubles as its own async context manager, so a test hands its constructor straight
    to `knowledge_base_capability`'s `gateway` argument (e.g. `knowledge_base_capability(client, gateway=FakeDomainReaderGateway)`).
    """

    bodies: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, Mapping[str, object] | None]] = field(default_factory=list)

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        """The scripted body for the requested collection, as one row; no row when there is none."""
        del cap
        self.calls.append((sql, params))
        collection = (params or {}).get("collection")
        if isinstance(collection, str) and collection in self.bodies:
            return SqlResult(rows=({"body": self.bodies[collection]},), columns=("body",))
        return SqlResult(rows=(), columns=("body",))

    async def __aenter__(self) -> Self:
        """This fake also serves as the context manager `knowledge_base_capability`'s `gateway()` opens."""
        return self

    async def __aexit__(self, *details: object) -> None:
        """Nothing to dispose; this fake never opens a real connection."""
        del details
