"""An ACP agent process whose turn never finishes on its own, for the client's timeout path.

It streams one chunk so a client can prove that what arrived before the cancel is still returned,
then sleeps. `session/cancel` sets the flag its prompt loop watches, and the turn answers with
`cancelled` the way a real agent's would. It also writes its own process id to `PID_FILE` in
`cwd` before streaming, so a client can confirm the process is gone once the turn ends.
"""

from __future__ import annotations

import asyncio
import os

from pathlib import Path
from typing import TYPE_CHECKING

import acp

from acp import Client, RequestError, schema

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

PID_FILE = "slow.pid"
"""Where this agent writes its own process id, relative to `cwd`."""


# The method signatures mirror the SDK's `acp.Agent` protocol exactly, `**kwargs: object` included,
# for the reason stated in `acp_echo_agent.py`.
class SlowAgent(acp.Agent):
    """Streams one chunk, then waits to be cancelled."""

    def __init__(self) -> None:
        self._client: Client | None = None
        self._cancelled = asyncio.Event()

    def on_connect(self, conn: Client) -> None:
        """Keep the outbound connection for streaming."""
        self._client = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: schema.ClientCapabilities | None = None,
        client_info: schema.Implementation | None = None,
        **kwargs: object,
    ) -> schema.InitializeResponse:
        """Answer the client's version."""
        del client_capabilities, client_info, kwargs
        return schema.InitializeResponse(protocol_version=protocol_version)

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: Sequence[object] | None = None,
        **kwargs: object,
    ) -> schema.NewSessionResponse:
        """One session id, always the same."""
        del cwd, additional_directories, mcp_servers, kwargs
        return schema.NewSessionResponse(session_id="slow-1")

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
        """Say one thing, then wait for the cancel that ends the turn."""
        del prompt, kwargs
        if self._client is None:
            raise RequestError(-32603, "the slow agent has no connection")
        Path(PID_FILE).write_text(str(os.getpid()), encoding="utf-8")
        await self._client.session_update(
            session_id=session_id,
            update=schema.AgentMessageChunk(session_update="agent_message_chunk", content=acp.text_block("starting")),
        )
        await self._cancelled.wait()
        return schema.PromptResponse(stop_reason="cancelled")

    async def cancel(self, session_id: str, **kwargs: object) -> None:
        """End the waiting turn."""
        del session_id, kwargs
        self._cancelled.set()

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
    asyncio.run(acp.run_agent(SlowAgent()))
