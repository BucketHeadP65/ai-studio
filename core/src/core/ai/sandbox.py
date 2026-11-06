"""The sandbox rules the agent needs beside `CodeMode`, and the way out of a sandbox refusal.

`CodeMode`'s own instructions never state the dialect rule: the only tools a program can call are the sandbox's own
functions, it is one top-level script, and it starts no event loop of its own.
`SandboxDiscipline` is mounted beside `CodeMode`, never wrapped around it.
"""

from __future__ import annotations

import builtins
import re

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry, ToolRetryError, UnexpectedModelBehavior
from pydantic_ai.tools import AgentDepsT

from core.ai.retrieval.contracts import RUN_CODE

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.tools import RunContext, ToolDefinition

_UNKNOWN_FUNCTION = re.compile(r"NameError: Unknown function: (\w+)|error\[unresolved-reference\]: Name `(\w+)` used when not defined")
"""The sandbox's refusal of a name it does not hold, as the runtime says it and as the type checker says it."""

_PYTHON_BUILTINS = frozenset(dir(builtins))
"""Every name CPython's builtins hold, which the way out never treats as a missing tool."""

_DIRECT_TOOL_HINT = (
    "`{name}` is not a function inside the sandbox. If `{name}` is a tool in your tool list, call it as its own "
    "tool call, with every argument written out as JSON in that call: a program cannot hand a variable to a "
    "direct tool, so a value you built in a program has to be written into the call itself."
)
"""The way out every agent reads, the first half of the hint."""

_LOAD_CAPABILITY_HINT = (
    " If `{name}` is in neither your tool list nor the sandbox's function list, look for it in the capabilities "
    "`load_capability` lists, load the one whose description names it, then call `{name}` where it appears, in the "
    "sandbox's function list or as its own tool call. If no capability covers it, it does not exist here."
)
"""The rest of the hint for an agent that loads capabilities."""

_NO_SUCH_TOOL_HINT = (
    " If `{name}` is in neither your tool list nor the sandbox's function list, it does not exist here: your tool list "
    "and the sandbox's functions are all you can call."
)
"""The rest of the hint for an agent that loads no capabilities."""

_RETRIES_SPENT = (
    "run_code has spent its retry budget for this turn, so do not write another program now. If you were calling "
    "a direct tool from inside a program, call it as its own tool call with every argument written out as JSON. "
    "Otherwise say plainly what you were doing and what failed, and let the person decide."
)
"""What an agent whose answer a person reads gets back once `run_code` has spent its retry budget."""

_DIALECT_TOOLS = (
    "## The sandbox's dialect\n"
    "Inside a program, the only tools you can call are the functions listed as available inside the "
    "sandbox. Every other tool in your tool list is called as its own tool call, never from a program."
)
"""The dialect's first rule: which tools a program calls and which are their own tool calls."""

_DIALECT_LOAD_CAPABILITY = (
    " A tool of a capability you have not loaded appears only after `load_capability`, in one of those two places."
)
"""The sentence an agent that loads capabilities reads after the first rule."""

_DIALECT_PROGRAM = (
    "\nA program is ONE top-level script, already running inside the host's event loop. Tool names "
    "resolve at top level only: a tool called inside a `def`/`async def` you wrote is refused as "
    "undefined, so do not wrap the work in a `main()`. Never call `asyncio.run()`, because there is "
    "no loop to start. Await tools directly, and for parallel calls write `import asyncio` then "
    "`results = await asyncio.gather(...)` at top level: the module is not preloaded, and a "
    "program that skips the import fails on NameError. The last expression is the program's "
    "result: end on the value, not on a bare function name. Build data as Python literals, and "
    "never paste a page's prose or markdown into the program as code. A "
    "generator expression is a list inside the sandbox, so take its first match with `next(iter(...))` "
    "or a comprehension's `[0]`, never `next(<generator expression>)`. There "
    "is no file system (`open` on any path is refused). Do not use `dir`, `vars`, `callable`, `hasattr` "
    "or `getattr`. A turn's first program is refused for naming them, and later ones find `dir`, `vars` "
    "and `callable` missing and `hasattr` blind to a list's, dict's or string's methods. Test a key with "
    "`in`, and a value's kind with `type()` or `isinstance()`. Keep values in variables between programs, "
    "and inspect one by printing or subscripting it."
)


