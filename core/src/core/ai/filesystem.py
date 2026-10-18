"""A `FileSystem` whose `read_file` returns each line as it is in the file, without a line number, and with no character cap by default."""

from __future__ import annotations

import re

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import FilteredToolset
from pydantic_ai_harness.filesystem import FileSystem

if TYPE_CHECKING:
    from pydantic_ai import RunContext
    from pydantic_ai.tools import ToolDefinition
    from pydantic_ai.toolsets.abstract import ToolsetTool
    from pydantic_ai_harness.filesystem import FileSystemToolset

_READ_FILE = "read_file"

_LINE_NUMBER = re.compile(r"^ *\d+\t")
"""The number `FileSystem` puts in front of each line it reads, right-aligned and followed by a tab."""

_READ_FILE_DESCRIPTION = (
    "<summary>Read a text file.</summary>\n<returns>\n<description>File content, plus metadata header.</description>\n</returns>"
)
"""What the model reads about `read_file`, which returns each line without a number."""


@dataclass
class UnnumberedFileSystem(FileSystem[AgentDepsT]):
    """`FileSystem` with the line numbers taken off `read_file`'s lines. Its header line and every other tool are unchanged.

    Only `max_read_lines` caps a read unless the caller sets `max_read_chars`.
    """

    max_read_chars: int | None = None
    """The most characters one `read_file` call returns. `None` leaves only `max_read_lines` in force."""

    def get_toolset(self) -> FileSystemToolset[AgentDepsT] | FilteredToolset[AgentDepsT]:
        """The filesystem's toolset, with `read_file` returning its lines unnumbered.

        Returns:
            Every tool of the upstream toolset, with `read_file` described and answered without line numbers.
        """
        return _UnnumberedReadFile(wrapped=super().get_toolset(), filter_func=_every_tool)


@dataclass
class _UnnumberedReadFile(FilteredToolset[AgentDepsT]):
    """Every tool of the wrapped toolset, with `read_file` described as returning its lines unnumbered."""

    async def get_tools(self, ctx: RunContext[AgentDepsT]) -> dict[str, ToolsetTool[AgentDepsT]]:
        """The wrapped tools, with `read_file` described as returning its lines unnumbered."""
        tools = await super().get_tools(ctx)
        read_file = tools.get(_READ_FILE)
        if read_file is not None:
            tools[_READ_FILE] = replace(read_file, tool_def=replace(read_file.tool_def, description=_READ_FILE_DESCRIPTION))
        return tools

    async def call_tool(
        self, name: str, tool_args: dict[str, object], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]
    ) -> object:
        """The wrapped tool's result, with `read_file`'s numbering removed after its header line."""
        result = await super().call_tool(name, tool_args, ctx, tool)
        if name != _READ_FILE or not isinstance(result, str):
            return result
        return _unnumbered(result)


def _unnumbered(result: str) -> str:
    """The `read_file` result with one line number removed from each line after its header line."""
    header, newline, body = result.partition("\n")
    return header + newline + "".join(_LINE_NUMBER.sub("", line, count=1) for line in body.splitlines(keepends=True))


def _every_tool(ctx: RunContext[AgentDepsT], tool_def: ToolDefinition) -> bool:
    """Keep every tool."""
    del ctx, tool_def
    return True
