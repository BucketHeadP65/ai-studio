"""A schedule: a standing intent to run one workflow on a cadence, and the rules its settings follow.

A cadence is an interval such as "every 30m", a 5-field cron expression, "on refresh", or "after <workflow name>".
The last two wait for an event: the clock never brings them due, an arm does, and the fire leaves the schedule resting.
`next_after` is the one place a cadence becomes a next fire time.
A blackout is one or more daily "HH:MM-HH:MM" windows, and each may wrap past midnight.
"""

from __future__ import annotations

import re

from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from croniter import croniter
from pydantic import BaseModel, Field, JsonValue, model_validator

from harness.domain.contracts import GateMode

if TYPE_CHECKING:
    from collections.abc import Mapping

    from core.ai.workflows.contracts import OutcomeStatus, RunEvidence

CHAINED_OUTCOMES: frozenset[OutcomeStatus] = frozenset({"succeeded", "partial"})
"""The checked outcomes of a run after which the schedules waiting for its workflow fire."""

_INTERVAL = re.compile(r"^every\s+(\d+)\s*(s|m|h|d)$", re.IGNORECASE)
_UNIT_SECONDS: dict[str, int] = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_ON_REFRESH = re.compile(r"^on\s+refresh$", re.IGNORECASE)
_AFTER_RUN = re.compile(r"^(?i:after)\s+([a-z0-9]+(?:-[a-z0-9]+)*)$")
"""`after` in any case, then a workflow name in a workflow's own grammar: lowercase words joined by single hyphens."""

_CADENCE_SHAPE = (
    "'every <N><s|m|h|d>' (e.g. 'every 30m'), a 5-field cron expression (e.g. '0 9 * * 1'), "
    "'on refresh', or 'after <workflow name>' (e.g. 'after nightly-report')"
)

_WINDOW = re.compile(r"^([0-2]\d):([0-5]\d)-([0-2]\d):([0-5]\d)$")
_BLACKOUT_SHAPE = "one or more 'HH:MM-HH:MM' windows, comma-separated (e.g. '22:00-06:00', wrapping midnight allowed)"

UNKNOWN_SCHEDULE = "no schedule with that id here"
"""The refusal every schedule route and schedule tool gives for an id no schedule has."""


class IntervalCadence(BaseModel, frozen=True):
    """A cadence that fires every `seconds` seconds, counted from the moment it advances from."""

    kind: Literal["interval"] = "interval"
    seconds: int


class CronCadence(BaseModel, frozen=True):
    """A cadence that fires on the 5-field cron `expression`."""

    kind: Literal["cron"] = "cron"
    expression: str


class OnRefreshCadence(BaseModel, frozen=True):
    """A cadence that fires once after each refresh run that created sources in the knowledge base the schedule's arguments select."""

    kind: Literal["on_refresh"] = "on_refresh"


class AfterRunCadence(BaseModel, frozen=True):
    """A cadence that fires once after a run of `workflow_name` ends succeeded or partial over a knowledge base the schedule also selects."""

    kind: Literal["after_run"] = "after_run"
    workflow_name: str
    """The `name` of the workflow whose runs arm the schedule, never a library id or a version."""


Cadence = IntervalCadence | CronCadence | OnRefreshCadence | AfterRunCadence
"""A parsed cadence, as `parse_cadence` returns it."""


def gate_wait_seconds(policy: str) -> float | None:
    """How long a gate policy lets a gate wait for a person.

    `"wait"` holds the run at its gate until somebody decides. `"fail_after:<seconds>"` gives the gate
    that many seconds before it times out.

    Args:
        policy: The gate policy text.

    Returns:
        The seconds a gate may wait, or `None` for `"wait"`, which waits forever.

    Raises:
        ValueError: The text is neither `wait` nor `fail_after:<seconds>` with a positive number.
    """
    if policy == "wait":
        return None
    head, sep, tail = policy.partition(":")
    if head == "fail_after" and sep:
        try:
            seconds = float(tail)
        except ValueError:
            seconds = -1.0
        if seconds > 0:
            return seconds
    msg = f"a gate policy is 'wait' or 'fail_after:<seconds>', not {policy!r}"
    raise ValueError(msg)


def parse_cadence(text: str) -> Cadence:
    """Parse `text` as an interval, a 5-field cron expression, "on refresh", or "after <workflow name>".

    Args:
        text: The cadence text.

    Returns:
        The parsed cadence.

    Raises:
        ValueError: `text` is neither shape. The message says what a cadence looks like.
    """
    stripped = text.strip()
    if (match := _INTERVAL.match(stripped)) is not None:
        count, unit = match.groups()
        return IntervalCadence(seconds=int(count) * _UNIT_SECONDS[unit.lower()])
    if _ON_REFRESH.match(stripped) is not None:
        return OnRefreshCadence()
    if (match := _AFTER_RUN.match(stripped)) is not None:
        return AfterRunCadence(workflow_name=match.group(1))
    if len(stripped.split()) == 5 and croniter.is_valid(stripped):  # noqa: PLR2004 (a cron expression has 5 fields)
        return CronCadence(expression=stripped)
    msg = f"{text!r} is not a cadence. A cadence looks like {_CADENCE_SHAPE}"
    raise ValueError(msg)


