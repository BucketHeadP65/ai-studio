"""`Telemetry` with a scripted answer, so a report is tested on what it asks and how it folds, never on a network."""

from __future__ import annotations

from harness.adapters.logfire_query import QueryResult, TelemetryUnreachable


class FakeTelemetry:
    """Answers `result` to every query, or raises `failure`, and remembers every SQL it was asked."""

    def __init__(self, result: QueryResult | None = None, failure: TelemetryUnreachable | None = None) -> None:
        self.asked: list[str] = []
        self.result = result if result is not None else QueryResult(columns=[])
        self.failure = failure

    async def query(self, sql: str) -> QueryResult:
        """Record `sql`, then answer as scripted."""
        self.asked.append(sql)
        if self.failure is not None:
            raise self.failure
        return self.result
