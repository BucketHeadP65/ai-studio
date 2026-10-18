"""The terminal websocket: every gate passes before a pty opens, in the order feature on, exact origin, `cwd`.

`cwd` resolves with symlinks followed and must be a directory inside a registered knowledge base root or this
checkout. The roots are read on every connect. Every refusal closes with `POLICY_VIOLATION`, and its reason names
nothing about the filesystem beyond "not a registered root".

The gate fixes only the starting directory. The child may `cd` anywhere its permissions allow, and it inherits this
server's whole environment, database DSNs, tokens and model keys included.

Wire framing: binary frames carry raw pty bytes both ways, keystrokes in and the child's output out. A text frame
is JSON control with one shape, `{"type": "resize", "cols": <int>, "rows": <int>}`, both from 1 to 65535 (the
unsigned short range a pty size is encoded in). Any other text frame is dropped and the connection stays open.
"""

from __future__ import annotations

import asyncio

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, WebSocket
from pydantic import BaseModel, Field, ValidationError
from starlette.websockets import WebSocketState

from harness.adapters.contracts import StudioApi, registered_knowledge_base_roots
from harness.features.terminal.internal_contracts import PtySpawner
from harness.settings import AI_FACTORY_STUDIO_ORIGINS, TerminalSettings

if TYPE_CHECKING:
    from harness.features.terminal.infrastructure.pty_session import PtySession

FEATURE_OFF = "terminal disabled"
"""The close reason when this deployment has not turned the terminal on."""

ORIGIN_NOT_ALLOWED = "origin not allowed"
"""The close reason when the handshake's `Origin` is not one of AI Factory Studio's origins."""

POLICY_VIOLATION = 1008
"""The close code of every refusal, the "policy violation" code of RFC 6455 section 7.4.1."""

MAX_TERMINAL_DIMENSION = 65535
"""The largest `cols` or `rows` a resize frame may carry."""


class _ResizeControl(BaseModel):
    """The one control-frame shape the terminal accepts: a client-declared size change."""

    type: Literal["resize"]
    cols: int = Field(gt=0, le=MAX_TERMINAL_DIMENSION)
    rows: int = Field(gt=0, le=MAX_TERMINAL_DIMENSION)


def _resolve_cwd(raw: str, roots: tuple[Path, ...]) -> Path:
    """`raw` resolved with symlinks followed, or `ValueError` when it is not a directory inside one of `roots`."""
    refusal = "not a registered root"
    try:
        target = Path(raw).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ValueError(refusal) from exc
    if not target.is_dir():
        raise ValueError(refusal)
    if not any(target.is_relative_to(root) for root in roots):
        raise ValueError(refusal)
    return target


async def _bridge(websocket: WebSocket, session: PtySession) -> None:
    """Run both pumps until either ends (end of output, a disconnect or a crash), then stop the other."""
    output = asyncio.ensure_future(_pump_output(websocket, session))
    incoming = asyncio.ensure_future(_pump_input(websocket, session))
    pumps = (output, incoming)
    try:
        await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for pump in pumps:
            pump.cancel()
        await asyncio.gather(*pumps, return_exceptions=True)


async def _pump_output(websocket: WebSocket, session: PtySession) -> None:
    """Pty -> socket: forward the child's output until it closes or the socket is gone."""
    while True:
        chunk = await session.read()
        if not chunk:
            return
        if websocket.client_state != WebSocketState.CONNECTED:
            return
        await websocket.send_bytes(chunk)


async def _pump_input(websocket: WebSocket, session: PtySession) -> None:
    """Socket -> pty: a binary frame is keystrokes, a text frame is a control message."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        data = message.get("bytes")
        if data is not None:
            session.write(data)
            continue
        text = message.get("text")
        if text is not None:
            _handle_control(session, text)


def _handle_control(session: PtySession, text: str) -> None:
    """Apply one control frame, and drop it when it is not a valid resize."""
    try:
        control = _ResizeControl.model_validate_json(text)
    except ValidationError:
        return
    session.resize(control.cols, control.rows)


class TerminalController:
    """The terminal websocket: every gate passes before a pty is ever opened."""

    router = APIRouter(prefix="/api/terminal", tags=["terminal"])

    def __init__(self, settings: TerminalSettings, studio: StudioApi, spawner: PtySpawner) -> None:
        """Hold the gate's setting, the client the registered roots are read over, and how a pty is opened.

        Args:
            settings: Whether this deployment turned the terminal on.
            studio: The studio API the registered knowledge base roots are read over, on every connect.
            spawner: How a pty is opened once every gate has passed.
        """
        self._settings = settings
        self._studio = studio
        self._spawner = spawner

    @router.websocket("/ws")
    async def terminal_ws(self, websocket: WebSocket, cwd: str) -> None:
        """Open a terminal in the folder `cwd` names, once every gate has passed."""
        if not self._settings.enabled:
            await websocket.close(code=POLICY_VIOLATION, reason=FEATURE_OFF)
            return
        # A websocket handshake is never CORS-preflighted, so without this check any web page could open a shell.
        if websocket.headers.get("origin") not in AI_FACTORY_STUDIO_ORIGINS:
            await websocket.close(code=POLICY_VIOLATION, reason=ORIGIN_NOT_ALLOWED)
            return
        try:
            roots = await registered_knowledge_base_roots(self._studio)
            target = _resolve_cwd(cwd, roots)
        except ValueError as exc:
            await websocket.close(code=POLICY_VIOLATION, reason=str(exc))
            return

        await websocket.accept()
        session = self._spawner.spawn(target)
        try:
            await _bridge(websocket, session)
        finally:
            await session.close()
