"""The job runner: claims jobs, delivers outbox events, runs periodics, and waits for a wake-up.

One loop serves every kind registered with `serve`, delivers to every subscription the bus holds,
and runs each periodic on its cadence. A raising handler is retried or abandoned, a raising
subscriber is logged and retried next tick without blocking its siblings, a raising periodic is
logged. Nothing raised by user code escapes the loop.
"""

from __future__ import annotations

import asyncio

from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from core.cqrs.domain import JobContext, JobPolicy, utcnow
from core.cqrs.internal_contracts import TransactionalSubscriber
from core.observability import get_logger

if TYPE_CHECKING:
    from datetime import datetime

    from core.cqrs.bus import MessageBus, Subscription
    from core.cqrs.domain import Clock, Event, Job, StoredEvent
    from core.cqrs.internal_contracts import JobHandler, JobsAndQueries, Outbox, Periodic, Wake

_logger = get_logger(__name__)


class PollingWake:
    """A bounded wait for a job runner with no notification listener."""

    async def wait(self, seconds: float) -> None:
        """Sleep for `seconds`, then let the job runner look for work again.

        Args:
            seconds: How long to sleep.
        """
        await asyncio.sleep(seconds)


class JobObserver(Protocol):
    """Told after every job transition the job runner causes, with the job as it now stands."""

    def transitioned(self, job: Job) -> None:
        """React to `job` having changed state. It must not raise or block.

        Args:
            job: The job as it now stands.
        """
        ...


class WorkerPass(BaseModel):
    """What one tick of the job runner did."""

    model_config = ConfigDict(frozen=True)

    jobs: int
    deliveries: int
    periodics: int


type _Prepared = Callable[[], Awaitable[str]]
type _Prepare = Callable[[dict[str, JsonValue], JobContext], _Prepared]


@dataclass(frozen=True)
class _Served:
    """One registered kind: `prepare` validates a raw payload now and answers the call to make later."""

    prepare: _Prepare
    policy: JobPolicy


@dataclass
class _Scheduled:
    """One periodic: how often it runs and when it last did."""

    name: str
    seconds: float
    work: Periodic
    last_run: datetime | None = None

    def due_in(self, now: datetime) -> float:
        """Seconds until this periodic is next due, `0.0` when it has never run or is due now."""
        if self.last_run is None:
            return 0.0
        return max(0.0, self.seconds - (now - self.last_run).total_seconds())


