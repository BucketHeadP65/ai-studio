"""What the app does about the Temporal server it was told to use, before it uses one.

Three answers and no fourth: something already listens (leave it alone), nothing listens on an
address that names this machine (start one, own it, stop it), nothing listens on somebody else's
address (say so and serve nothing). The probe and the starter are arguments, so every branch is
provable with no socket and no binary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from harness.adapters.temporal_server import PROBE_SECONDS, UI_PORT, ServerOutcome, ensure_server, split_address
from harness.settings import RunSettings

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class ScriptedProbe:
    """An `AddressProbe` that answers what it was built to answer, and records every question."""

    answers: bool
    asked: list[tuple[str, float]] = field(default_factory=list)

    async def __call__(self, address: str, *, probe_seconds: float) -> bool:
        self.asked.append((address, probe_seconds))
        return self.answers


@dataclass
class FakeServer:
    """A started server that records being stopped."""

    stopped: bool = False

    async def shutdown(self) -> None:
        self.stopped = True


@dataclass
class RecordingStarter:
    """A `Starter` that records its arguments and hands back one `FakeServer`."""

    server: FakeServer = field(default_factory=FakeServer)
    started: list[tuple[int, Path]] = field(default_factory=list)

    async def __call__(self, *, port: int, database_file: Path) -> FakeServer:
        self.started.append((port, database_file))
        return self.server


@dataclass
class RaisingStarter:
    """A `Starter` that never comes up: no network, no cached binary, or a port lost to a race."""

    async def __call__(self, *, port: int, database_file: Path) -> FakeServer:
        del port, database_file
        msg = "no network, no cached binary, and nobody to fetch one"
        raise RuntimeError(msg)


def _settings(address: str) -> RunSettings:
    """Settings built by hand, so the test's address is the one under test whatever is exported."""
    return RunSettings(run_engine="temporal", temporal_address=address)


async def test_an_answering_address_is_left_alone(tmp_path: Path) -> None:
    """A server somebody else runs is somebody else's to stop."""
    probe, starter = ScriptedProbe(answers=True), RecordingStarter()

    async with ensure_server(_settings("localhost:7233"), tmp_path, probe=probe, starter=starter) as outcome:
        assert outcome == ServerOutcome(address="localhost:7233", started=False)

    assert starter.started == [], "nothing may be started on an address that already answers"
    assert probe.asked == [("localhost:7233", PROBE_SECONDS)], "the probe is bounded, so a dead address cannot hang a boot"


async def test_a_silent_local_address_gets_a_server_this_process_owns(tmp_path: Path) -> None:
    """No Docker, no server, no environment: the app brings one up on the port it was told."""
    probe, starter = ScriptedProbe(answers=False), RecordingStarter()

    async with ensure_server(_settings("127.0.0.1:7999"), tmp_path, probe=probe, starter=starter) as outcome:
        assert outcome == ServerOutcome(address="127.0.0.1:7999", started=True)
        assert starter.started == [(7999, tmp_path / "temporal.db")], "history goes in the state dir, so a restart finds it"
        assert not starter.server.stopped, "the server stands for as long as the context does"

    assert starter.server.stopped, "a server this process started is a server this process stops"


async def test_a_starter_that_raises_leaves_the_app_reachable_false_rather_than_unbootable(tmp_path: Path) -> None:
    """No network, no cached binary, a port lost to a race: none of it should take the app down."""
    probe, starter = ScriptedProbe(answers=False), RaisingStarter()

    async with ensure_server(_settings("127.0.0.1:7999"), tmp_path, probe=probe, starter=starter) as outcome:
        assert outcome == ServerOutcome(address="127.0.0.1:7999", started=False, reachable=False)


async def test_a_silent_remote_address_is_refused_rather_than_started(tmp_path: Path) -> None:
    """Starting a server on this laptop would not make the remote one answer, only hide that it does not."""
    probe, starter = ScriptedProbe(answers=False), RecordingStarter()

    async with ensure_server(_settings("temporal.internal:7233"), tmp_path, probe=probe, starter=starter) as outcome:
        assert outcome == ServerOutcome(address="temporal.internal:7233", started=False, reachable=False)

    assert starter.started == []


async def test_a_bracketed_ipv6_loopback_counts_as_this_machine(tmp_path: Path) -> None:
    """`[::1]:7233` is the same laptop as `127.0.0.1:7233`, so it gets the same answer."""
    probe, starter = ScriptedProbe(answers=False), RecordingStarter()

    async with ensure_server(_settings("[::1]:7233"), tmp_path, probe=probe, starter=starter) as outcome:
        assert outcome.started is True

    assert starter.started == [(7233, tmp_path / "temporal.db")]


def test_an_address_splits_into_a_host_and_a_port() -> None:
    assert split_address("localhost:7233") == ("localhost", 7233)
    assert split_address("[::1]:7233") == ("::1", 7233)


def test_an_address_with_no_port_is_refused_in_words() -> None:
    """A bound-less address would otherwise fail deep inside the SDK, naming nothing a person set."""
    with pytest.raises(ValueError, match="host:port"):
        split_address("localhost")


def test_the_ui_port_is_the_one_the_compose_file_publishes() -> None:
    """Whichever way the server came up, the history is read at the same URL."""
    assert UI_PORT == 8233
