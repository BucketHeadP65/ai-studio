"""The supervised child process (`core.supervisor`).

Driven against a real child script under `tmp_path`, never a patched `asyncio` — the spawn callable
is the seam. A child that exits on its own comes back; a spawn that fails is retried on the same
backoff instead of ending supervision; `stop` terminates it, never respawns, and never re-raises
what supervision died of.
"""

from __future__ import annotations

import asyncio
import sys

from typing import TYPE_CHECKING

from core.supervisor import SupervisedChild

if TYPE_CHECKING:
    from asyncio.subprocess import Process
    from pathlib import Path

    from core.supervisor import Spawn

_CHILD = """
import pathlib
import sys
import time

marker = pathlib.Path(sys.argv[1])
with marker.open("a", encoding="utf-8") as handle:
    handle.write("started\\n")
if sys.argv[2] == "exit":
    raise SystemExit(3)
time.sleep(30)
"""


def _child_script(tmp_path: Path) -> Path:
    """Write the child program and answer its path."""
    script = tmp_path / "child.py"
    script.write_text(_CHILD, encoding="utf-8")
    return script


class _FlakySpawn:
    """A spawn callable that raises its first `failures` calls, then delegates to `inner`."""

    def __init__(self, inner: Spawn, *, failures: int) -> None:
        self._inner = inner
        self.failures = failures
        self.calls = 0

    async def __call__(self) -> Process:
        self.calls += 1
        if self.calls <= self.failures:
            msg = "the child could not be started"
            raise OSError(msg)
        return await self._inner()


class _RaisingSleep:
    """A backoff wait that always raises, ending the supervising task the way a stray failure would."""

    def __init__(self) -> None:
        self.called = asyncio.Event()

    async def __call__(self, seconds: float) -> None:
        del seconds
        self.called.set()
        msg = "the wait failed"
        raise RuntimeError(msg)


def _spawn(script: Path, marker: Path, mode: str) -> Spawn:
    """A spawn callable starting the child script in `mode` (`exit` or `sleep`)."""

    async def spawn() -> Process:
        return await asyncio.create_subprocess_exec(sys.executable, str(script), str(marker), mode)

    return spawn


async def _wait_for_starts(marker: Path, count: int) -> None:
    """Wait until the child has recorded at least `count` starts."""
    async with asyncio.timeout(10):
        while True:
            if _starts(marker) >= count:
                return
            await asyncio.sleep(0.02)


def _starts(marker: Path) -> int:
    """How many times the child has started so far."""
    return len(marker.read_text(encoding="utf-8").splitlines()) if marker.exists() else 0


class TestSupervisedChild:
    async def test_a_child_that_exits_is_respawned(self, tmp_path: Path):
        marker = tmp_path / "starts.txt"
        child = SupervisedChild(
            _spawn(_child_script(tmp_path), marker, "exit"), name="test-child", backoff_seconds=0.01, backoff_cap_seconds=0.02
        )

        child.start()
        await _wait_for_starts(marker, 3)
        await child.stop()

        assert _starts(marker) >= 3
        assert child.running is False

    async def test_stop_terminates_a_live_child_and_never_respawns_it(self, tmp_path: Path):
        marker = tmp_path / "starts.txt"
        child = SupervisedChild(_spawn(_child_script(tmp_path), marker, "sleep"), name="test-child", backoff_seconds=0.01)

        child.start()
        await _wait_for_starts(marker, 1)
        await child.stop()
        await asyncio.sleep(0.2)

        assert _starts(marker) == 1
        assert child.running is False

    async def test_stop_before_start_is_a_no_op(self, tmp_path: Path):
        marker = tmp_path / "starts.txt"
        child = SupervisedChild(_spawn(_child_script(tmp_path), marker, "sleep"), name="test-child")

        await child.stop()  # must not raise

        assert child.running is False
        assert _starts(marker) == 0

    async def test_a_spawn_that_fails_is_retried_rather_than_ending_supervision(self, tmp_path: Path):
        marker = tmp_path / "starts.txt"
        spawn = _FlakySpawn(_spawn(_child_script(tmp_path), marker, "sleep"), failures=1)
        child = SupervisedChild(spawn, name="test-child", backoff_seconds=0.01, backoff_cap_seconds=0.02)

        child.start()
        await _wait_for_starts(marker, 1)

        assert spawn.calls == 2  # the failed attempt, then the one that started the child
        assert child.running is True
        await child.stop()

    async def test_stop_never_raises_what_supervision_died_of(self, tmp_path: Path):
        marker = tmp_path / "starts.txt"
        sleep = _RaisingSleep()
        child = SupervisedChild(
            _FlakySpawn(_spawn(_child_script(tmp_path), marker, "sleep"), failures=99),
            name="test-child",
            backoff_seconds=0.01,
            sleep=sleep,
        )

        child.start()
        async with asyncio.timeout(10):
            await sleep.called.wait()
        assert child.running is False  # the raised wait ended supervision

        await child.stop()  # must not re-raise what it died of

        assert child.running is False
