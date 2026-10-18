"""A capability that records, per model request, the tools the model could call and the settings it was sent with."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.tools import AgentDepsT
from pydantic_ai_harness.step_persistence import StepPersistence
from pydantic_core import to_jsonable_python

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue
    from pydantic_ai import RunContext
    from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
    from pydantic_ai.settings import ModelSettings

    from core.ai.history.internal_contracts import RequestParametersSink


@dataclass
class RequestParametersRecorder(AbstractCapability[AgentDepsT]):
    """Writes the request parameters of every model request to the sink, keyed by run id and step index.

    The parameters are a record for reading, never an input to a run: the agent keeps deciding its tools and settings.

    A sink whose rows reference the record's runs table needs a transcript (step persistence) in scope for the same run. Without
    one, the `RunContext.run_id` fallback names a run nothing registered and the foreign key refuses the write.
    """

    sink: RequestParametersSink

    async def before_model_request(self, ctx: RunContext[AgentDepsT], request_context: ModelRequestContext) -> ModelRequestContext:
        """Record what this request is about to be sent with, then let it go out unchanged.

        The row is keyed by the run the transcript registered when a transcript is in scope, so the transcript's join on
        `(run_id, step_index)` holds whichever id scheme the persistence capability keys its own rows by.

        Args:
            ctx: The run's context, which carries the step number.
            request_context: The request about to be sent.

        Returns:
            The request context, unchanged.

        Raises:
            RuntimeError: when the run has no id to key the record by.
        """
        run_id = _transcript_run_id(ctx) or ctx.run_id
        if run_id is None:
            msg = "the request-parameters recorder needs a run id from the transcript or from the agent graph"
            raise RuntimeError(msg)
        parameters: dict[str, JsonValue] = {
            "model": request_context.model.model_name,
            "provider": request_context.model.system,
            "model_settings": _model_settings(request_context.model_settings),
            "request_parameters": _request_parameters(request_context.model_request_parameters),
        }
        await self.sink.record_request_parameters(run_id=run_id, step_index=ctx.run_step, parameters=parameters)
        return request_context


def _transcript_run_id(ctx: RunContext[AgentDepsT]) -> str | None:
    """The run id the run's step persistence registered, or `None` when no step persistence is mounted."""
    for capability in ctx.capabilities.values():
        if isinstance(capability, StepPersistence):
            return capability.run_id
    return None


def _request_parameters(parameters: ModelRequestParameters) -> dict[str, JsonValue]:
    """The request parameters as JSON, field by field when the whole object holds something that will not serialize."""
    _, rendered = _jsonable(parameters)
    if isinstance(rendered, dict):
        return rendered
    return _field_by_field(vars(parameters))


def _model_settings(settings: ModelSettings | None) -> JsonValue:
    """The model settings as JSON, setting by setting when one of them will not serialize."""
    serialized, rendered = _jsonable(settings)
    if serialized:
        return rendered
    return _field_by_field(settings or {})


def _field_by_field(values: Mapping[str, object]) -> dict[str, JsonValue]:
    """Each value on its own, leaving out the ones nothing could turn into JSON."""
    rendered: dict[str, JsonValue] = {}
    for name, value in values.items():
        serialized, jsonable = _jsonable(value)
        if serialized:
            rendered[name] = jsonable
    return rendered


def _jsonable(value: object) -> tuple[bool, JsonValue]:
    """Whether the value could be turned into JSON, and what it turned into when it could."""
    try:
        return True, to_jsonable_python(value)
    except (TypeError, ValueError):
        return False, None
