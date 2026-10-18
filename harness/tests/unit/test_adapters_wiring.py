"""Whether an adapters' factory creates the file home it opens, and opens it with the marker."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.cqrs.contracts import Broadcaster

from harness.adapters.wiring import session_log
from harness.domain.events import ConversationForgotten
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


def test_the_session_log_factory_opens_the_log_with_the_forgotten_marker(tmp_path: Path) -> None:
    """The boot sweep migrates a marker an older process left, which needs the path at construction."""
    state = StateSettings(home=tmp_path / "state")
    log = session_log(state, _a_channel())
    state.forgotten_marker.write_text("2026-09-01T10:00:00+00:00", encoding="utf-8")

    log.boot_sweep()

    assert [type(record.event) for record in log.events()] == [ConversationForgotten]
    assert not state.forgotten_marker.exists(), "the marker is deleted once the log carries the boundary"
