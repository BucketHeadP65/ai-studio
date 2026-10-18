"""Drive another ACP agent as a one-shot subagent: spawn it, brief it, and collect what it did."""

from __future__ import annotations

import asyncio
import contextlib

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from importlib.metadata import version
from typing import TYPE_CHECKING

import acp

from acp import Client, RequestError, schema

from harness.adapters.acp_client._permission import AllowOnce, PermissionPolicy, outcome_for

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import JsonValue

CLIENT_NAME = "harness"
"""The client name advertised to the external agent during `initialize`."""

CLIENT_VERSION = version("harness")
"""The client version advertised to the external agent during `initialize`, the installed harness package's."""

CANCEL_GRACE_SECONDS = 5.0
"""How long a cancelled turn is given to answer before the client stops waiting for it."""

# What every connection this package opens advertises: the protocol version, no capability at all,
# and who is calling.
_INITIALIZE = {
    "protocol_version": acp.PROTOCOL_VERSION,
    "client_capabilities": schema.ClientCapabilities(
        fs=schema.FileSystemCapabilities(read_text_file=False, write_text_file=False), terminal=False
    ),
    "client_info": schema.Implementation(name=CLIENT_NAME, version=CLIENT_VERSION),
}

# Every `session/update` payload an ACP agent may send, as the SDK's `Client.session_update` accepts it.
type AgentUpdate = (
    schema.UserMessageChunk
    | schema.AgentMessageChunk
    | schema.AgentThoughtChunk
    | schema.ToolCallStart
    | schema.ToolCallProgress
    | schema.AgentPlanUpdate
    | schema.AgentPlanContentUpdate
    | schema.AgentPlanRemovedUpdate
    | schema.AvailableCommandsUpdate
    | schema.CurrentModeUpdate
    | schema.ConfigOptionUpdate
    | schema.SessionInfoUpdate
    | schema.UsageUpdate
)

# Where each forwarded update goes, called synchronously as it arrives.
type UpdateSink = Callable[[AgentUpdate], None]


@dataclass(frozen=True, kw_only=True)
class ExternalResult:
    """What one external-agent turn produced: its text, why it stopped, and the files it reported touching."""

    text: str
    stop_reason: str
    touched_files: tuple[str, ...]


def _not_advertised(method: str) -> RequestError:
    """The refusal for a client method this client never advertised in `initialize`."""
    return RequestError(-32601, f"{method} is not advertised by this client, so the agent works its own disk here")


class _ExternalClient(Client):
    """The client side of one turn: forward updates, answer permission, advertise nothing else."""

    # acp declares every `Client` member abstract, so each one is defined here, even where it only refuses.

    def __init__(self, *, permission: PermissionPolicy, on_update: UpdateSink) -> None:
        self._permission = permission
        self._on_update = on_update
        self.text_parts: list[str] = []
        self.touched: list[str] = []

    async def session_update(self, session_id: str, update: AgentUpdate, **kwargs: JsonValue) -> None:
        """Forward the update, and keep the agent's text and the paths it reported touching."""
        del session_id, kwargs
        self._on_update(update)
        if isinstance(update, schema.AgentMessageChunk) and isinstance(update.content, schema.TextContentBlock):
            self.text_parts.append(update.content.text)
        if isinstance(update, schema.ToolCallStart | schema.ToolCallProgress):
            for location in update.locations or ():
                if location.path not in self.touched:
                    self.touched.append(location.path)

    async def request_permission(
        self, session_id: str, tool_call: schema.ToolCallUpdate, options: list[schema.PermissionOption], **kwargs: JsonValue
    ) -> schema.RequestPermissionResponse:
        """Ask the policy, then spell its verdict with one of the options the agent offered."""
        del kwargs
        request = schema.RequestPermissionRequest(session_id=session_id, tool_call=tool_call, options=options)
        allowed = await self._permission.decide(request)
        return schema.RequestPermissionResponse(outcome=outcome_for(options, allowed=allowed))

    async def read_text_file(
        self, session_id: str, path: str, line: int | None = None, limit: int | None = None, **kwargs: JsonValue
    ) -> schema.ReadTextFileResponse:
        del session_id, path, line, limit, kwargs
        raise _not_advertised(method="fs/read_text_file")

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: JsonValue) -> schema.WriteTextFileResponse | None:
        del session_id, path, content, kwargs
        raise _not_advertised(method="fs/write_text_file")

    async def create_terminal(  # noqa: PLR0913, PLR0917 (the signature of acp's `Client.create_terminal`)
        self,
        session_id: str,
        command: str,
        args: list[str] | None = None,
        env: list[schema.EnvVariable] | None = None,
        cwd: str | None = None,
        output_byte_limit: int | None = None,
        **kwargs: JsonValue,
    ) -> schema.CreateTerminalResponse:
        del session_id, command, args, env, cwd, output_byte_limit, kwargs
        raise _not_advertised(method="terminal/create")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: JsonValue) -> schema.TerminalOutputResponse:
        del session_id, terminal_id, kwargs
        raise _not_advertised(method="terminal/output")

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: JsonValue) -> schema.ReleaseTerminalResponse | None:
        del session_id, terminal_id, kwargs
        raise _not_advertised(method="terminal/release")

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: JsonValue) -> schema.WaitForTerminalExitResponse:
        del session_id, terminal_id, kwargs
        raise _not_advertised(method="terminal/wait_for_exit")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: JsonValue) -> schema.KillTerminalResponse | None:
        del session_id, terminal_id, kwargs
        raise _not_advertised(method="terminal/kill")

    async def create_elicitation(self, message: str, mode: schema.ElicitationMode, **kwargs: JsonValue) -> schema.CreateElicitationResponse:
        del message, mode, kwargs
        raise _not_advertised(method="session/create_elicitation")

    async def complete_elicitation(self, elicitation_id: str, **kwargs: JsonValue) -> None:
        del elicitation_id, kwargs

    async def ext_method(self, method: str, params: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
        del params
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: Mapping[str, JsonValue]) -> None:
        del method, params

    def on_connect(self, conn: acp.Agent) -> None:
        """Nothing to keep: this client only ever answers what the agent asks it."""
        del conn