class Worker:
    """The job runner: one loop over the jobs it serves, the outbox, and the periodics it was given."""

    def __init__(
        self,
        *,
        jobs: JobsAndQueries,
        outbox: Outbox,
        bus: MessageBus,
        wake: Wake,
        name: str,
        clock: Clock = utcnow,
        observer: JobObserver | None = None,
        poll_seconds: float = 30.0,
    ) -> None:
        """Build the job runner over its ports. It serves no kind and runs no periodic until told to.

        Args:
            jobs: The jobs table.
            outbox: The outbox and its deliveries.
            bus: Holds the subscriptions delivery goes to and the wire names to decode events with.
            wake: Returns when work may exist, or when the timeout passes.
            name: The job runner's lease holder name, unique per process (`hostname:pid` is a good one).
            clock: Where the time comes from.
            observer: Told after each job transition.
            poll_seconds: How long to wait for a wake-up before looking anyway.
        """
        self._jobs = jobs
        self._outbox = outbox
        self._bus = bus
        self._wake = wake
        self._name = name
        self._clock = clock
        self._observer = observer
        self._poll_seconds = poll_seconds
        self._served: dict[str, _Served] = {}
        self._periodics: list[_Scheduled] = []

    def serve[P: BaseModel](self, kind: str, payload_type: type[P], handler: JobHandler[P], *, policy: JobPolicy | None = None) -> None:
        """Run jobs of `kind` with `handler`, validating each payload as `payload_type` first.

        Args:
            kind: The job kind to serve.
            payload_type: The model each payload is validated as.
            handler: What runs one job of the kind.
            policy: The retries and lease of the kind. The default policy when omitted.
        """

        def prepare(raw: dict[str, JsonValue], context: JobContext) -> _Prepared:
            payload = payload_type.model_validate(raw)
            return lambda: handler(payload, context)

        self._served[kind] = _Served(prepare=prepare, policy=policy or JobPolicy())

    def every(self, seconds: float, work: Periodic, *, name: str) -> None:
        """Run `work` once now and then every `seconds`, on the job runner's ticks.

        Args:
            seconds: The cadence.
            work: The work to run.
            name: The name failures are logged under.
        """
        self._periodics.append(_Scheduled(name=name, seconds=seconds, work=work))

    async def tick(self) -> WorkerPass:
        """Claim and run every claimable job, deliver the outbox, run the due periodics.

        Returns:
            How many jobs, deliveries and periodics this tick ran.
        """
        ran = 0
        if self._served:
            while (job := await self._jobs.claim(list(self._served), worker=self._name)) is not None:
                await self._run(job)
                ran += 1
        delivered = await self.deliver_once()
        periodics = await self._run_due_periodics()
        return WorkerPass(jobs=ran, deliveries=delivered, periodics=periodics)

    async def run_forever(self, *, stop: asyncio.Event) -> None:
        """Loop until `stop` is set: a tick, then wait for a wake-up or the next due periodic.

        Args:
            stop: Set to end the loop after the current tick or wait.
        """
        while not stop.is_set():
            await self.tick()
            wait_for = min([self._poll_seconds, *(p.due_in(self._clock()) for p in self._periodics)])
            waiting = asyncio.create_task(self._wake.wait(wait_for))
            stopping = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait({waiting, stopping}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (waiting, stopping):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(waiting, stopping, return_exceptions=True)
            for task in done:
                if task is waiting:
                    try:
                        task.result()
                    except OSError as exc:
                        _logger.warning("wake wait failed, skipping this wait: {reason}", reason=str(exc))
                        await asyncio.sleep(self._poll_seconds)
                else:
                    task.result()

    async def deliver_once(self) -> int:
        """Deliver undelivered outbox events to their subscriptions.

        Returns:
            How many events were closed.
        """
        closed = 0
        wire_names = self._bus.event_types()
        for stored in await self._outbox.undelivered():
            event_type = wire_names.get(stored.event_type)
            if event_type is None:
                _logger.warning("No subscriber knows event type {event_type}, closing {event_id} undelivered", event_type=stored.event_type, event_id=str(stored.event_id))
                await self._outbox.close(stored.event_id)
                closed += 1
                continue
            subscriptions = self._bus.subscriptions_for(event_type)
            if not subscriptions:
                _logger.debug("Event type {event_type} has no subscribers, closing {event_id} undelivered", event_type=stored.event_type, event_id=str(stored.event_id))
                await self._outbox.close(stored.event_id)
                closed += 1
                continue
            try:
                event = event_type.model_validate(stored.payload)
            except ValidationError:
                _logger.exception("Event {event_id} ({event_type}) failed to decode, leaving undelivered", event_id=str(stored.event_id), event_type=stored.event_type)
                continue
            if await self._deliver(stored, event, subscriptions):
                await self._outbox.close(stored.event_id)
                closed += 1
        return closed

    async def _deliver(self, stored: StoredEvent, event: Event, subscriptions: tuple[Subscription, ...]) -> bool:
        """Deliver one event to each subscription not yet recorded. True when all are recorded."""
        complete = True
        for subscription in subscriptions:
            if await self._outbox.delivered(stored.event_id, subscription.name):
                continue
            try:
                if isinstance(subscription.handler, TransactionalSubscriber):
                    async with subscription.handler.uow_factory() as uow:
                        await subscription.handler(event, uow)
                        uow.record_delivery(stored.event_id, subscription.name)
                        await uow.commit()
                else:
                    await subscription.handler(event)
                    await self._outbox.record_delivery(stored.event_id, subscription.name)
            except Exception:
                _logger.exception(
                    "Subscriber {subscriber} failed on {event_type} {event_id}, retrying next pass",
                    subscriber=subscription.name,
                    event_type=stored.event_type,
                    event_id=str(stored.event_id),
                )
                complete = False
        return complete

    async def _run(self, job: Job) -> None:
        served = self._served[job.kind]
        self._notify(job)
        if job.attempts > job.max_attempts:
            await self._jobs.fail(job.id, error="lease expired after the last attempt", retry_at=None, claimed_by=self._name)
            await self._notify_current(job.id)
            return
        context = JobContext(job_id=job.id, kind=job.kind, key=job.key, attempt=job.attempts)
        try:
            run = served.prepare(job.payload, context)
        except ValidationError as exc:
            await self._jobs.fail(job.id, error=f"payload: {exc}", retry_at=None, claimed_by=self._name)
            await self._notify_current(job.id)
            return
        beat = asyncio.create_task(self._heartbeat(job.id, served.policy.lease_seconds / 3))
        try:
            result = await run()
        except Exception as exc:
            _logger.exception("Job {job_id} ({kind} {key}) failed on attempt {attempt}", job_id=str(job.id), kind=job.kind, key=str(job.key), attempt=job.attempts)
            retry_at = served.policy.retry_at(job.attempts, self._clock(), max_attempts=job.max_attempts)
            await self._jobs.fail(job.id, error=f"{type(exc).__name__}: {exc}", retry_at=retry_at, claimed_by=self._name)
        else:
            await self._jobs.complete(job.id, result=result, claimed_by=self._name)
        finally:
            beat.cancel()
            with suppress(asyncio.CancelledError):
                await beat
        await self._notify_current(job.id)

    async def _heartbeat(self, job_id: int, every_seconds: float) -> None:
        while True:
            await asyncio.sleep(every_seconds)
            await self._jobs.heartbeat(job_id)

    async def _run_due_periodics(self) -> int:
        ran = 0
        for scheduled in self._periodics:
            now = self._clock()
            if scheduled.due_in(now) > 0:
                continue
            scheduled.last_run = now
            ran += 1
            try:
                await scheduled.work()
            except Exception:
                _logger.exception("Periodic {periodic} failed", periodic=scheduled.name)
        return ran

    def _notify(self, job: Job) -> None:
        if self._observer is None:
            return
        try:
            self._observer.transitioned(job)
        except Exception:
            _logger.exception("Observer failed on job {job_id}, the job's outcome stands", job_id=str(job.id))

    async def _notify_current(self, job_id: int) -> None:
        if self._observer is None:
            return
        job = await self._jobs.read(job_id)
        if job is not None:
            self._notify(job)
