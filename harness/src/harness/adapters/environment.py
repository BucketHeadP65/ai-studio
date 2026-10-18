"""What every step runs with: the sandbox, the shell, the spill store for oversized returns, and the guards.

The tool names a step may hold are defined in `harness.domain.contracts`. This module builds what those names run on,
how much of their returns reaches the model, and where the rest goes.
"""

from __future__ import annotations

import json
import os
import shutil

from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
from itertools import count
from typing import TYPE_CHECKING, Any

from core.ai.format_maps.contracts import FileContentCache
from core.ai.tool_output_limits import ToolOutputLimits
from pydantic import TypeAdapter
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai_harness._monty_exec import MontyExecutor
from pydantic_ai_harness.compaction import WarnNearLimits
from pydantic_ai_harness.shell import LLM_API_KEY_ENV_PATTERNS, Shell
from pydantic_ai_harness.tool_output_limits import Band, LocalFileStore, OverflowStore, Spill, Truncate
from pydantic_core import to_jsonable_python
from pydantic_monty import AsyncMonty, CollectString, MontyError, OSPolicy

from harness import settings
from harness.domain.contracts import NOT_IN_SANDBOX, PROGRAM_OUTPUT_LIMIT, SANDBOX, SHELL_TOOL
from harness.settings import CHECKOUT, SPILL_DIR
from harness.vendor.loop_detection import LoopDetection

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

    from core.ai.workflows.contracts import CodeRunner, Program
    from pydantic_ai import RunContext
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai.tools import ToolDefinition

    from harness.domain.contracts import ToolCaller


_ALIASES = count(1)
"""The alias numbers every spill store in this process draws from, so two stores never issue the same alias."""


@dataclass
class ShortHandles:
    """A spill store that hands back short aliases such as `r1`, which a model can copy without mistakes.

    Every store in this process draws its aliases from one count, so stores that share a directory never collide.
    """

    inner: LocalFileStore
    _aliases: dict[str, str] = field(default_factory=dict[str, str])

    async def write(self, key: str, data: bytes) -> str:
        """Store `data` under a new short alias.

        Args:
            key: The harness's own key for the call. It is ignored.
            data: The oversized return.

        Returns:
            The alias `read` takes back.
        """
        del key
        alias = f"r{next(_ALIASES)}"
        self._aliases[alias] = await self.inner.write(alias, data)
        return alias

    async def read(self, handle: str) -> bytes:
        """Read a spilled return back.

        Args:
            handle: An alias `write` returned, or a handle of the inner store.

        Returns:
            The stored bytes.
        """
        return await self.inner.read(self._aliases.get(handle, handle))


def in_sandbox(_ctx: RunContext[object], tool: ToolDefinition) -> bool:
    """Whether one of an agent's tools becomes a Python function in a program, as every tool outside `NOT_IN_SANDBOX` does.

    Args:
        _ctx: The run context. It is not read.
        tool: The tool's definition.

    Returns:
        True when a program may call the tool.
    """
    return tool.name not in NOT_IN_SANDBOX


def spill_store() -> OverflowStore:
    """Where an oversized tool return goes, with handles short enough to retype.

    Returns:
        A new store in this process's spill directory, whose files are removed after twelve hours.
    """
    return ShortHandles(LocalFileStore(SPILL_DIR / str(os.getpid()), cleanup_after=timedelta(hours=12)))


def prune_ended_spill_folders(spill_dir: Path) -> int:
    """Remove the spill folders and content caches of processes that have ended, and answer how many went.

    A folder is named for its process, `<pid>` or `<pid>-maps`. This process's own folders, a process still running and a folder
    named any other way are left alone.

    Args:
        spill_dir: The directory every process spills under.

    Returns:
        How many folders were removed.
    """
    if not spill_dir.is_dir():
        return 0
    removed = 0
    for folder in spill_dir.iterdir():
        pid_text = folder.name.removesuffix("-maps")
        if not (folder.is_dir() and pid_text.isdigit()) or int(pid_text) == os.getpid() or _is_running(int(pid_text)):
            continue
        shutil.rmtree(folder, ignore_errors=True)
        removed += 1
    return removed


