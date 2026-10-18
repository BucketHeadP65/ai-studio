"""A hand-written ACP agent process, the double the external-agent plugin is driven against.

One turn does four things in order: echo the prompt back as an agent message chunk, report the
working directory in a second chunk, ask the client for permission to write one file, and report
that file as touched by a completed tool call before ending the turn. Answering the permission
with a rejection skips the write and says so, so both verdicts are observable from the client
side. In `DISCONNECT_ON_CANCEL` mode the turn instead reports this process's id and hangs, and
`cancel` kills the process outright rather than answering, so a client sees the connection drop
mid-turn. In `HANGS_ON_INITIALIZE` mode `initialize` writes this process's id to a file in `cwd`
and then never answers, so a client's handshake timeout has something to prove itself against. In
`PROBE_UNADVERTISED_READ` mode the turn instead calls the client's `fs/read_text_file` and reports
the error text as its one chunk, proving the method is refused as not advertised.
"""

from __future__ import annotations

import asyncio
import os
import sys

from pathlib import Path
from typing import TYPE_CHECKING

import acp

from acp import Client, RequestError, schema

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

TOUCHED_FILE = "notes.txt"
"""The one path this agent reports touching, so a client can assert on it."""

DISCONNECT_ON_CANCEL = "disconnect_on_cancel"
"""The mode where `cancel` kills the process instead of answering, so a client sees the
connection drop mid-turn rather than a cancelled response."""

HANGS_ON_INITIALIZE = "hangs_on_initialize"
"""The mode where `initialize` writes this process's id to `HUNG_PID_FILE` in `cwd` and then
never answers, so a client's handshake timeout has a process to prove is gone afterward."""

HUNG_PID_FILE = "hung.pid"
"""Where `HANGS_ON_INITIALIZE` mode writes this process's id, relative to `cwd`."""

PROBE_UNADVERTISED_READ = "probe_unadvertised_read"
"""The mode where the turn calls the client's `fs/read_text_file` and reports the error text as
its one chunk, instead of the normal echo flow."""


