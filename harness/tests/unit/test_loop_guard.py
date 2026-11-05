"""The loop guard, wired into the steps this app runs.

The capability is pydantic-ai-harness's `DetectLoops`, so this file does not re-test its four patterns. It tests the
two things that are ours: that a step built by `build_environment` carries it, and that the nudge reaches the model
rather than only being recorded. Each model here stops only when it sees the nudge, so if delivery broke a test would
run to its request limit instead of passing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import prepare
from pydantic_ai.messages import ModelMessage, ModelResponse, SystemPromptPart, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.profiles import ModelProfile

from tests.support.environment import defined_step, spec_for, world_over
from tests.support.streaming import streaming_function_model

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_ai.messages import ModelRequestPart, ModelResponsePart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

NUDGE_ORIGIN = "pydantic-ai-harness.detect_loops.nudge.v1"
"""The `origin` in the metadata of the system prompt part the nudge arrives as. If upstream renames it, these tests
fail loudly rather than passing on a guard that never speaks."""

INLINE = ModelProfile(supports_inline_system_prompts=True)
"""A profile under which a mid-run system prompt reaches the model as the part it was enqueued as, metadata included."""

SPIN = "rows = await run_sql(sql='select 1')\nrows"

MONOLOGUE = "I will look into the question now."


def _enqueued_nudge(part: ModelRequestPart | ModelResponsePart) -> str | None:
    """The nudge's text when `part` is the nudge as enqueued, found by its origin and never by its text."""
    if isinstance(part, SystemPromptPart) and isinstance(part.metadata, dict) and part.metadata.get("origin") == NUDGE_ORIGIN:
        return part.content
    return None


def _wrapped_nudge(part: ModelRequestPart | ModelResponsePart) -> str | None:
    """The text of a `<system>`-tagged user prompt, the only form a mid-run system prompt takes without inline support."""
    if isinstance(part, UserPromptPart) and isinstance(part.content, str) and part.content.startswith("<system>"):
        return part.content
    return None


def _stuck(
    repeat: Callable[[], ModelResponse], nudge_in: Callable[[ModelRequestPart | ModelResponsePart], str | None], *, profile: ModelProfile | None
) -> tuple[FunctionModel, list[str]]:
    """A model that answers `repeat()` until `nudge_in` finds the nudge in its request, then calls the output tool.

    The returned list records `spin` for each repeated answer and the nudge's text for the answer it ended on.
    """
    seen: list[str] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        nudges = [text for message in messages for part in message.parts if (text := nudge_in(part)) is not None]
        if nudges:
            seen.append(nudges[-1])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "changed approach"})])
        seen.append("spin")
        return repeat()

    return streaming_function_model(respond, profile=profile), seen


def _same_call() -> ModelResponse:
    return ModelResponse(parts=[ToolCallPart("run_code", {"code": SPIN})])


def _same_text() -> ModelResponse:
    return ModelResponse(parts=[TextPart(MONOLOGUE)])


async def _run_step(model: FunctionModel, *, tools: tuple[str, ...]) -> None:
    """Build the one-step workflow over `model`, holding `tools`, and run its step once."""
    environment = await world_over(model)
    ready = prepare(spec_for(defined_step(tools=tools)), environment)
    assert ready.report.ok, ready.report.issues
    implementation = ready.registry.get("dig")
    assert implementation is not None
    await implementation.function(question="spin on purpose")


async def test_a_spinning_step_is_nudged_and_the_nudge_arrives() -> None:
    """A step repeating one call is interrupted, and the interruption lands in its next request."""
    model, seen = _stuck(_same_call, _enqueued_nudge, profile=INLINE)

    await _run_step(model, tools=("run_sql",))

    assert seen[-1] != "spin", f"the guard never reached the model, it saw {seen}"
    # It should be caught early, not after the budget is nearly gone: the default checks fire at
    # three identical results or five identical calls, so a handful of turns, not twenty-four.
    assert len(seen) <= 8, f"caught too late: {len(seen)} turns before the nudge"


async def test_without_inline_system_prompts_the_nudge_arrives_as_system_tagged_user_text() -> None:
    """On a model with no mid-run system role, the same nudge text reaches it inside a `<system>` tag."""
    inline_model, inline_seen = _stuck(_same_call, _enqueued_nudge, profile=INLINE)
    await _run_step(inline_model, tools=("run_sql",))
    wrapped_model, wrapped_seen = _stuck(_same_call, _wrapped_nudge, profile=None)
    await _run_step(wrapped_model, tools=("run_sql",))

    assert inline_seen[:-1] == wrapped_seen[:-1], "both models should be nudged after the same turns"
    assert wrapped_seen[-1] == f"<system>{inline_seen[-1]}</system>"


async def test_a_step_with_no_tools_that_repeats_one_text_is_nudged_too() -> None:
    """A step that holds no tools can still answer the same text again and again, so the guard does not depend on tools."""
    model, seen = _stuck(_same_text, _enqueued_nudge, profile=INLINE)

    await _run_step(model, tools=())

    assert seen[-1] != "spin", f"the guard never reached the model, it saw {seen}"
    assert seen.count("spin") == 3, f"the default check fires at three identical texts, it saw {seen}"