def _is_running(pid: int) -> bool:
    """Whether a process with `pid` exists, counting one this user may not signal as running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def content_cache() -> FileContentCache:
    """Where a mapped tool's fetched content waits for `part=` calls.

    The cache sits beside the spill directory, never inside it, so a `read_tool_result` handle never resolves into it.

    Returns:
        A cache in this process's own directory, whose files are removed after twelve hours like the spill files.
    """
    return FileContentCache(SPILL_DIR / f"{os.getpid()}-maps", cleanup_after=timedelta(hours=12))


ACT_OUTPUT_LIMIT = 16_000
"""How much of one act's result (a shell command, a file read, a search) reaches the model whole before it is spilled."""

ACT_PREVIEW = 6_000
"""How much of a spilled act result rides inline with its handle, enough to decide whether the rest is worth a page."""

SHELL_OUTPUT_CAP = 32_000
"""Where the shell itself cuts a command's output. It must stay above `ACT_OUTPUT_LIMIT`, so the spill band decides what the model sees."""


COMMAND_ENV_NAMES: tuple[str, ...] = ("TMPDIR", "SSH_AUTH_SOCK", "USER", "LOGNAME")
"""The only variables of this process's environment that a command run by an agent inherits.

The workspace adds its own `PATH`, `HOME` and locale, and a shell adds its own `env`. Nothing else of this process reaches a command."""


def workspace_env(source: Mapping[str, str]) -> dict[str, str]:
    """The environment a workspace gives every command: the `COMMAND_ENV_NAMES` that `source` sets, and nothing else.

    No model provider key is ever among them.

    Args:
        source: The environment to take the names from. Production passes this process's own `os.environ`.

    Returns:
        Each listed name that `source` sets, with its value.
    """
    return {name: source[name] for name in COMMAND_ENV_NAMES if name in source}


def chat_workspace(root: Path, *, environ: Mapping[str, str]) -> LocalWorkspace[object]:
    """The writable workspace a session's shell and files work in, with the allow-listed command environment.

    Args:
        root: Where commands start and relative paths resolve.
        environ: The environment `workspace_env` takes the command environment from.

    Returns:
        A local workspace on `root`, for any deps.
    """
    return LocalWorkspace[object](root, env=workspace_env(environ))


def step_workspace(*, writable: bool, environ: Mapping[str, str] | None = None) -> LocalWorkspace[object]:
    """The workspace a workflow step's agent runs in, on this checkout.

    Args:
        writable: Whether the step holds a shell, which needs a workspace that can run commands. A step that only
            reads its skills gets a read-only one.
        environ: The environment `workspace_env` takes the command environment from. `None` reads this process's own.

    Returns:
        A local workspace on this checkout, for any deps.
    """
    return LocalWorkspace[object](CHECKOUT, read_only=not writable, env=workspace_env(os.environ if environ is None else environ))


def step_shell() -> Shell[None]:
    """The shell a declared step gets when it asks for one, with the agent's own guards.

    A command run in it cannot read the model credentials this process holds, and it finds the console closed, like
    every shell a run holds. Its `env` adds only that marker, so the command environment is the workspace's. The step's agent must also hold `step_workspace(writable=True)`.

    Returns:
        A shell with a twenty second default timeout and its output cut at `SHELL_OUTPUT_CAP`.
    """
    return Shell[None](
        env={settings.CONSOLE_CLOSED_ENV: "1"},
        denied_env_patterns=LLM_API_KEY_ENV_PATTERNS,
        default_timeout=20.0,
        max_output_chars=SHELL_OUTPUT_CAP,
    )


def step_guards(budget: int, *, direct_reads: tuple[str, ...] = ()) -> list[AbstractCapability[object]]:
    """What every step gets besides its tools: a loop guard, a budget warning and an output limit.

    The set takes `object` deps, so any agent this app spawns can hold it. The output limit spills only what the model
    reads: the sandbox's own return, and each tool in `direct_reads`. A tool called inside the sandbox is never limited,
    so a program always receives the tool's real return.

    Args:
        budget: The iteration budget the warning counts toward.
        direct_reads: The read tools whose returns reach the step's own transcript with no sandbox in between, as a
            agent step's reads do. Each is spilled above `ACT_OUTPUT_LIMIT`. A briefing, whose reads run inside the
            sandbox, passes none.

    Returns:
        The loop guard, the budget warning and the output limit, in that order.
    """
    return [
        LoopDetection(on_loop="nudge"),
        WarnNearLimits(max_iterations=budget),
        ToolOutputLimits(
            bands=[Band(over=PROGRAM_OUTPUT_LIMIT, action=Spill(preview_chars=3_000, then=Truncate(max_chars=PROGRAM_OUTPUT_LIMIT)))],
            per_tool={
                name: [Band(over=ACT_OUTPUT_LIMIT, action=Spill(preview_chars=ACT_PREVIEW, then=Truncate(max_chars=ACT_OUTPUT_LIMIT)))]
                for name in direct_reads
            },
            store=spill_store(),
            tool_filter=(SANDBOX, *direct_reads),
        ),
    ]