# Every method below mirrors the SDK's `acp.Agent` protocol signature exactly, `**kwargs: object`
# included: the router passes each request model's fields as keywords, and a concrete agent must
# cover the whole protocol surface because the SDK marks its members abstract.
class EchoAgent(acp.Agent):
    """Echoes, asks once, reports one touched file, ends the turn.

    A `mode` of `DISCONNECT_ON_CANCEL` makes the turn report this process's id and then hang, and
    makes `cancel` kill the process outright instead of answering. A `mode` of
    `HANGS_ON_INITIALIZE` makes `initialize` itself hang. A `mode` of `PROBE_UNADVERTISED_READ`
    makes the turn call the client's `fs/read_text_file` and report the error text instead.
    """

    def __init__(self, mode: str = "echo") -> None:
        self._client: Client | None = None
        self._sessions = 0
        self._cwd: str | None = None
        self._mode = mode

    def on_connect(self, conn: Client) -> None:
        """Keep the outbound connection: updates and permission requests ride it."""
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: object,
    ) -> schema.InitializeResponse:
        """Answer the client's version, advertising no capability and naming itself after the caller.

        In `HANGS_ON_INITIALIZE` mode, writes this process's id to `HUNG_PID_FILE` and hangs.
        """
        del client_capabilities, kwargs
        if self._mode == HANGS_ON_INITIALIZE:
            Path(HUNG_PID_FILE).write_text(str(os.getpid()), encoding="utf-8")
            await asyncio.Event().wait()
        caller = None if client_info is None else schema.Implementation(name=f"echo for {client_info.name}", version=client_info.version)
        return schema.InitializeResponse(protocol_version=protocol_version, agent_info=caller)

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: Sequence[object] | None = None,
        **kwargs: object,
    ) -> schema.NewSessionResponse:
        """Mint a session id and remember the working directory for prompt to report."""
        del additional_directories, mcp_servers, kwargs
        self._sessions += 1
        self._cwd = cwd
        return schema.NewSessionResponse(session_id=f"echo-{self._sessions}")

    async def prompt(
        self,
        session_id: str,
        prompt: list[
            schema.TextContentBlock
            | schema.ImageContentBlock
            | schema.AudioContentBlock
            | schema.ResourceContentBlock
            | schema.EmbeddedResourceContentBlock
        ],
        **kwargs: object,
    ) -> schema.PromptResponse:
        """Echo, report the working directory, ask, report, end.

        In `DISCONNECT_ON_CANCEL` mode, report this process's id instead and hang. In
        `PROBE_UNADVERTISED_READ` mode, ask the client to read a file and report its refusal
        instead.
        """
        del kwargs
        if self._client is None:
            raise RequestError(-32603, "the echo agent has no connection")
        if self._mode == DISCONNECT_ON_CANCEL:
            await self._client.session_update(
                session_id=session_id,
                update=schema.AgentMessageChunk(
                    session_update="agent_message_chunk", content=acp.text_block(f"pid {os.getpid()}")
                ),
            )
            await asyncio.Event().wait()
            return schema.PromptResponse(stop_reason="cancelled")
        if self._mode == PROBE_UNADVERTISED_READ:
            try:
                await self._client.read_text_file(session_id=session_id, path="probe.txt")
            except RequestError as exc:
                refusal = str(exc)
            else:
                refusal = "fs/read_text_file answered instead of refusing"
            await self._client.session_update(
                session_id=session_id,
                update=schema.AgentMessageChunk(session_update="agent_message_chunk", content=acp.text_block(refusal)),
            )
            return schema.PromptResponse(stop_reason="end_turn")
        said = " ".join(block.text for block in prompt if isinstance(block, schema.TextContentBlock))
        await self._client.session_update(
            session_id=session_id,
            update=schema.AgentMessageChunk(session_update="agent_message_chunk", content=acp.text_block(f"echo {said}")),
        )
        await self._client.session_update(
            session_id=session_id,
            update=schema.AgentMessageChunk(
                session_update="agent_message_chunk", content=acp.text_block(f" in {self._cwd}")
            ),
        )
        answer = await self._client.request_permission(
            session_id=session_id,
            tool_call=schema.ToolCallUpdate(tool_call_id="write-1", title=f"write {TOUCHED_FILE}", kind="edit", status="pending"),
            options=[
                schema.PermissionOption(option_id="allow_once", name="Allow", kind="allow_once"),
                schema.PermissionOption(option_id="reject_once", name="Reject", kind="reject_once"),
            ],
        )
        allowed = isinstance(answer.outcome, schema.AllowedOutcome) and answer.outcome.option_id == "allow_once"
        await self._client.session_update(
            session_id=session_id,
            update=schema.ToolCallStart(
                session_update="tool_call",
                tool_call_id="write-1",
                title=f"write {TOUCHED_FILE}",
                kind="edit",
                status="completed" if allowed else "failed",
                locations=[schema.ToolCallLocation(path=TOUCHED_FILE)] if allowed else None,
            ),
        )
        await self._client.session_update(
            session_id=session_id,
            update=schema.AgentMessageChunk(
                session_update="agent_message_chunk", content=acp.text_block(" wrote it" if allowed else " skipped it")
            ),
        )
        return schema.PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id: str, **kwargs: object) -> None:
        """Nothing runs long enough here to cancel, except in `DISCONNECT_ON_CANCEL` mode, which exits instead of answering."""
        del session_id, kwargs
        if self._mode == DISCONNECT_ON_CANCEL:
            os._exit(1)

    async def authenticate(self, method_id: str, **kwargs: object) -> schema.AuthenticateResponse | None:
        """No authentication."""
        del method_id, kwargs
        return None

    async def load_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: Sequence[object] | None = None,
        additional_directories: list[str] | None = None,
        **kwargs: object,
    ) -> schema.LoadSessionResponse | None:
        del cwd, session_id, mcp_servers, additional_directories, kwargs
        method = "session/load"
        raise RequestError.method_not_found(method)

    async def list_sessions(self, cwd: str | None = None, cursor: str | None = None, **kwargs: object) -> schema.ListSessionsResponse:
        del cwd, cursor, kwargs
        method = "session/list"
        raise RequestError.method_not_found(method)

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs: object) -> schema.SetSessionModeResponse | None:
        del session_id, mode_id, kwargs
        method = "session/set_mode"
        raise RequestError.method_not_found(method)

    async def set_config_option(
        self, config_id: str, session_id: str, value: str | bool, **kwargs: object
    ) -> schema.SetSessionConfigOptionResponse | None:
        del config_id, session_id, value, kwargs
        method = "session/set_config_option"
        raise RequestError.method_not_found(method)

    async def fork_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: Sequence[object] | None = None,
        **kwargs: object,
    ) -> schema.ForkSessionResponse:
        del session_id, cwd, additional_directories, mcp_servers, kwargs
        method = "session/fork"
        raise RequestError.method_not_found(method)

    async def resume_session(
        self,
        session_id: str,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: Sequence[object] | None = None,
        **kwargs: object,
    ) -> schema.ResumeSessionResponse:
        del session_id, cwd, additional_directories, mcp_servers, kwargs
        method = "session/resume"
        raise RequestError.method_not_found(method)

    async def close_session(self, session_id: str, **kwargs: object) -> schema.CloseSessionResponse | None:
        del session_id, kwargs
        return schema.CloseSessionResponse()

    async def ext_method(self, method: str, params: Mapping[str, object]) -> dict[str, object]:
        del params
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: Mapping[str, object]) -> None:
        del method, params


if __name__ == "__main__":
    _mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    asyncio.run(acp.run_agent(EchoAgent(mode=_mode)))
