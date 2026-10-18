"""The ACP client, driven against a real agent subprocess.

The double is a hand-written ACP agent script, not a mock: the whole point of this package is the
wire, so the test spawns a process and speaks the protocol to it. `AllowOnce` and a rejecting
policy prove both permission verdicts, and `fs/read_text_file`, one of the capabilities we never
advertise, is proved by asking for it from the agent side.
"""

from __future__ import annotations

import asyncio
import gc
import os
import sys
import time

from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from harness.adapters.acp_client import AllowOnce, ExternalAgent, ExternalResult

if TYPE_CHECKING:
    from acp import schema

    from harness.adapters.acp_client import AgentUpdate

ECHO_AGENT = Path(__file__).resolve().parents[1] / "support" / "acp_echo_agent.py"


@dataclass
class Recorder:
    """Collects every forwarded update, in order."""

    updates: list[AgentUpdate] = field(default_factory=list)

    def __call__(self, update: AgentUpdate) -> None:
        self.updates.append(update)


@dataclass
class RejectAll:
    """A `PermissionPolicy` that refuses everything, so the reject arm is exercised."""

    seen: list[str] = field(default_factory=list)

    async def decide(self, request: schema.RequestPermissionRequest) -> bool:
        self.seen.append(request.tool_call.tool_call_id)
        return False


def _echo(tmp_path: Path, *args: str) -> ExternalAgent:
    return ExternalAgent(command=sys.executable, args=(str(ECHO_AGENT), *args), env={"PYTHONUNBUFFERED": "1"})


async def test_a_turn_comes_back_as_text_a_stop_reason_and_the_files_it_touched(tmp_path: Path) -> None:
    recorder = Recorder()

    result = await _echo(tmp_path).run(
        "do the thing", cwd=tmp_path, permission=AllowOnce(), on_update=recorder, timeout_seconds=30.0
    )

    assert isinstance(result, ExternalResult)
    assert result.text == f"echo do the thing in {tmp_path} wrote it"
    assert result.stop_reason == "end_turn"
    assert result.touched_files == ("notes.txt",)


async def test_every_session_update_reaches_the_sink_in_order(tmp_path: Path) -> None:
    recorder = Recorder()

    await _echo(tmp_path).run("hello", cwd=tmp_path, permission=AllowOnce(), on_update=recorder, timeout_seconds=30.0)

    kinds = [update.session_update for update in recorder.updates]
    assert kinds == ["agent_message_chunk", "agent_message_chunk", "tool_call", "agent_message_chunk"]


async def test_a_refusal_is_answered_and_the_agent_reports_it(tmp_path: Path) -> None:
    policy = RejectAll()

    result = await _echo(tmp_path).run("hello", cwd=tmp_path, permission=policy, on_update=Recorder(), timeout_seconds=30.0)

    assert policy.seen == ["write-1"]
    assert result.text == f"echo hello in {tmp_path} skipped it"
    assert result.touched_files == ()


async def test_a_client_method_we_never_advertised_answers_with_a_named_refusal(tmp_path: Path) -> None:
    """`fs/read_text_file` is proved unadvertised by asking for it from the agent side."""
    probing = _echo(tmp_path, "probe_unadvertised_read")

    result = await probing.run("go", cwd=tmp_path, permission=AllowOnce(), on_update=Recorder(), timeout_seconds=30.0)

    assert result.stop_reason == "end_turn"
    assert "fs/read_text_file" in result.text
    assert "not advertised" in result.text


async def test_a_disconnect_during_the_cancel_grace_window_still_settles_as_cancelled(tmp_path: Path) -> None:
    """A process that dies answering cancel still settles as cancelled, not left hanging."""
    disconnecting = _echo(tmp_path, "disconnect_on_cancel")

    result = await disconnecting.run("go", cwd=tmp_path, permission=AllowOnce(), on_update=Recorder(), timeout_seconds=1.0)

    assert result.stop_reason == "cancelled"
    pid = int(result.text.removeprefix("pid "))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_a_handshake_answers_a_protocol_version_without_prompting(tmp_path: Path) -> None:
    """The cheap liveness check an adapter probe uses: no prompt, so no model and no credential."""
    answer = await _echo(tmp_path).handshake(cwd=tmp_path)

    assert answer.protocol_version >= 1


async def test_a_handshake_names_this_client_as_the_harness_at_its_installed_version(tmp_path: Path) -> None:
    answer = await _echo(tmp_path).handshake(cwd=tmp_path)

    assert answer.agent_info is not None
    assert answer.agent_info.name == "echo for harness"
    assert answer.agent_info.version == version("harness")


async def test_a_handshake_past_its_timeout_raises_and_leaves_no_child_process(tmp_path: Path) -> None:
    """An adapter that starts but never answers `initialize` does not hang the handshake forever."""
    hanging = _echo(tmp_path, "hangs_on_initialize")
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        await hanging.handshake(cwd=tmp_path, timeout_seconds=0.5)

    assert time.monotonic() - started < 1.0
    pid = int((tmp_path / "hung.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


async def test_the_working_directory_is_the_one_we_asked_for(tmp_path: Path) -> None:
    """`session/new` gets an absolute path, which is what ACP requires of a client."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    recorder = Recorder()

    result = await _echo(tmp_path).run("hi", cwd=workspace, permission=AllowOnce(), on_update=recorder, timeout_seconds=30.0)

    assert result.stop_reason == "end_turn"
    assert str(workspace) in result.text


SLOW_AGENT = Path(__file__).resolve().parents[1] / "support" / "acp_slow_agent.py"


async def test_a_turn_past_its_timeout_is_cancelled_and_still_returns_what_streamed(tmp_path: Path) -> None:
    recorder = Recorder()
    slow = ExternalAgent(command=sys.executable, args=(str(SLOW_AGENT),), env={"PYTHONUNBUFFERED": "1"})

    result = await slow.run("go", cwd=tmp_path, permission=AllowOnce(), on_update=recorder, timeout_seconds=1.0)

    assert result.stop_reason == "cancelled"
    assert result.text == "starting"
    assert result.touched_files == ()
    assert [update.session_update for update in recorder.updates] == ["agent_message_chunk"]


async def test_run_cancelled_from_outside_mid_turn_does_not_leak_the_turn(tmp_path: Path) -> None:
    """Cancelling `run` itself does not leave the turn's exception unretrieved or the process alive."""
    slow = ExternalAgent(command=sys.executable, args=(str(SLOW_AGENT),), env={"PYTHONUNBUFFERED": "1"})
    started = asyncio.Event()

    def on_started(update: AgentUpdate) -> None:
        del update
        started.set()

    loop = asyncio.get_running_loop()
    reported: list[object] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    before = asyncio.all_tasks()
    try:
        running = asyncio.ensure_future(
            slow.run("go", cwd=tmp_path, permission=AllowOnce(), on_update=on_started, timeout_seconds=30.0)
        )
        await asyncio.wait_for(started.wait(), timeout=5.0)
        running.cancel()

        with pytest.raises(asyncio.CancelledError):
            await running

        del running
        for _ in range(5):
            gc.collect()
            await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(None)

    assert asyncio.all_tasks() - before == set()
    assert reported == []
    pid = int((tmp_path / "slow.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