PROGRAM_TIME_LIMIT = 10.0
"""Seconds a program may run before the sandbox stops it."""

PROGRAM_OS_POLICY: OSPolicy = {"datetime": "call_host", "random_start": "call_host", "sleep": "call_host"}
"""The clock, randomness and sleep a program reaches: none.

Each is routed to a host handler the runner never installs, so a call to one fails inside the program, and the same
program with the same inputs returns the same result.
"""

SANDBOX_REQUEST_TIMEOUT = 30.0
"""Seconds the pool waits on a worker that stops answering at all before it kills it, a second limit behind `PROGRAM_TIME_LIMIT`."""


def sandbox_pool() -> AsyncMonty:
    """The worker pool programs run in, an async context manager the caller enters and owns.

    Enter one pool per process and share it across every step of every run.

    Returns:
        The pool, not yet entered.
    """
    return AsyncMonty(request_timeout=SANDBOX_REQUEST_TIMEOUT)


def code_runner(sandbox: AsyncMonty, *, reads: Sequence[str], call: ToolCaller) -> CodeRunner:
    """Run a workflow's programs in the sandbox, with no model turn anywhere.

    A program may call only the tools it was granted, and only those in `reads`. A grant outside `reads` is refused
    before the program starts, and an ungranted name raises `NameError` inside the program without reaching this
    process. A program granted nothing has no network, no filesystem, no clock, no third-party import and no host
    function.

    Args:
        sandbox: The entered worker pool.
        reads: The tool names a program may call here, this run's reads.
        call: How to run one of them.

    Returns:
        The runner. It binds a program's inputs, runs its source and validates the result against the declared output
        type, and raises `RuntimeError` for a grant outside `reads` or a program that fails in the sandbox.
    """
    reachable = frozenset(reads)

    async def run_code_step(step: Program, output_type: type, /, **inputs: object) -> object:
        """Bind this step's inputs, run its source, and validate what came back against its declared shape."""
        if unreachable := tuple(name for name in step.tools if name not in reachable):
            msg = (
                f"the program {step.name!r} was granted {', '.join(unreachable)}, which a program cannot reach here: "
                f"a sandboxed program calls this run's reads ({', '.join(reads) or 'none'}), while {SHELL_TOOL} is a "
                f"capability only a briefing holds. Make it a briefing, or drop the grant."
            )
            raise RuntimeError(msg)

        async def dispatch(name: str, kwargs: dict[str, Any]) -> object:
            """One granted call, from inside the program. Only plain data crosses back in."""
            return to_jsonable_python(await call(name, dict(kwargs)))

        printed = CollectString()
        try:
            async with sandbox.checkout(
                script_name=f"{step.name}.py", limits={"max_feed_duration_secs": PROGRAM_TIME_LIMIT}, os_policy=PROGRAM_OS_POLICY
            ) as session:
                # Inputs arrive as globals, under the names the workflow gave them. Only plain data crosses
                # the boundary, in both directions.
                started = partial(
                    session.feed_start,
                    step.source,
                    inputs={name: to_jsonable_python(value) for name, value in inputs.items()},
                    print_callback=printed,
                )
                completed = await MontyExecutor(dispatch=dispatch, valid_names=frozenset(step.tools)).run(started)
        except MontyError as error:
            detail = f"{printed.output.strip()}\n{error}".strip() if printed.output.strip() else str(error)
            msg = f"the program {step.name!r} failed in the sandbox: {detail}"
            raise RuntimeError(msg) from error
        # A program is done only when its result validates against the declared output type.
        return TypeAdapter(output_type).validate_python(completed.output)

    return run_code_step


def as_text(value: object) -> str:
    """One input as a briefing shows it to the model: text stays text, and anything else becomes indented JSON, whole.

    Args:
        value: The input.

    Returns:
        The text, or `str(value)` when the value has no JSON form.
    """
    if isinstance(value, str):
        return value
    try:
        return json.dumps(to_jsonable_python(value), indent=2, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)
