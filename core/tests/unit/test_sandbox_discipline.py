"""The sandbox's refusal of a name it does not hold says what to do, instead of a bare `Unknown function`.

Watched live: the studio agent built twelve edit ops as a variable inside `run_code`, then called
`adapt_workflow` (a direct tool) from the same program three times, read `NameError: Unknown
function: adapt_workflow` three times, and burned the tool's retry budget without ever learning
that a program cannot hand a variable to a direct tool.

These tests drive `wrap_tool_execute` with the exception shapes the tool manager actually hands
it. The first version of this guard sat on `on_tool_execute_error` and was tested by calling the
hook directly with a raw `ModelRetry`, which the manager never routes there (retry signals are
control flow to it), so the guard was green in the suite and dead on the live path, and the same
incident happened again with the hint nowhere in the model's history.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic_ai.exceptions import ModelRetry, ToolRetryError, UnexpectedModelBehavior
from pydantic_ai.messages import RetryInfo, ToolCallPart, ToolReturnPart
from pydantic_ai.tools import ToolDefinition

from core.ai.retrieval.contracts import RUN_CODE
from core.ai.sandbox import SandboxDiscipline

_REFUSAL = 'Runtime error:\nTraceback (most recent call last):\n  File "<python-input-5>", line 1, in <module>\n    res = await adapt_workflow(ops=ops)\nNameError: Unknown function: adapt_workflow'

_TYPE_CHECK_REFUSAL = (
    "Type error in code:\nerror[unresolved-reference]: Name `adapt_workflow` used when not defined\n  --> main.py:24:17\n"
    "   |\n24 |     res = await adapt_workflow(workflow_id='wf.research-compound.v6', ops=ops)\n   |                 ^^^^^^^^^^^^^^\n"
)

_CEILING = "Tool 'run_code' exceeded max retries count of 3. Consider raising the retry limit."


def _chat() -> SandboxDiscipline[Any]:
    """The discipline of an agent that loads capabilities and whose answer a person reads."""
    return SandboxDiscipline[Any](loads_capabilities=True, answers_a_person=True)


def _step() -> SandboxDiscipline[Any]:
    """The discipline of an agent that loads no capabilities and whose answer no person reads."""
    return SandboxDiscipline[Any](loads_capabilities=False, answers_a_person=False)


async def _wrapped(tool: str, error: Exception, discipline: SandboxDiscipline[Any] | None = None) -> Exception | object:
    """What the discipline does with a handler that fails this way: the exception it raised, or the result it returned."""

    async def failing_handler(args: dict[str, Any]) -> object:
        del args
        raise error

    try:
        return await (discipline if discipline is not None else _chat()).wrap_tool_execute(
            cast("Any", None),
            call=ToolCallPart(tool_name=tool, args={}),
            tool_def=ToolDefinition(name=tool),
            args={},
            handler=failing_handler,
        )
    except Exception as raised:  # noqa: BLE001 (the hook's contract is to raise or return, and the test reads which)
        return raised


def _retry(message: str) -> ToolRetryError:
    """The shape the tool manager hands the wrap hook: the tool's `ModelRetry`, counted and wrapped."""
    return ToolRetryError(ToolReturnPart(tool_name=RUN_CODE, content=message, outcome="retried", retry=RetryInfo(cause="model_retry")))


async def test_an_unknown_function_refusal_names_the_way_out() -> None:
    raised = await _wrapped(RUN_CODE, _retry(_REFUSAL))

    assert isinstance(raised, ToolRetryError), "rewritten in the shape it arrived in, so the counted retry is not counted again"
    content = raised.tool_retry.content
    assert isinstance(content, str)
    assert content.startswith(_REFUSAL), "the sandbox's own words stay first"
    assert "`adapt_workflow` is not a function inside the sandbox" in content
    assert "call it as its own tool call" in content
    assert "cannot hand a variable" in content


async def test_a_name_the_agent_cannot_see_is_sent_to_load_capability_not_given_up() -> None:
    """Live, `list_skills` sat behind a deferred capability and the hint told the model it did not exist, so it gave up."""
    raised = await _wrapped(RUN_CODE, _retry(_REFUSAL.replace("adapt_workflow", "list_skills")))

    assert isinstance(raised, ToolRetryError)
    content = raised.tool_retry.content
    assert isinstance(content, str)
    assert "If `list_skills` is in neither your tool list nor the sandbox's function list" in content
    assert "load the one whose description names it" in content
    assert "it does not exist here" not in content.split("load_capability")[0], "the way out comes before any verdict that it is gone"


