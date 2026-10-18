"""What a run is doing, as a watcher can read it.

Nothing here imports the session, so `harness.session` can import this module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from time import monotonic
from typing import TYPE_CHECKING, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable

    from core.ai.workflows.contracts import RunObserver


class StepEvent(TypedDict):
    """One transition, as the browser receives it.

    It serialises to JSON with no encoder.
    """

    seq: int
    node: str
    at: float
    """Seconds since this run's first transition, on the server's monotonic clock."""
    finished: bool
    failed: bool


@dataclass
class LiveProgress:
    """Which steps are running right now, and which have finished.

    A `RunObserver` the browser can read. Two nodes appear in `running` at once when the graph
    forks, which is the one place its parallelism becomes visible to a person.
    """

    running: set[str] = field(default_factory=set)
    done: list[str] = field(default_factory=list)
    failed: set[str] = field(default_factory=set)
    events: list[StepEvent] = field(default_factory=list)
    """Every transition, append-only, each with a sequence number.

    A reader with a cursor gets everything since it last looked, including a step shorter than its
    poll interval.
    """
    started_at: float | None = None
    """Monotonic base for this run, so event times are seconds since it began."""

    def reset(self) -> None:
        """Back to a blank run: a new launch must not inherit the last run's transitions."""
        self.running.clear()
        self.done.clear()
        self.failed.clear()
        self.events.clear()
        self.started_at = None

    def started(self, node_id: str) -> None:
        """A step began: show it running, and log the transition.

        Args:
            node_id: The node that began.
        """
        self.running.add(node_id)
        self._record(node_id, finished=False)

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        """A step ended, well or badly: move it to done, and log the transition.

        Args:
            node_id: The node that ended.
            failed: Whether it ended badly.
        """
        self.running.discard(node_id)
        if failed:
            self.failed.add(node_id)
        # A rejected gate reruns its upstream, so a node can finish twice. Keep the last say.
        if node_id in self.done:
            self.done.remove(node_id)
        self.done.append(node_id)
        self._record(node_id, finished=True, failed=failed)

    def _record(self, node_id: str, *, finished: bool, failed: bool = False) -> None:
        now = monotonic()
        if self.started_at is None:
            self.started_at = now
        self.events.append(
            StepEvent(seq=len(self.events) + 1, node=node_id, at=round(now - self.started_at, 3), finished=finished, failed=failed)
        )

    def replayed(self, node_id: str) -> None:
        """A run log settled this node's value without running it, so show it done.

        It logs one `finished` transition and no `started`.

        Args:
            node_id: The node the run log settled.
        """
        if node_id in self.done:
            self.done.remove(node_id)
        self.done.append(node_id)
        self._record(node_id, finished=True)

    def since(self, cursor: int) -> list[StepEvent]:
        """The transitions after `cursor`.

        Args:
            cursor: The last `seq` the reader saw. 0 or less reads the whole history.

        Returns:
            The transitions, oldest first.
        """
        # Without the floor at 0, a negative cursor slices from the end and repeats the newest event forever.
        return self.events[max(0, cursor) :]


class Watchers:
    """One `RunObserver` made of several.

    The engine takes exactly one observer. This one tells each of its observers every transition,
    in order.
    """

    def __init__(self, *observers: RunObserver) -> None:
        """Hold the observers every transition is told to.

        Args:
            *observers: The observers, told in this order.
        """
        self._observers = observers

    def started(self, node_id: str) -> None:
        """Tell every watcher the step began.

        Args:
            node_id: The node that began.
        """
        for observer in self._observers:
            observer.started(node_id)

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        """Tell every watcher the step ended, and how.

        Args:
            node_id: The node that ended.
            failed: Whether it ended badly.
        """
        for observer in self._observers:
            observer.finished(node_id, failed=failed)


@dataclass(frozen=True)
class RunTopics:
    """A `RunObserver` that announces one workflow's run every time it moves.

    It publishes only the topic `run.<workflow_id>`, and a reader re-reads what it shows.
    """

    workflow_id: str
    publish: Callable[[str], None]

    def started(self, node_id: str) -> None:
        """Announce that this run moved.

        Args:
            node_id: The node that began. The announcement does not carry it.
        """
        del node_id
        self.publish(f"run.{self.workflow_id}")

    def finished(self, node_id: str, *, failed: bool = False) -> None:
        """Announce that this run moved, well or badly.

        Args:
            node_id: The node that ended. The announcement does not carry it.
            failed: Whether it ended badly. The announcement does not carry it.
        """
        del node_id, failed
        self.publish(f"run.{self.workflow_id}")


@dataclass(frozen=True)
class GateVerdict:
    """A person's whole answer at a gate: whether to release the value, and anything they said.

    `feedback` is read only on a rejection. An approval releases the value regardless of what was
    typed alongside it, so a stray remark on an approval can never turn into a rerun nobody asked
    for.
    """

    approve: bool
    feedback: str = ""


@dataclass(frozen=True)
class PendingReview:
    """A run that has stopped at a gate and is waiting for a person."""

    gate_id: str
    workflow: str
    """The workflow the run was launched from, as the card names it."""
    reviewing: str
    """What the approver is being asked to read, rendered."""
    rejection_reruns: str
    """Which step a rejection goes back to, so the choice is informed."""