@dataclass(kw_only=True)
class SandboxDiscipline(AbstractCapability[AgentDepsT]):
    """The sandbox's dialect as agent instructions, and a way out added to a sandbox refusal.

    It adds no tools, and it sits beside `CodeMode` rather than wrapping it. You must say what the agent has, so it reads
    only text that is true for it.
    """

    loads_capabilities: bool
    """Whether the agent reaches more tools through `load_capability`. Only then do the dialect and the hint name it."""

    answers_a_person: bool
    """Whether a person reads the agent's answer. Only then does a spent `run_code` retry budget come back as a result
    the model answers from. Otherwise the run fails as it would without this capability."""

    def get_instructions(self) -> Callable[[RunContext[AgentDepsT]], str]:
        """The rule that keeps a program flat, stated once for every program the agent writes.

        Returns:
            A function answering the dialect text. It is a function, so the model reads it after the agent's identity,
            never before it.
        """
        dialect = _DIALECT_TOOLS + (_DIALECT_LOAD_CAPABILITY if self.loads_capabilities else "") + _DIALECT_PROGRAM

        def compose(_ctx: RunContext[AgentDepsT]) -> str:
            return dialect

        return compose

    async def wrap_tool_execute(
        self, ctx: RunContext[AgentDepsT], *, call: ToolCallPart, tool_def: ToolDefinition, args: ValidatedToolArgs, handler: WrapToolExecuteHandler
    ) -> object:
        """Add the way out to a `run_code` refusal of an unknown name, and answer a spent retry budget for a person.

        Args:
            ctx: The run context. Unused.
            call: The tool call. Unused.
            tool_def: The tool being called. Only `run_code` is touched.
            args: The validated arguments, handed to `handler` unchanged.
            handler: What executes the tool.

        Returns:
            The tool's own result. When `run_code` has spent its retry budget and a person reads the answer, a text naming
            the way out instead, so the turn stays the model's to finish. The turn's request budget still bounds the loop.

        Raises:
            ToolRetryError: The sandbox's refusal, with the hint appended after the sandbox's own words when it names an
                unknown function that is not a Python builtin. It is the same retry, so it is counted once.
            ModelRetry: The same refusal, hinted the same way, for a caller that runs tools without retry wrapping.
            UnexpectedModelBehavior: `run_code` has spent its retry budget and no person reads the answer.
            Exception: Any other error, exactly as it arrived.
        """
        del ctx, call
        if tool_def.name != RUN_CODE:
            return await handler(args)
        try:
            return await handler(args)
        except ToolRetryError as error:
            content = error.tool_retry.content
            hinted = self._hinted(content) if isinstance(content, str) else None
            if hinted is None:
                raise
            raise ToolRetryError(replace(error.tool_retry, content=hinted)) from error
        except ModelRetry as error:
            # The same refusal in its unwrapped voice, for a caller that runs tools without retry wrapping.
            hinted = self._hinted(error.message)
            if hinted is None:
                raise
            raise ModelRetry(hinted) from error
        except UnexpectedModelBehavior as error:
            if not self.answers_a_person or "max retries" not in str(error):
                raise
            return _RETRIES_SPENT

    def _hinted(self, message: str) -> str | None:
        """The refusal with the way out appended, or `None` when it is not about an unknown name or names a Python builtin."""
        unknown = _UNKNOWN_FUNCTION.search(message)
        if unknown is None:
            return None
        name = unknown.group(1) or unknown.group(2)
        if name in _PYTHON_BUILTINS:
            return None
        rest = _LOAD_CAPABILITY_HINT if self.loads_capabilities else _NO_SUCH_TOOL_HINT
        return f"{message}\n\n{_DIRECT_TOOL_HINT.format(name=name)}{rest.format(name=name)}"
