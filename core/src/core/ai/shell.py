"""A `Shell` that refuses a command naming a denied path, before the command runs."""

from __future__ import annotations

import fnmatch
import glob
import os
import shlex

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AbstractToolset, WrapperToolset

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from pydantic_ai import AgentRunResult, RunContext
    from pydantic_ai.capabilities import WrapRunHandler
    from pydantic_ai.toolsets.abstract import ToolsetTool
    from pydantic_ai_harness.shell import Shell

_COMMAND_TOOLS = frozenset({"run_command", "start_command", "shell"})
"""The shell's tools whose `command` argument is run."""

_SEPARATORS = "();<>|&`"
"""The characters that end a word of a command with no space around them."""

_GLOB_CHARACTERS = frozenset("*?[")


@dataclass
class PathGuardedShell(AbstractCapability[AgentDepsT]):
    """A `Shell` that refuses a command when a path it names matches a denied pattern, before the command runs.

    A named path is every word of the command, the part of a word after an `=`, and every file a glob word matches.
    Each is read from the working directory of the run's workspace with links followed, and matched the way `FileSystem`
    matches its `denied_patterns`. The guard reads what the command says, not what its programs open, so a program that finds a
    file by itself still reaches it.
    """

    shell: Shell[AgentDepsT]
    """The shell whose commands are checked. Relative paths are read from the run's workspace directory, even after a `cd`."""

    denied_path_patterns: Sequence[str]
    """Glob patterns for paths no command may name, such as `**/.env`. A path inside the workspace directory is matched relative to it."""

    async def before_run(self, ctx: RunContext[AgentDepsT]) -> None:
        """Run the shell's own start-of-run check, which refuses a run that has no workspace.

        Args:
            ctx: The run.
        """
        await self.shell.before_run(ctx)

    async def wrap_run(self, ctx: RunContext[AgentDepsT], *, handler: WrapRunHandler) -> AgentRunResult[object]:
        """The run's result, with the shell's own end-of-run work done.

        Args:
            ctx: The run.
            handler: Runs the rest of the run.

        Returns:
            What the shell's `wrap_run` returns.
        """
        return await self.shell.wrap_run(ctx, handler=handler)

    def get_toolset(self) -> AbstractToolset[AgentDepsT]:
        """The shell's tools, each refusing a command that names a denied path.

        Returns:
            The shell's toolset, answering such a command with a retry that names the path and the pattern it matched.
        """
        return _PathGuard(wrapped=self.shell.get_toolset(), patterns=tuple(self.denied_path_patterns))


@dataclass
class _PathGuard(WrapperToolset[AgentDepsT]):
    """The wrapped shell tools, refusing a command that names a denied path."""

    patterns: tuple[str, ...]

    async def call_tool(
        self, name: str, tool_args: dict[str, object], ctx: RunContext[AgentDepsT], tool: ToolsetTool[AgentDepsT]
    ) -> object:
        """The wrapped tool's result, or a retry naming the first denied path its command names."""
        command = tool_args.get("command")
        if name in _COMMAND_TOOLS and isinstance(command, str):
            cwd = Path(await ctx.workspace.working_dir())
            for written, path in _named_paths(command, cwd):
                pattern = next((pattern for pattern in self.patterns if _matches(path, pattern)), None)
                if pattern is not None:
                    refusal = f"Path {written!r} is denied by pattern {pattern!r}."
                    raise ModelRetry(refusal)
        return await super().call_tool(name, tool_args, ctx, tool)


def _named_paths(command: str, cwd: Path) -> Iterator[tuple[str, str]]:
    """Each path the command names as written, beside its lexical and its resolved form relative to `cwd`."""
    for word in _words(command):
        for written in _expanded(word, cwd):
            located = cwd / _home_expanded(written)
            for path in (os.path.normpath(located), os.path.realpath(located)):
                yield written, _relative(Path(path), cwd)


def _words(command: str) -> list[str]:
    """Every word of the command without its quotes, and the part after the `=` of each word holding one."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SEPARATORS)
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        words = list(lexer)
    except ValueError:
        words = command.split()
    return [part for word in words for part in (word, word.partition("=")[2]) if part]


def _expanded(word: str, cwd: Path) -> list[str]:
    """The word, and every path it matches from `cwd` when it holds a glob character."""
    if not _GLOB_CHARACTERS.intersection(word):
        return [word]
    return [word, *glob.glob(_home_expanded(word), root_dir=cwd)]  # noqa: PTH207 (`Path.glob` refuses an absolute pattern and matches hidden files the shell skips)


def _home_expanded(word: str) -> str:
    """The word with a leading `~` read as a home directory, left as written for a user with none."""
    return os.path.expanduser(word)  # noqa: PTH111 (`Path.expanduser` raises for an unknown user, whose `~` the shell leaves as written)


def _relative(path: Path, cwd: Path) -> str:
    """The path relative to `cwd` when it lies inside it, else the whole path."""
    return path.relative_to(cwd).as_posix() if path.is_relative_to(cwd) else path.as_posix()


def _matches(path: str, pattern: str) -> bool:
    """Whether the path matches the pattern the way `FileSystem` matches one, a leading `**/` also matching no directory."""
    return fnmatch.fnmatch(path, pattern) or (pattern.startswith("**/") and fnmatch.fnmatch(path, pattern.removeprefix("**/")))