@dataclass(frozen=True, kw_only=True)
class ExternalAgent:
    """One ACP agent launched by `command` and `args`, whose provider credentials must be in `env`, not the shell."""

    command: str
    args: Sequence[str] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    """Merged over the SDK's trimmed default environment. The agent inherits none of the launching shell's exports."""

    async def run(
        self, brief: str, *, cwd: Path, permission: PermissionPolicy, on_update: UpdateSink, timeout_seconds: float
    ) -> ExternalResult:
        """Run `brief`, the agent's whole prompt, as one turn in the absolute directory `cwd`, cancelled after `timeout_seconds` with its output kept. Opening the session any slower raises `TimeoutError`."""
        client = _ExternalClient(permission=permission, on_update=on_update)
        async with acp.spawn_agent_process(client, self.command, *self.args, env=dict(self.env) or None, cwd=cwd) as (conn, _process):

            async def _open_session() -> schema.NewSessionResponse:
                await conn.initialize(**_INITIALIZE)
                return await conn.new_session(cwd=str(cwd), mcp_servers=[])

            session = await asyncio.wait_for(_open_session(), timeout=timeout_seconds)
            turn = asyncio.ensure_future(conn.prompt(session_id=session.session_id, prompt=[acp.text_block(brief)]))
            try:
                done, _pending = await asyncio.wait([turn], timeout=timeout_seconds)
                if done:
                    stop_reason = turn.result().stop_reason
                else:
                    await conn.cancel(session_id=session.session_id)
                    stop_reason = await _settled(turn)
            finally:
                if not turn.done():
                    turn.cancel()
                with contextlib.suppress(asyncio.CancelledError, RequestError, ConnectionError):
                    await turn
        return ExternalResult(text="".join(client.text_parts), stop_reason=stop_reason, touched_files=tuple(client.touched))

    async def handshake(self, *, cwd: Path, timeout_seconds: float = 120.0) -> schema.InitializeResponse:
        """Spawn the agent and return its `initialize` answer, prompting nothing. An answer slower than `timeout_seconds` raises `TimeoutError`."""
        client = _ExternalClient(permission=AllowOnce(), on_update=lambda _update: None)
        async with acp.spawn_agent_process(client, self.command, *self.args, env=dict(self.env) or None, cwd=cwd) as (conn, _process):
            return await asyncio.wait_for(conn.initialize(**_INITIALIZE), timeout=timeout_seconds)


async def _settled(turn: asyncio.Task[schema.PromptResponse]) -> str:
    """The stop reason a cancelled turn answered with, or `'cancelled'` when it never answered or its process disconnected."""
    try:
        response = await asyncio.wait_for(turn, timeout=CANCEL_GRACE_SECONDS)
    except (TimeoutError, asyncio.CancelledError, RequestError, ConnectionError):
        return "cancelled"
    return response.stop_reason