def _dialect(discipline: SandboxDiscipline[Any] | None = None) -> str:
    """The instructions `SandboxDiscipline` adds to the agent, the chat role's by default."""
    return (discipline if discipline is not None else _chat()).get_instructions()(cast("Any", None))


def test_the_dialect_says_up_front_that_a_program_calls_only_the_sandboxs_functions() -> None:
    """Live, models called `fetch`, `save_workflow` and `list_skills` inside a program and learned the rule only from a refusal."""
    dialect = _dialect()

    rule = dialect.index("the only tools you can call are the functions listed as available inside the sandbox")
    assert rule < dialect.index("top-level script"), "the rule comes before the rest of the dialect"
    assert "its own tool call" in dialect
    assert "load_capability" in dialect


def test_the_dialect_says_json_dumps_takes_no_default() -> None:
    """Live, `json.dumps(value, default=str)` failed in the sandbox, whose `json` takes no `default=`."""
    dialect = _dialect()

    assert "`json.dumps` takes no `default=`" in dialect
    assert "already a JSON value" in dialect


async def test_the_type_checkers_refusal_of_the_same_name_gets_the_same_hint() -> None:
    """A fresh program is type-checked before it runs, and that voice refused `adapt_workflow` three times live with no hint."""
    raised = await _wrapped(RUN_CODE, _retry(_TYPE_CHECK_REFUSAL))

    assert isinstance(raised, ToolRetryError)
    content = raised.tool_retry.content
    assert isinstance(content, str)
    assert content.startswith(_TYPE_CHECK_REFUSAL)
    assert "`adapt_workflow` is not a function inside the sandbox" in content


async def test_the_unwrapped_voice_of_the_same_refusal_gets_the_same_hint() -> None:
    """A caller that runs tools without retry wrapping hands the raw `ModelRetry`, and the hint rides it the same way."""
    raised = await _wrapped(RUN_CODE, ModelRetry(_REFUSAL))

    assert isinstance(raised, ModelRetry)
    assert raised.message.startswith(_REFUSAL)
    assert "`adapt_workflow` is not a function inside the sandbox" in raised.message


async def test_any_other_sandbox_refusal_passes_through_untouched() -> None:
    original = _retry("Syntax error in code:\n  something else entirely")
    assert await _wrapped(RUN_CODE, original) is original


async def test_the_retry_ceiling_comes_back_as_a_result_not_a_dead_turn() -> None:
    """Live, the fourth refusal raised `UnexpectedModelBehavior` and the person watched the turn die."""
    result = await _wrapped(RUN_CODE, UnexpectedModelBehavior(_CEILING))

    assert isinstance(result, str), "a result the model reads, not an exception that ends the run"
    assert "retry budget" in result
    assert "its own tool call" in result


async def test_other_unexpected_behavior_is_not_swallowed() -> None:
    original = UnexpectedModelBehavior("Received empty model response")
    assert await _wrapped(RUN_CODE, original) is original


async def test_another_tools_error_is_not_the_sandboxs_business() -> None:
    original = _retry(_REFUSAL)
    assert await _wrapped("fetch", original) is original
    ceiling = UnexpectedModelBehavior(_CEILING.replace("run_code", "fetch"))
    assert await _wrapped("fetch", ceiling) is ceiling
    plain = RuntimeError("boom")
    assert await _wrapped(RUN_CODE, plain) is plain


async def test_a_healthy_program_result_passes_through() -> None:
    async def handler(args: dict[str, Any]) -> object:
        del args
        return "the program's own answer"

    result = await _chat().wrap_tool_execute(
        cast("Any", None),
        call=ToolCallPart(tool_name=RUN_CODE, args={}),
        tool_def=ToolDefinition(name=RUN_CODE),
        args={},
        handler=handler,
    )
    assert result == "the program's own answer"


