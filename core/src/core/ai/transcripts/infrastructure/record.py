"""The conversation record as a transcript source: `record:<run id>` opens one run.

One `Step` per step the record holds, each message under the ordinal the conversation knows it by, the tool definitions
the request-parameters recorder kept for that step, and the wall time of every tool call from the run's events.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict
from pydantic_ai.messages import ModelRequest, UserPromptPart
from pydantic_ai.models import ModelRequestParameters

from core.ai.history.contracts import reads
from core.ai.transcripts.exceptions import RunEmptyError
from core.ai.transcripts.loaders import scheme_and_locator
from core.ai.transcripts.models import Step, Transcript
from core.ai.transcripts.read_models import RunSummary

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai_harness.step_persistence import RunRecord, StepEvent

    from core.ai.history.contracts import Connection, Database, RecordedStep


def record_source(run_id: str) -> str:
    """The transcript source that names a run of the conversation record.

    Args:
        run_id: The run's id in the record.

    Returns:
        The source, `record:<run id>`.
    """
    return f"record:{run_id}"


class _RecordedRequest(BaseModel):
    """The document the request-parameters recorder writes, the part of it a transcript reads."""

    model_config = ConfigDict(extra="ignore", arbitrary_types_allowed=True)

    request_parameters: ModelRequestParameters
    model: str | None = None


@dataclass(frozen=True)
class RecordTranscriptLoader:
    """Opens a run of the conversation record as a transcript."""

    database: Database

    async def __call__(self, locator: str) -> Transcript:
        """Open the run with id `locator` from the conversation record, one step per step the record holds.

        Args:
            locator: The run id.

        Returns:
            The run as a transcript, with the wall time of every tool call its events recorded.

        Raises:
            LookupError: when the record holds no such run.
            RunEmptyError: when the record holds the run but it recorded no step.
        """
        async with self.database.acquire() as connection:
            found = await reads.run(connection, locator)
            if found is None:
                msg = f"no run {locator!r} in the record"
                raise LookupError(msg)
            steps = await reads.recorded_steps(connection, found)
            if not steps:
                msg = f"run {locator!r} recorded no step"
                raise RunEmptyError(msg)
            events = await reads.events(connection, locator)
            parameters = await reads.step_parameters(connection, locator)
        return Transcript(
            source=record_source(locator), steps=tuple(_step(step, parameters) for step in steps), durations_ms=_durations(events)
        )


PROMPT_CHARS: Final = 200
"""How much of a run's first prompt a summary line carries."""


@dataclass(frozen=True)
class RecordRunsOfConversation:
    """The conversation record's runs: one run's own summary, and the earlier runs of the conversation it belongs to."""

    database: Database

    async def before(self, source: str) -> list[RunSummary]:
        """The earlier runs, read from the conversation record for a `record:` source.

        Args:
            source: The run as a transcript source, `record:<run id>`.

        Returns:
            The runs of its conversation that started before it, newest first. Empty when the source is not a `record:`
            source or the record holds no such run.
        """
        run_id = _run_id_of(source)
        if run_id is None:
            return []
        async with self.database.acquire() as connection:
            found = await reads.run(connection, run_id)
            if found is None:
                return []
            siblings = await reads.run_records(connection, parent_run_id=None, conversation_id=str(found.conversation_id))
            earlier = [other for other in siblings if other.started_at < found.started_at]
            return [await _summary(connection, other) for other in reversed(earlier)]

    async def summary_of(self, source: str) -> RunSummary | None:
        """The run's summary line, read from the conversation record for a `record:` source.

        Args:
            source: The run as a transcript source, `record:<run id>`.

        Returns:
            The run as one line, or None when the source is not a `record:` source or the record holds no such run.
        """
        run_id = _run_id_of(source)
        if run_id is None:
            return None
        async with self.database.acquire() as connection:
            found = await reads.run_record(connection, run_id)
            return None if found is None else await _summary(connection, found)


def _run_id_of(source: str) -> str | None:
    """The run id a `record:` source names, or None for any other source."""
    try:
        scheme, run_id = scheme_and_locator(source)
    except ValueError:
        return None
    return run_id if scheme == "record" else None


async def _summary(connection: Connection, found: RunRecord) -> RunSummary:
    """One run as a line: its source, its agent, when it started, what it was asked first, how and when it ended."""
    steps = await reads.snapshots(connection, found.run_id, include_interrupted=True)
    happened = await reads.events(connection, found.run_id)
    return RunSummary(
        source=record_source(found.run_id),
        agent=found.agent_name,
        started_at=found.started_at,
        first_prompt=_first_prompt(steps[0].messages) if steps else "",
        last_event_type=happened[-1].kind if happened else "no events",
        ended_at=happened[-1].timestamp if happened else None,
    )


def _first_prompt(messages: Sequence[ModelMessage]) -> str:
    """The first thing the user asked, cut short, or an empty string when the step holds no prompt."""
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                return str(part.content)[:PROMPT_CHARS]
    return ""


def _step(recorded: RecordedStep, parameters: Mapping[int, Mapping[str, JsonValue]]) -> Step:
    sent_with = parameters.get(recorded.step_index)
    sent = _RecordedRequest.model_validate(sent_with) if sent_with is not None else None
    return Step(
        messages=tuple(recorded.messages),
        ordinals=recorded.ordinals,
        parameters=sent.request_parameters if sent is not None else None,
        model=sent.model if sent is not None else None,
    )


def _durations(events: Sequence[StepEvent]) -> dict[str, int]:
    started: dict[str, datetime] = {}
    durations: dict[str, int] = {}
    for event in events:
        if event.tool_call_id is None:
            continue
        if event.kind == "tool_call_started":
            started[event.tool_call_id] = event.timestamp
        elif event.kind in {"tool_call_completed", "tool_call_failed"} and event.tool_call_id in started:
            durations[event.tool_call_id] = int((event.timestamp - started.pop(event.tool_call_id)).total_seconds() * 1000)
    return durations
