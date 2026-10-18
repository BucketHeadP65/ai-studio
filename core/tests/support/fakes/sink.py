"""A request-parameters sink that remembers every call."""

from collections.abc import Mapping
from dataclasses import dataclass, field

from pydantic import JsonValue


@dataclass
class RecordedParameters:
    """One call to the sink."""

    run_id: str
    step_index: int
    parameters: Mapping[str, JsonValue]


@dataclass
class FakeSink:
    """Records what the recorder hands it, in order."""

    calls: list[RecordedParameters] = field(default_factory=list)

    async def record_request_parameters(self, *, run_id: str, step_index: int, parameters: Mapping[str, JsonValue]) -> None:
        self.calls.append(RecordedParameters(run_id=run_id, step_index=step_index, parameters=parameters))
