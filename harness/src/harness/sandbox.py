"""The sandbox rules the agent needs beside `CodeMode`, and the way out of a sandbox refusal.

`CodeMode`'s own instructions never state the dialect rule: the only tools a program can call are the sandbox's own
functions, it is one top-level script, and it starts no event loop of its own.
`SandboxDiscipline` is mounted beside `CodeMode`, never wrapped around it.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry, ToolRetryError, UnexpectedModelBehavior
from pydantic_ai.tools import AgentDepsT

from harness.domain.contracts import SANDBOX

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic_ai.capabilities.abstract import ValidatedToolArgs, WrapToolExecuteHandler
    from pydantic_ai.messages import ToolCallPart
    from pydantic_ai.tools import RunContext, ToolDefinition

_UNKNOWN_FUNCTION = re.compile(r"NameError: Unknown function: (\w+)|error\[unresolved-reference\]: Name `(\w+)` used when not defined")
"""The sandbox's refusal of a name it does not hold, as the runtime says it and as the type checker says it."""

_DIRECT_TOOL_HINT = (
    "`{name}` is not a function inside the sandbox. If `{name}` is a tool in your tool list, call it as its own "
    "tool call, with every argument written out as JSON in that call: a program cannot hand a variable to a "
    "direct tool, so a value you built in a program has to be written into the call itself. If `{name}` is in "
    "neither your tool list nor the sandbox's function list, look for it in the capabilities `load_capability` "
    "lists, load the one whose description names it, then call `{name}` where it appears, in the sandbox's "
    "function list or as its own tool call. If no capability covers it, it does not exist here."
)

_RETRIES_SPENT = (
    "run_code has spent its retry budget for this turn, so do not write another program now. If you were calling "
    "a direct tool from inside a program, call it as its own tool call with every argument written out as JSON. "
    "Otherwise say plainly what you were doing and what failed, and let the person decide."
)

_DIALECT = (
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
    "result: end on the value, not on a bare function name. Build data as Python literals, "
    "never paste a page's prose or markdown into the program as code, and keep any one program "
    "under a few thousand characters: a huge program is where the sandbox breaks. `json.dumps` "
    "takes no `default=`, and a tool's return is already a JSON value, so dump it as it is. There "
    "is no file system (`open` on any path is refused) and no `dir`/`hasattr`/`type` introspection: "
    "keep values in variables between programs, and inspect one by printing or subscripting it."
)


@dataclass
class SandboxDiscipline(AbstractCapability[AgentDepsT]):
    """The sandbox's dialect as agent instructions, and a way out added to a sandbox refusal.

    It adds no tools, and it sits beside `CodeMode` rather than wrapping it.
    """

    def get_instructions(self) -> Callable[[RunContext[AgentDepsT]], str]:
        """The rule that keeps a program flat, stated once for every program the agent writes.

        Returns:
            A function answering the dialect text. It is a function, so the model reads it after the agent's identity,
            never before it.
        """

        def compose(_ctx: RunContext[AgentDepsT]) -> str:
            return _DIALECT

        return compose

    async def wrap_tool_execute(
        self, ctx: RunContext[AgentDepsT], *, call: ToolCallPart, tool_def: ToolDefinition, args: ValidatedToolArgs, handler: WrapToolExecuteHandler
    ) -> object:
        """Add the way out to a `run_code` refusal of an unknown name, and answer a spent retry budget as a result.

        Args:
            ctx: The run context. Unused.
            call: The tool call. Unused.
            tool_def: The tool being called. Only `run_code` is touched.
            args: The validated arguments, handed to `handler` unchanged.
            handler: What executes the tool.

        Returns:
            The tool's own result. When `run_code` has spent its retry budget, a text naming the way out instead, so the
            turn stays the model's to finish. The turn's request budget still bounds the loop.

        Raises:
            ToolRetryError: The sandbox's refusal, with the hint appended after the sandbox's own words when it names an
                unknown function. It is the same retry, so it is counted once.
            ModelRetry: The same refusal, hinted the same way, for a caller that runs tools without retry wrapping.
            Exception: Any other error, exactly as it arrived.
        """
        del ctx, call
        if tool_def.name != SANDBOX:
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
            if "max retries" not in str(error):
                raise
            return _RETRIES_SPENT

    @staticmethod
    def _hinted(message: str) -> str | None:
        """The refusal with the way out appended, or `None` for a refusal that is not about an unknown name."""
        unknown = _UNKNOWN_FUNCTION.search(message)
        if unknown is None:
            return None
        name = unknown.group(1) or unknown.group(2)
        return f"{message}\n\n{_DIRECT_TOOL_HINT.format(name=name)}"
