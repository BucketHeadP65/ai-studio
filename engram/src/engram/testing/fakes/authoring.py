"""Fakes for the authoring tools: a recorded reindex and a scripted admin gateway."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from core.ai.retrieval.contracts import SchemaDescription, SqlResult

from engram.authoring.contracts import ReindexFinding, ReindexResult

if TYPE_CHECKING:
    from collections.abc import Mapping

    from engram.authoring.contracts import AuthoringKnowledgeBase


@dataclass
class FakeReindex:
    """Records every knowledge base it was asked to reindex, answering with a scripted `ReindexResult`.

    `outcome` scripts what every call answers: `"clean"` (the default), `"validation_errors"` (read
    `findings`/`findings_total`), `"not_indexed"` (read `detail`), or `"deferred"`. The knowledge base
    is always recorded first, so a test can assert the attempt happened even under a `"not_indexed"` script.
    """

    knowledge_bases: list[AuthoringKnowledgeBase] = field(default_factory=list)
    outcome: Literal["clean", "validation_errors", "not_indexed", "deferred"] = "clean"
    detail: str = ""
    findings: tuple[ReindexFinding, ...] = ()
    findings_total: int = 0

    async def __call__(self, knowledge_base: AuthoringKnowledgeBase) -> ReindexResult:
        """Record the knowledge base and answer the scripted outcome.

        Args:
            knowledge_base: The knowledge base to index again.

        Returns:
            A result of the scripted `outcome`.
        """
        self.knowledge_bases.append(knowledge_base)
        if self.outcome == "not_indexed":
            return ReindexResult(outcome="not_indexed", detail=self.detail or "reindex failed")
        if self.outcome == "deferred":
            return ReindexResult(outcome="deferred", detail=self.detail or f"{knowledge_base.path} is indexed when the run ends")
        if self.outcome == "validation_errors":
            return ReindexResult(
                outcome="validation_errors",
                detail=self.detail or f"reindexed {knowledge_base.path} with {self.findings_total} validation error(s)",
                findings=self.findings,
                findings_total=self.findings_total,
            )
        return ReindexResult(outcome="clean", detail=self.detail or f"reindexed {knowledge_base.path}")


@dataclass
class FakeAdminGateway:
    """Answers every `rows` call with the scripted rows and records the SQL and params it saw.

    `rows_queue`, when given, answers one call at a time in call order. Once it runs out, or when
    it was never given, every call answers the static `rows_out`.
    """

    rows_out: list[dict[str, object]] = field(default_factory=list)
    calls: list[tuple[str, Mapping[str, object]]] = field(default_factory=list)

    def __init__(self, rows: list[dict[str, object]] | None = None, *, rows_queue: list[list[dict[str, object]]] | None = None) -> None:
        """Script the answers.

        Args:
            rows: The rows every call answers once the queue is empty.
            rows_queue: Rows for one call each, answered in call order before `rows`.
        """
        self.rows_out = rows or []
        self.calls = []
        self._rows_queue = list(rows_queue) if rows_queue is not None else []

    async def rows(self, sql: str, params: Mapping[str, object] | None = None, *, cap: int = 50) -> SqlResult:
        """Record the query and answer the next scripted rows.

        Args:
            sql: The SQL, recorded.
            params: The bound parameters, recorded as an empty mapping when absent.
            cap: Ignored.

        Returns:
            The next queued rows, or `rows_out` when the queue is empty.
        """
        del cap
        self.calls.append((sql, params or {}))
        if self._rows_queue:
            return SqlResult(rows=tuple(self._rows_queue.pop(0)))
        return SqlResult(rows=tuple(self.rows_out))

    async def api_objects(self) -> SchemaDescription:
        """Describe an empty schema.

        Returns:
            A description with no objects.
        """
        return SchemaDescription(objects=())