def next_after(cadence: Cadence, now: datetime) -> datetime | None:
    """The next fire time strictly after `now`, or `None` for a cadence that waits for an event.

    Args:
        cadence: The parsed cadence.
        now: The moment to advance from.

    Returns:
        The next fire time, or `None` for "on refresh" and "after <workflow name>", which the clock never brings due.
    """
    match cadence:
        case IntervalCadence(seconds=seconds):
            return now + timedelta(seconds=seconds)
        case CronCadence(expression=expression):
            return croniter(expression, now).get_next(datetime)
        case OnRefreshCadence() | AfterRunCadence():
            return None


class BlackoutWindow(BaseModel, frozen=True):
    """One daily quiet hours window, in minutes since midnight.

    An end at or before the start means the window wraps past midnight, as 22:00-06:00 does.
    """

    start_minutes: int
    end_minutes: int

    def contains(self, minute_of_day: int) -> bool:
        """Whether a minute of the day falls inside this window.

        Args:
            minute_of_day: Minutes since midnight, from 0 to 1439.

        Returns:
            True when the minute is inside the window.
        """
        if self.start_minutes < self.end_minutes:
            return self.start_minutes <= minute_of_day < self.end_minutes
        return minute_of_day >= self.start_minutes or minute_of_day < self.end_minutes

    def end_time(self) -> time:
        """This window's end as a wall clock time, the time a held fire waits until.

        Returns:
            The end time.
        """
        return time(hour=self.end_minutes // 60, minute=self.end_minutes % 60)


def parse_blackout(text: str) -> tuple[BlackoutWindow, ...]:
    """Parse `text` as one or more comma separated daily "HH:MM-HH:MM" windows.

    A window may wrap past midnight, as "22:00-06:00" does. A window whose start equals its end is refused.

    Args:
        text: The blackout text.

    Returns:
        The windows, in the order written.

    Raises:
        ValueError: `text` is empty, a piece of it is not "HH:MM-HH:MM" with a real hour and minute,
            or a window starts and ends on the same minute.
    """
    stripped = text.strip()
    pieces = [piece.strip() for piece in stripped.split(",")] if stripped else []
    if not pieces:
        msg = f"{text!r} is not a blackout. A blackout looks like {_BLACKOUT_SHAPE}"
        raise ValueError(msg)
    windows: list[BlackoutWindow] = []
    for piece in pieces:
        match = _WINDOW.match(piece)
        if match is None:
            msg = f"{text!r} is not a blackout. A blackout looks like {_BLACKOUT_SHAPE}"
            raise ValueError(msg)
        start_hour, start_minute, end_hour, end_minute = (int(group) for group in match.groups())
        if start_hour > 23 or end_hour > 23:  # noqa: PLR2004 (23 is the last hour of the day)
            msg = f"{text!r} is not a blackout. A blackout looks like {_BLACKOUT_SHAPE}"
            raise ValueError(msg)
        start_minutes, end_minutes = start_hour * 60 + start_minute, end_hour * 60 + end_minute
        if start_minutes == end_minutes:
            msg = f"{text!r} is not a blackout. A window's start and end cannot be the same minute"
            raise ValueError(msg)
        windows.append(BlackoutWindow(start_minutes=start_minutes, end_minutes=end_minutes))
    return tuple(windows)


def blacked_out_until(windows: tuple[BlackoutWindow, ...], moment: datetime) -> time | None:
    """The wall clock time a moment is blacked out until.

    Args:
        windows: The blackout windows.
        moment: The moment to test.

    Returns:
        The end of the first window that holds `moment`, or `None` when no window holds it.
    """
    minute_of_day = moment.hour * 60 + moment.minute
    for window in windows:
        if window.contains(minute_of_day):
            return window.end_time()
    return None


def preview_fires(cadence: Cadence, *, count: int = 3, since: datetime | None = None) -> list[datetime]:
    """The next fires a cadence would produce.

    Args:
        cadence: The parsed cadence.
        count: How many fires to list.
        since: The moment to walk forward from. Now when not given.

    Returns:
        The fire times, earliest first. A cadence that waits for an event has none.
    """
    moment = since if since is not None else datetime.now(UTC)
    occurrences: list[datetime] = []
    for _ in range(count):
        following = next_after(cadence, moment)
        if following is None:
            break
        occurrences.append(following)
        moment = following
    return occurrences


class Schedule(BaseModel):
    """A standing intent to run one workflow on a cadence, with its settings and its tick log.

    A schedule whose cadence waits for an event rests with no next fire until a refresh run or a workflow run arms it, and rests again after the fire.

    A schedule with no `next_run_at` never comes due.
    `capacity_key` shares a limit of `capacity_limit` live runs across every schedule that names it.
    `catch_up` makes a held fire run once as soon as the hold clears, instead of skipping to the next turn.
    A schedule built disabled with no `paused_at` is paused from its `created_at`.
    """

    id: UUID = Field(default_factory=uuid4)
    workflow_id: str
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    cadence: str
    """The cadence text `parse_cadence` reads."""
    enabled: bool = True
    gate_policy: str = "wait"
    """`wait` or `fail_after:<seconds>`, as `gate_wait_seconds` reads it."""
    gate_mode: GateMode = "wait"
    capacity_key: str | None = None
    capacity_limit: int | None = None
    blackout: str | None = None
    """Daily quiet hours in the grammar `parse_blackout` reads."""
    catch_up: bool = False
    session_id: str = ""
    """The runner session of the latest fire. Empty until the first fire."""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_run_at: datetime | None = None
    last_outcome: str = ""
    """What the latest fire did, in free text. Empty until the first fire."""
    next_run_at: datetime | None = None
    paused_at: datetime | None = None

    @model_validator(mode="after")
    def _stamp_paused_at_when_minted_disabled(self) -> Schedule:
        """Stamp `paused_at` with `created_at` when the schedule is built disabled and `paused_at` was not passed."""
        if not self.enabled and "paused_at" not in self.model_fields_set:
            self.paused_at = self.created_at
        return self

    def paused_at_after(self, *, enabled: bool | None, now: datetime) -> datetime | None:
        """The pause stamp this schedule holds once `enabled` is applied to it.

        Turning the schedule off stamps `now`. Turning it on clears the stamp.
        Leaving `enabled` unset, or setting it to the value it already has, keeps the stamp it has.

        Args:
            enabled: The new `enabled` value, or `None` when the change leaves it alone.
            now: The moment a pause is stamped with.

        Returns:
            The pause stamp to save, or `None` when the schedule is not paused.
        """
        if enabled is None or enabled == self.enabled:
            return self.paused_at
        return None if enabled else now


class ScheduleUpdate(BaseModel):
    """A partial change to one schedule's settings. A field left `None` is not changed.

    It covers the settings an owner edits, a next fire for a schedule that had none, and the pause stamp.
    The rest of the tick log is written by `stamp` alone.
    `capacity_key`, `capacity_limit` and `blackout` can be replaced but never cleared back to `None`.
    `paused_at` is the one exception to the rule on `None`: it is saved as given whenever `enabled` is set.
    """

    workflow_id: str | None = None
    arguments: dict[str, JsonValue] | None = None
    cadence: str | None = None
    enabled: bool | None = None
    gate_policy: str | None = None
    gate_mode: GateMode | None = None
    capacity_key: str | None = None
    capacity_limit: int | None = None
    blackout: str | None = None
    catch_up: bool | None = None
    next_run_at: datetime | None = None
    """The next fire, set only when a save brings back a schedule that had none."""
    paused_at: datetime | None = None
    """The pause stamp, written only together with `enabled`."""
    clears_next_fire: bool = False
    """Set when the change makes the schedule rest: the next fire is cleared in the same write and `next_run_at` is ignored."""


ON_REFRESH_SUBSCRIBER = "schedules.on_refresh"
"""The subscriber name a refresh run's event id is recorded under in `coordination.event_deliveries` once the scheduler armed for it."""


class Armed(BaseModel, frozen=True):
    """What one arming did: whether its event was handled before, and which schedules will fire on the next tick."""

    repeat: bool = False
    """True when the event id was recorded before, so this arming changed nothing."""
    schedule_ids: tuple[UUID, ...] = ()
    """Every schedule that will fire on the next tick. One that was already armed keeps its earlier time."""


NEW_SOURCES_TITLE = "New sources"
"""The title of the notification the owner gets about a refresh run that created sources."""


def new_sources_notice(knowledge_base: str, sources_created: int, requested_by_entry: Mapping[str, int]) -> str:
    """The sentence the owner is told about a refresh run that created sources.

    Each watch-list entry that found new sources is named with how many it found, the largest first. An entry's count
    is what it put on the source queue, so it can differ from the sources created.

    Args:
        knowledge_base: The registry name of the refreshed knowledge base.
        sources_created: How many sources the refresh run created.
        requested_by_entry: The new sources each watch-list entry found, by entry label.

    Returns:
        The sentence, such as `notes: 6 new sources (6 found by x:someone)`.
    """
    noun = "source" if sources_created == 1 else "sources"
    origins = sorted(((count, label) for label, count in requested_by_entry.items() if count > 0), key=lambda pair: (-pair[0], pair[1]))
    said = f"{knowledge_base}: {sources_created} new {noun}"
    if origins:
        said += " (" + ", ".join(f"{count} found by {label}" for count, label in origins) + ")"
    return said


def chains_after(evidence: RunEvidence) -> bool:
    """Whether a run's ending fires the schedules waiting for its workflow: its checked outcome is succeeded or partial.

    Args:
        evidence: What the run's ending recorded.

    Returns:
        True when the run's checked outcome is one of `CHAINED_OUTCOMES`.
    """
    return evidence.result is not None and evidence.result.status in CHAINED_OUTCOMES
