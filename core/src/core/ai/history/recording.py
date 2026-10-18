"""The two capabilities that record an agent's run in the conversation record: its steps and what each request carried."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai_harness.step_persistence import StepPersistence

from core.ai.history.request_parameters_recorder import RequestParametersRecorder

if TYPE_CHECKING:
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai_harness.step_persistence import StepStore

    from core.ai.history.internal_contracts import RequestParametersSink


def recording_capabilities[DepsT](
    store: StepStore, parameters: RequestParametersSink, *, agent_name: str, deps_type: type[DepsT]
) -> tuple[AbstractCapability[DepsT], ...]:
    """The capabilities that write one agent's steps to `store` and each request's parameters to `parameters`.

    No parent run is set, so the run belongs to its own conversation.

    Args:
        store: Where each step of the run is written.
        parameters: Where the parameters of each request are written.
        agent_name: The agent's name, recorded on every run.
        deps_type: The agent's dependencies type, which the capabilities are typed over.

    Returns:
        The step recorder, then the request parameters recorder.
    """
    del deps_type
    return (StepPersistence[DepsT](store=store, agent_name=agent_name), RequestParametersRecorder[DepsT](sink=parameters))
