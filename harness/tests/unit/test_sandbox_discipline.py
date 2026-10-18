"""The sandbox's refusal of a name it does not hold says what to do, instead of a bare `Unknown function`.

Watched live: the studio agent built twelve edit ops as a variable inside `run_code`, then called
`adapt_workflow` (a direct tool) from the same program three times, read `NameError: Unknown
function: adapt_workflow` three times, and burned the tool's retry budget without ever learning
that a program cannot hand a variable to a direct tool.

These tests drive `wrap_tool_execute` with the exception shapes the tool manager actually hands
it. The first version of this guard sat on `on_tool_execute_error` and was tested by calling the
hook directly with a raw `ModelRetry` -- which the manager never routes there (retry signals are
control flow to it), so the guard was green in the suite and dead on the live path, and the same
incident happened again with the hint nowhere in the model's history.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic_ai.exceptions import ModelRetry, ToolRetryError, UnexpectedModelBehavior
from pydantic_ai.messages import RetryPromptPart, ToolCallPart
from pydantic_ai.tools import ToolDefinition

from harness.domain.ports import SANDBOX
from harness.sandbox import SandboxDiscipline

_REFUSAL = 'Runtime error:\nTraceback (most recent call last):\n  File "<python-input-5>", line 1, in <module>\n    res = await adapt_workflow(ops=ops)\nNameError: Unknown function: adapt_workflow'

_TYPE_CHECK_REFUSAL = (
    "Type error in code:\nerror[unresolved-reference]: Name `adapt_workflow` used when not defined\n  --> main.py:24:17\n"
    "   |\n24 |     res = await adapt_workflow(workflow_id='wf.research-compound.v6', ops=ops)\n   |                 ^^^^^^^^^^^^^^\n"
)

_CEILING = "Tool 'run_code' exceeded max retries count of 3. Consider raising the retry limit."


async def _wrapped(tool: str, error: Exception) -> Exception | object:
    """What the discipline does with a handler that fails this way: the exception it raised, or the result it returned."""

    async def failing_handler(args: dict[str, Any]) -> object:
        del args
        raise error

    try:
        return await SandboxDiscipline[Any]().wrap_tool_execute(
            cast("Any", None),
            call=ToolCallPart(tool_name=tool, args={}),
            tool_def=ToolDefinition(name=tool),
            args={},
            handler=failing_handler,
        )
    except Exception as raised:  # noqa: BLE001 -- the hook's contract is to raise or return, and the test reads which
        return raised


def _retry(message: str) -> ToolRetryError:
    """The shape the tool manager hands the wrap hook: the tool's `ModelRetry`, counted and wrapped."""
    return ToolRetryError(RetryPromptPart(content=message, tool_name=SANDBOX))


async def test_an_unknown_function_refusal_names_the_way_out() -> None:
    raised = await _wrapped(SANDBOX, _retry(_REFUSAL))

    assert isinstance(raised, ToolRetryError), "rewritten in the shape it arrived in, so the counted retry is not counted again"
    content = raised.tool_retry.content
    assert isinstance(content, str)
    assert content.startswith(_REFUSAL), "the sandbox's own words stay first"
    assert "`adapt_workflow` is not a function inside the sandbox" in content
    assert "call it as its own tool call" in content
    assert "cannot hand a variable" in content


async def test_the_type_checkers_refusal_of_the_same_name_gets_the_same_hint() -> None:
    """A fresh program is type-checked before it runs, and that voice refused `adapt_workflow` three times live with no hint."""
    raised = await _wrapped(SANDBOX, _retry(_TYPE_CHECK_REFUSAL))

    assert isinstance(raised, ToolRetryError)
    content = raised.tool_retry.content
    assert isinstance(content, str)
    assert content.startswith(_TYPE_CHECK_REFUSAL)
    assert "`adapt_workflow` is not a function inside the sandbox" in content


async def test_the_unwrapped_voice_of_the_same_refusal_gets_the_same_hint() -> None:
    """A caller that runs tools without retry wrapping hands the raw `ModelRetry`; the hint rides it the same way."""
    raised = await _wrapped(SANDBOX, ModelRetry(_REFUSAL))

    assert isinstance(raised, ModelRetry)
    assert raised.message.startswith(_REFUSAL)
    assert "`adapt_workflow` is not a function inside the sandbox" in raised.message


async def test_any_other_sandbox_refusal_passes_through_untouched() -> None:
    original = _retry("Syntax error in code:\n  something else entirely")
    assert await _wrapped(SANDBOX, original) is original


async def test_the_retry_ceiling_comes_back_as_a_result_not_a_dead_turn() -> None:
    """Live, the fourth refusal raised `UnexpectedModelBehavior` and the person watched the turn die."""
    result = await _wrapped(SANDBOX, UnexpectedModelBehavior(_CEILING))

    assert isinstance(result, str), "a result the model reads, not an exception that ends the run"
    assert "retry budget" in result
    assert "its own tool call" in result


async def test_other_unexpected_behavior_is_not_swallowed() -> None:
    original = UnexpectedModelBehavior("Received empty model response")
    assert await _wrapped(SANDBOX, original) is original


async def test_another_tools_error_is_not_the_sandboxs_business() -> None:
    original = _retry(_REFUSAL)
    assert await _wrapped("fetch", original) is original
    ceiling = UnexpectedModelBehavior(_CEILING.replace("run_code", "fetch"))
    assert await _wrapped("fetch", ceiling) is ceiling
    plain = RuntimeError("boom")
    assert await _wrapped(SANDBOX, plain) is plain


async def test_a_healthy_program_result_passes_through() -> None:
    async def handler(args: dict[str, Any]) -> object:
        del args
        return "the program's own answer"

    result = await SandboxDiscipline[Any]().wrap_tool_execute(
        cast("Any", None),
        call=ToolCallPart(tool_name=SANDBOX, args={}),
        tool_def=ToolDefinition(name=SANDBOX),
        args={},
        handler=handler,
    )
    assert result == "the program's own answer"
