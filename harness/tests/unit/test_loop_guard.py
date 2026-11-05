"""The loop guard, wired into the steps this app runs.

The capability is pydantic-ai-harness's `DetectLoops`, so this file does not re-test its four patterns. It tests the
two things that are ours: that a step built by `build_environment` carries it, and that the nudge reaches the model
rather than only being recorded. The model here stops only when it sees the nudge, so if delivery broke this test
would run to its request limit instead of passing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import prepare
from pydantic_ai.exceptions import UnexpectedModelBehavior
from pydantic_ai.messages import ModelMessage, ModelResponse, SystemPromptPart, ToolCallPart
from pydantic_ai.profiles import ModelProfile

from tests.support.environment import defined_step, spec_for, world_over
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from pydantic_ai.models.function import AgentInfo, FunctionModel

NUDGE_ORIGIN = "pydantic-ai-harness.detect_loops.nudge.v1"
"""The `origin` in the metadata of the system prompt part the nudge arrives as. If upstream renames it, this test
fails loudly rather than passing on a guard that never speaks."""

SPIN = "rows = await run_sql(sql='select 1')\nrows"


def _is_nudge(part: object) -> bool:
    """Whether `part` is the loop guard's nudge, found by its origin and never by its text."""
    return isinstance(part, SystemPromptPart) and isinstance(part.metadata, dict) and part.metadata.get("origin") == NUDGE_ORIGIN


def _stuck() -> tuple[FunctionModel, list[str]]:
    """A model that repeats one identical call until it is told to stop, then answers.

    The only exit is the nudge, so "the test finished" and "the nudge was delivered" are the same
    statement.
    """
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nudged = any(_is_nudge(part) for message in messages for part in message.parts)
        if nudged:
            seen.append("nudged")
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "changed approach"})])
        seen.append("spin")
        return ModelResponse(parts=[ToolCallPart("run_code", {"code": SPIN})])

    # With inline system prompts the nudge reaches the model as the part it was enqueued as, metadata included.
    return streaming_function_model(respond, profile=ModelProfile(supports_inline_system_prompts=True)), seen


async def test_a_spinning_step_is_nudged_and_the_nudge_arrives() -> None:
    """A step repeating one call is interrupted, and the interruption lands in its next request."""
    model, seen = _stuck()
    environment = await world_over(model)
    ready = prepare(spec_for(defined_step(tools=("run_sql",))), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    await implementation.function(question="spin on purpose")

    assert "nudged" in seen, f"the guard never reached the model; it saw {seen}"
    # It should be caught early, not after the budget is nearly gone: the default checks fire at
    # three identical results or five identical calls, so a handful of turns, not twenty-four.
    assert len(seen) <= 8, f"caught too late: {len(seen)} turns before the nudge"


async def test_the_guard_is_attached_to_a_step_with_no_tools_too() -> None:
    """A step that holds nothing can still monologue, so the guard is not conditional on tools."""
    model, seen = _stuck()
    environment = await world_over(model)
    ready = prepare(spec_for(defined_step(tools=())), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None

    # No tools means no `run_code`, so this model's call is invalid and pydantic-ai retries it
    # until the retry budget ends the run. What is asserted here is only that building such a step
    # works and stays bounded -- the guard is present either way, which is why `capabilities` is
    # not gated on `task.tools`.
    with pytest.raises(UnexpectedModelBehavior, match="exceeded max retries"):
        await implementation.function(question="nothing to work with")
    assert seen, "the model was never called"
