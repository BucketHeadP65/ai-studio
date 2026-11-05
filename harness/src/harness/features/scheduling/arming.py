"""What arms the on-step-change schedules when the workflow library saves a new reusable step version."""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import ReusableStepSaved
from core.observability import get_logger

from harness.features.scheduling.domain import ON_STEP_CHANGE_SUBSCRIBER

if TYPE_CHECKING:
    from core.cqrs.contracts import Clock, Event, MessageBus

    from harness.features.scheduling.service import Scheduler

_logger = get_logger(__name__)


class StepChangeArming:
    """Arms every enabled on-step-change schedule with the reusable step version a `ReusableStepSaved` names.

    It is an event subscriber over the library's outbox. Each schedule's quiet period counts from the moment the event is
    delivered, read from its clock, so events delivered after a stop still wait out the quiet period. An event that finds no
    on-step-change schedule is recorded and arms nothing, and no schedule created later sees it. Any other event is ignored.
    """

    def __init__(self, scheduler: Scheduler, *, clock: Clock) -> None:
        """Hold the scheduler and the clock.

        Args:
            scheduler: The scheduler the on-step-change schedules are armed through.
            clock: Where the moment each quiet period counts from is read.
        """
        self._scheduler = scheduler
        self._clock = clock

    def subscribe(self, bus: MessageBus) -> None:
        """Subscribe this arming to `ReusableStepSaved` on `bus`, under the subscriber name its arm records the event with.

        A worker that delivers the event then finds the delivery the arm recorded, so a delivery cut short after the arm
        is not handed to it again.

        Args:
            bus: The bus of the worker over the library's outbox.
        """
        bus.register_subscriber(ReusableStepSaved, self, name=ON_STEP_CHANGE_SUBSCRIBER)

    async def __call__(self, event: Event) -> None:
        """Arm for `event` when it is a `ReusableStepSaved`.

        Args:
            event: The event being delivered.
        """
        if isinstance(event, ReusableStepSaved):
            await self.step_saved(event)

    async def step_saved(self, event: ReusableStepSaved) -> None:
        """Arm every enabled on-step-change schedule with the saved version's id, once per event.

        Args:
            event: The new reusable step version.
        """
        armed = await self._scheduler.arm_after_step_change(event_id=event.event_id, step_ids=(event.step_id,), now=self._clock())
        if armed.repeat:
            return
        _logger.info(
            "reusable step {step_id} saved by {saved_by} armed {count} on-step-change schedule(s)",
            step_id=event.step_id,
            saved_by=event.saved_by,
            count=len(armed.schedule_ids),
        )
