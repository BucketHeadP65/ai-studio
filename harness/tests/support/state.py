"""One test's own state home, and everything a session or a run writes under it."""

from __future__ import annotations

from itertools import count
from pathlib import Path
from tempfile import mkdtemp
from typing import TYPE_CHECKING

from pydantic_ai_harness.step_persistence import FileStepStore

from harness.adapters.session_log import SessionLog
from harness.features.running.records import RunRecords
from harness.settings import RunSettings, StateSettings

if TYPE_CHECKING:
    from harness.domain.events import Recorded, SessionEvent
    from harness.domain.ports import SessionEvents


_OUTSIDE = Path(mkdtemp(prefix="harness-state-"))
"""One directory per test process, outside this checkout, for the homes no test names itself."""

_UNNAMED = count()

KEPT_DAYS = RunSettings().archive_days
"""How long the `run` section keeps an ended run by default: what a test that is not about retention hands an archive."""


def a_state(home: Path) -> StateSettings:
    """A state home under `home`, so nothing this test writes lands in the real one."""
    return StateSettings(home=home)


def a_throwaway_state() -> StateSettings:
    """A state home of its own, outside this checkout: for a session whose files no test reads back.

    Named, never created -- a writer brings its own home into being -- so a session handed one and
    left alone leaves nothing behind anywhere.
    """
    return StateSettings(home=_OUTSIDE / f"state-{next(_UNNAMED)}")


def a_session_log(home: Path) -> SessionLog:
    """One session log under `home`, announcing nowhere."""
    return a_log_under(a_state(home))


def a_log_under(state: StateSettings) -> SessionLog:
    """One session log under `state`'s own home, announcing nowhere.

    For a caller that already holds the state a session was composed with, so the log and every
    other file that session writes hang off the one home.
    """
    return SessionLog(state.session_log, topics=None)


def a_transcript(home: Path) -> FileStepStore:
    """One transcript under `home`."""
    return FileStepStore(a_state(home).home / "steps")


def recorded(log: SessionEvents, event: SessionEvent) -> Recorded:
    """`event` on `log`'s timeline, asserted onto it: a test that could not record has nothing to assert."""
    entry = log.record(event)
    assert entry is not None, "the event was not recorded"
    return entry


def records_under(home: Path) -> RunRecords:
    """Every record a run writes, under a state home at `home`, an ended run kept for the `run` section's own default."""
    return RunRecords.under(a_state(home), retention_days=KEPT_DAYS)
