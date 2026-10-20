"""Whether an adapters' factory creates the file home it opens, and opens it with the marker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.cqrs.contracts import Broadcaster

from harness.adapters.wiring import session_log
from harness.settings import StateSettings
from tests.support.fakes.topics import FakeListener, FakeNotifier

if TYPE_CHECKING:
    from pathlib import Path


def _a_channel() -> Broadcaster:
    """A change channel that reaches no database, for a factory that only hands it on."""
    return Broadcaster("postgresql://u:p@h/db", notifier=FakeNotifier(), listener=FakeListener())


def test_the_session_log_factory_creates_its_home(tmp_path: Path) -> None:
    home = tmp_path / "state"
    state = StateSettings(home=home)

    session_log(state, _a_channel())

    assert home.is_dir()


def test_the_session_log_factory_reads_no_forgotten_at_file(tmp_path: Path) -> None:
    """A Forget is a session log entry naming its session. A `forgotten-at` file in the state folder is not read."""
    state = StateSettings(home=tmp_path / "state")
    log = session_log(state, _a_channel())
    marker = state.home / "forgotten-at"
    marker.write_text("2026-09-01T10:00:00+00:00", encoding="utf-8")

    log.boot_sweep()

    assert log.events() == ()
    assert marker.exists()
