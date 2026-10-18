"""The terminal websocket's four hard gates -- every refusal happens before a pty exists.

`/api/terminal/ws` is shell access to the machine behind the agent server, so every test here
proves a refusal rather than a success: the gate order is enabled -> origin -> cwd, and each one
closes the socket with a policy-violation code before a pty is ever opened (no pty layer is faked
for the gate tests, because none of these paths ever touch it). The one full pipeline test spawns a
real `/bin/cat` behind the gates to prove the assembled route -- not just its parts -- echoes.

`TerminalController` is constructed directly and driven over a bare app through
`tests.support.controllers.mounted`, wrapped in Starlette's own `TestClient` for the websocket
handshake (`httpx`, and so `tests.support.controllers.a_client_of`, has no websocket support).
"""

from __future__ import annotations

import json
import os
import time

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from starlette.testclient import TestClient, WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from harness.features.terminal.controller import POLICY_VIOLATION, TerminalController
from harness.features.terminal.infrastructure.pty_session import PtySession
from harness.settings import AI_FACTORY_STUDIO_ORIGINS, TerminalSettings
from tests.support.controllers import mounted
from tests.support.fakes.studio_registry import FakeStudioApi, registry_row, unreachable_studio

if TYPE_CHECKING:
    from pathlib import Path

    from harness.features.terminal.internal_contracts import PtySpawner

STUDIO_ORIGIN = "http://localhost:3031"
REAP_DEADLINE_SECONDS = 5.0
"""How long the deadlock-repro test waits for a reap before failing -- generous, since the whole
point of the finding this pins is that the old ordering never reaped at all."""


@dataclass
class _CapturingSpawner:
    """Wraps the real `PtySession.spawn`: fixes the command it runs, and remembers what it opened."""

    command: tuple[str, ...] | None = None
    spawned: list[PtySession] = field(default_factory=list)

    def spawn(self, cwd: Path) -> PtySession:
        """Open a real session behind `self.command`, and keep it for the test to inspect."""
        opened = PtySession.spawn(cwd, self.command)
        self.spawned.append(opened)
        return opened


def _studio_over(root: Path) -> FakeStudioApi:
    """A studio answering one registered knowledge base at `root`."""
    return FakeStudioApi(rows=[registry_row("kb", root)])


def _controller(
    root: Path, *, enabled: bool = True, studio: FakeStudioApi | None = None, spawner: PtySpawner = PtySession
) -> TerminalController:
    """A controller with the feature on and `root` as the one registered knowledge base, unless overridden."""
    return TerminalController(
        settings=TerminalSettings(enabled=enabled), studio=studio if studio is not None else _studio_over(root), spawner=spawner
    )


def _client(controller: TerminalController) -> TestClient:
    """A `TestClient` over `controller`'s own routes alone."""
    return TestClient(mounted(controller))


def _refused(client: TestClient, url: str, *, origin: str | None = STUDIO_ORIGIN) -> WebSocketDisconnect:
    """Connect to `url`; the handshake must be refused rather than ever completing."""
    headers = {} if origin is None else {"Origin": origin}
    with pytest.raises(WebSocketDisconnect) as excinfo, client.websocket_connect(url, headers=headers):
        pass
    return excinfo.value


def _collect_until(ws: WebSocketTestSession, needle: bytes, attempts: int = 20) -> bytes:
    """Accumulate binary frames until `needle` appears, or give up after `attempts`."""
    collected = b""
    for _ in range(attempts):
        collected += ws.receive_bytes()
        if needle in collected:
            break
    return collected


def _assert_reaped_within(pid: int, deadline_seconds: float) -> None:
    """Poll `os.kill(pid, 0)` until it raises (the process is gone) or the deadline passes."""
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    message = f"pid {pid} is still reachable {deadline_seconds}s after disconnect"
    raise AssertionError(message)


@pytest.fixture
def roots(tmp_path: Path) -> Path:
    """One registered root the gate tests may open a terminal inside."""
    root = tmp_path / "knowledge_base"
    root.mkdir()
    return root


def test_gate_off_refuses_before_origin_or_cwd_are_even_checked(roots: Path) -> None:
    client = _client(_controller(roots, enabled=False))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={roots}")

    assert disconnect.code == POLICY_VIOLATION


def test_a_foreign_origin_is_refused(roots: Path) -> None:
    client = _client(_controller(roots))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={roots}", origin="https://evil.example")

    assert disconnect.code == POLICY_VIOLATION


def test_a_missing_origin_header_is_refused(roots: Path) -> None:
    client = _client(_controller(roots))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={roots}", origin=None)

    assert disconnect.code == POLICY_VIOLATION


@pytest.mark.parametrize("origin", AI_FACTORY_STUDIO_ORIGINS)
def test_every_ai_factory_studio_origin_passes_the_origin_gate(tmp_path: Path, roots: Path, origin: str) -> None:
    """A `cwd` outside every root is refused by the third gate, so the origin gate let this origin through."""
    outside = tmp_path / "not-registered"
    outside.mkdir()
    client = _client(_controller(roots))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={outside}", origin=origin)

    assert disconnect.reason == "not a registered root"


