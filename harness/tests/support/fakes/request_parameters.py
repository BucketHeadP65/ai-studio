"""The record's request-parameters sink, without a database behind it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.history.contracts import RequestParametersSink
    from pydantic import JsonValue


@dataclass(frozen=True)
class RecordedParameters:
    """One request's parameters as the sink received them."""

    run_id: str
    step_index: int
    parameters: Mapping[str, JsonValue]


@dataclass
class FakeRequestParametersSink:
    """Keeps every call in order, so a test reads back what a turn sent."""

    recorded: list[RecordedParameters] = field(default_factory=list)

    async def record_request_parameters(self, *, run_id: str, step_index: int, parameters: Mapping[str, JsonValue]) -> None:
        """Remember one request's parameters."""
        self.recorded.append(RecordedParameters(run_id=run_id, step_index=step_index, parameters=parameters))


_proof: type[RequestParametersSink] = FakeRequestParametersSink
"""Pyright fails this module the moment `FakeRequestParametersSink` drifts from the `RequestParametersSink` port."""