_CHAT_DIALECT = (
    "## The sandbox's dialect\n"
    "Inside a program, the only tools you can call are the functions listed as available inside the "
    "sandbox. Every other tool in your tool list is called as its own tool call, never from a program. "
    "A tool of a capability you have not loaded appears only after `load_capability`, in one of those "
    "two places.\n"
    "A program is ONE top-level script, already running inside the host's event loop. Tool names "
    "resolve at top level only: a tool called inside a `def`/`async def` you wrote is refused as "
    "undefined, so do not wrap the work in a `main()`. Never call `asyncio.run()`, because there is "
    "no loop to start. Await tools directly, and for parallel calls write `import asyncio` then "
    "`results = await asyncio.gather(...)` at top level: the module is not preloaded, and a "
    "program that skips the import fails on NameError. The last expression is the program's "
    "result: end on the value, not on a bare function name. Build data as Python literals, and "
    "never paste a page's prose or markdown into the program as code. `json.dumps` "
    "takes no `default=`, and a tool's return is already a JSON value, so dump it as it is. A "
    "generator expression is a list inside the sandbox, so take its first match with `next(iter(...))` "
    "or a comprehension's `[0]`, never `next(<generator expression>)`. There "
    "is no file system (`open` on any path is refused) and no `dir`/`hasattr`/`type` introspection: "
    "keep values in variables between programs, and inspect one by printing or subscripting it."
)
"""The chat role's dialect, frozen as the chat agent reads it."""

_CHAT_HINT = (
    "`adapt_workflow` is not a function inside the sandbox. If `adapt_workflow` is a tool in your tool list, call it as its own "
    "tool call, with every argument written out as JSON in that call: a program cannot hand a variable to a "
    "direct tool, so a value you built in a program has to be written into the call itself. If `adapt_workflow` is in "
    "neither your tool list nor the sandbox's function list, look for it in the capabilities `load_capability` "
    "lists, load the one whose description names it, then call `adapt_workflow` where it appears, in the sandbox's "
    "function list or as its own tool call. If no capability covers it, it does not exist here."
)
"""The chat role's way out for `adapt_workflow`, frozen as the chat agent has always read it."""

_CHAT_RETRIES_SPENT = (
    "run_code has spent its retry budget for this turn, so do not write another program now. If you were calling "
    "a direct tool from inside a program, call it as its own tool call with every argument written out as JSON. "
    "Otherwise say plainly what you were doing and what failed, and let the person decide."
)
"""The chat role's answer to a spent retry budget, frozen as the chat agent has always read it."""


async def test_the_chat_roles_texts_are_the_ones_it_has_always_read() -> None:
    raised = await _wrapped(RUN_CODE, _retry(_REFUSAL))
    assert isinstance(raised, ToolRetryError)

    assert _dialect(_chat()) == _CHAT_DIALECT
    assert raised.tool_retry.content == f"{_REFUSAL}\n\n{_CHAT_HINT}"
    assert await _wrapped(RUN_CODE, UnexpectedModelBehavior(_CEILING)) == _CHAT_RETRIES_SPENT


def test_a_step_roles_dialect_names_no_capability_loading_and_no_person() -> None:
    dialect = _dialect(_step())

    assert "load_capability" not in dialect
    assert "person" not in dialect
    assert dialect == _CHAT_DIALECT.replace(
        " A tool of a capability you have not loaded appears only after `load_capability`, in one of those two places.", ""
    ), "the rest of the dialect is the chat role's, word for word"


async def test_a_step_roles_way_out_is_its_tool_list_not_capability_loading() -> None:
    raised = await _wrapped(RUN_CODE, _retry(_REFUSAL), _step())

    assert isinstance(raised, ToolRetryError)
    content = raised.tool_retry.content
    assert isinstance(content, str)
    hint = content.removeprefix(_REFUSAL)
    assert "`adapt_workflow` is not a function inside the sandbox" in hint
    assert "call it as its own tool call" in hint
    assert "your tool list and the sandbox's functions are all you can call" in hint
    assert "load_capability" not in hint
    assert "person" not in hint


async def test_a_step_roles_spent_retry_budget_still_ends_the_run() -> None:
    """No person reads a step's answer, so the run's own error handling decides, not a text the model answers from."""
    ceiling = UnexpectedModelBehavior(_CEILING)
    assert await _wrapped(RUN_CODE, ceiling, _step()) is ceiling