def test_a_cwd_outside_every_registered_root_is_refused(tmp_path: Path, roots: Path) -> None:
    outside = tmp_path / "not-registered"
    outside.mkdir()
    client = _client(_controller(roots))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={outside}")

    assert disconnect.code == POLICY_VIOLATION


def test_a_symlink_escaping_the_registered_root_is_refused(tmp_path: Path, roots: Path) -> None:
    outside = tmp_path / "not-registered"
    outside.mkdir()
    escape = roots / "escape"
    escape.symlink_to(outside)
    client = _client(_controller(roots))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={escape}")

    assert disconnect.code == POLICY_VIOLATION


def test_a_cwd_that_does_not_exist_is_refused_the_same_generic_way(roots: Path) -> None:
    client = _client(_controller(roots))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={roots / 'nowhere'}")

    assert disconnect.code == POLICY_VIOLATION


def test_a_cwd_that_is_a_file_not_a_directory_is_refused(roots: Path) -> None:
    a_file = roots / "knowledge-base-schema.json"
    a_file.write_text("{}")
    client = _client(_controller(roots))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={a_file}")

    assert disconnect.code == POLICY_VIOLATION


def test_a_refusal_never_names_the_filesystem(roots: Path) -> None:
    """The `cwd`-containment gate's reason says "not a registered root", nothing more specific."""
    client = _client(_controller(roots))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={roots / 'nowhere'}")

    reason = (disconnect.reason or "").lower()
    assert "nowhere" not in reason
    assert str(roots) not in reason


def test_every_gate_passing_opens_a_real_pty_that_echoes(roots: Path) -> None:
    """The assembled route, not just its parts: gates pass, a real `/bin/cat` answers what it is sent."""
    client = _client(_controller(roots, spawner=_CapturingSpawner(command=("/bin/cat",))))

    with client.websocket_connect(f"/api/terminal/ws?cwd={roots}", headers={"Origin": STUDIO_ORIGIN}) as ws:
        ws.send_bytes(b"hi\n")
        assert b"hi" in _collect_until(ws, b"hi")


def test_an_oversized_resize_frame_is_dropped_without_killing_the_session(roots: Path) -> None:
    client = _client(_controller(roots, spawner=_CapturingSpawner(command=("/bin/cat",))))

    with client.websocket_connect(f"/api/terminal/ws?cwd={roots}", headers={"Origin": STUDIO_ORIGIN}) as ws:
        ws.send_text(json.dumps({"type": "resize", "cols": 999_999, "rows": 40}))
        ws.send_bytes(b"still alive\n")
        assert b"still alive" in _collect_until(ws, b"still alive")


def test_a_zero_valued_resize_frame_is_dropped_without_killing_the_session(roots: Path) -> None:
    client = _client(_controller(roots, spawner=_CapturingSpawner(command=("/bin/cat",))))

    with client.websocket_connect(f"/api/terminal/ws?cwd={roots}", headers={"Origin": STUDIO_ORIGIN}) as ws:
        ws.send_text(json.dumps({"type": "resize", "cols": 0, "rows": 0}))
        ws.send_bytes(b"still alive\n")
        assert b"still alive" in _collect_until(ws, b"still alive")


def test_disconnect_while_the_child_is_idle_still_reaps_it_within_a_bounded_time(roots: Path) -> None:
    """The regression case for the close()-ordering deadlock.

    Nothing is ever sent, so the output pump's `os.read(master_fd)` sits blocked in another
    thread with the child alive and silent -- exactly the state the old ordering (closing the
    master fd before killing the child) could hang forever on. The child never terminates on its
    own here, so a bounded reap is only possible if `close()` kills it first.
    """
    spawner = _CapturingSpawner(command=("/bin/cat",))
    client = _client(_controller(roots, spawner=spawner))

    with client.websocket_connect(f"/api/terminal/ws?cwd={roots}", headers={"Origin": STUDIO_ORIGIN}):
        assert len(spawner.spawned) == 1
        os.kill(spawner.spawned[0].process.pid, 0)  # alive while the socket is open -- otherwise this proves nothing

    _assert_reaped_within(spawner.spawned[0].process.pid, REAP_DEADLINE_SECONDS)


def test_a_studio_that_does_not_answer_refuses_cleanly_instead_of_a_traceback(roots: Path) -> None:
    """The roots come from the studio, and a studio that is down must narrow the gate, not crash it.

    The real reader runs here, against a studio that refuses every request: a registered folder
    the gate can no longer confirm is refused, with a policy violation rather than a traceback.
    """
    client = _client(_controller(roots, studio=unreachable_studio()))

    disconnect = _refused(client, f"/api/terminal/ws?cwd={roots}")

    assert disconnect.code == POLICY_VIOLATION
