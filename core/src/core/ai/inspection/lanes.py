"""The three lanes as job kinds on the coordination backbone.

`inspect_run` is the rules lane: free, immediate, one command in flight per watch and run. `attribute_lead` and
`scan_run` are the inspector's lanes: a model call each, one command per lead and one per run to read whole. Each kind
has its own policy, so a slow or refused model never holds the rules lane.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel, ConfigDict, Field

from core.ai.inspection.inspect_run import InspectRun
from core.cqrs.contracts import Enqueued, JobPolicy

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.cqrs.contracts import Job, JobsAndQueries

INSPECT_RUN_KIND: Final = "inspect_run"
ATTRIBUTE_LEAD_KIND: Final = "attribute_lead"
SCAN_RUN_KIND: Final = "scan_run"

INSPECTOR_AGENT_NAME: Final = "inspector"
"""The agent name every run of the inspector is recorded under, whichever lane asked it. No watch selects its runs."""

INSPECT_RUN_POLICY: Final = JobPolicy(max_attempts=3, lease_seconds=600, backoff_base_seconds=30.0, backoff_cap_seconds=600.0)
"""Three tries, a ten-minute lease, a short backoff."""
ATTRIBUTE_LEAD_POLICY: Final = JobPolicy(max_attempts=2, lease_seconds=600, backoff_base_seconds=60.0, backoff_cap_seconds=600.0)
"""A model call: two tries, then the lead stays a lead."""
SCAN_RUN_POLICY: Final = JobPolicy(max_attempts=2, lease_seconds=600, backoff_base_seconds=60.0, backoff_cap_seconds=600.0)
"""A model reading a whole run: two tries, then the run stays unscanned."""


class AttributeLead(BaseModel):
    """Ask the inspector about one stored lead."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lead_id: int = Field(description="The lead's id in the inspection's store.")


class ScanRun(BaseModel):
    """Ask the inspector to read one whole run and name every symptom it sees."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(description="The run, as `<scheme>:<locator>`.")
    watch: str = Field(description="The watch its leads and findings are stored under.")
    terminal_event: int | None = Field(default=None, ge=1, description="The committed terminal event this scan must cover, if any.")


def inspect_run_key(watch: str, source: str) -> str:
    """The live key of an `InspectRun`: one in flight per watch and run.

    Args:
        watch: The watch name.
        source: The transcript source of the run.

    Returns:
        The key.
    """
    return f"{watch}:{source}"


def scan_run_key(watch: str, source: str) -> str:
    """The live key of a `ScanRun`: one in flight per watch and run, and never confused with an `InspectRun`.

    Args:
        watch: The watch name.
        source: The transcript source of the run.

    Returns:
        The key.
    """
    return f"scan:{watch}:{source}"


@dataclass(frozen=True)
class JobLanes:
    """The three lanes over one jobs table: what issues commands into them and what asks whether one was issued."""

    jobs: JobsAndQueries

    async def inspect_run(self, source: str, watch: str, *, terminal_event: int | None = None) -> Enqueued:
        """Issue `InspectRun(source, watch)`, or answer the one already in flight.

        Args:
            source: The transcript source of the run.
            watch: The watch it runs under.
            terminal_event: The event that ended the run, when one did.

        Returns:
            The job id, and whether this call created it.
        """
        key = inspect_run_key(watch, source)
        if terminal_event is not None and (done := await self._covered_inspection(watch, source, terminal_event)) is not None:
            return Enqueued(job_id=done.id, created=False)
        return await self.jobs.enqueue(
            INSPECT_RUN_KIND, key, InspectRun(source=source, watch=watch, terminal_event=terminal_event), policy=INSPECT_RUN_POLICY
        )

    async def scan_run(self, source: str, watch: str, *, terminal_event: int | None = None) -> Enqueued:
        """Issue `ScanRun(source, watch)`, or answer the one already in flight or already done for `terminal_event`.

        Args:
            source: The transcript source of the run.
            watch: The watch it runs under.
            terminal_event: The event that ended the run, when one did.

        Returns:
            The job id, and whether this call created it.

        Raises:
            RuntimeError: An earlier scan still owns the live key. The caller must retry the terminal command.
        """
        key = scan_run_key(watch, source)
        if terminal_event is not None and (done := await self._completed(SCAN_RUN_KIND, key, terminal_event)) is not None:
            return Enqueued(job_id=done.id, created=False)
        answer = await self.jobs.enqueue(
            SCAN_RUN_KIND, key, ScanRun(source=source, watch=watch, terminal_event=terminal_event), policy=SCAN_RUN_POLICY
        )
        if terminal_event is not None and not answer.created:
            existing = await self.jobs.read(answer.job_id)
            if existing is None or existing.payload.get("terminal_event") != terminal_event:
                msg = f"{source}: an earlier transcript scan is still in flight. Retry the terminal inspection"
                raise RuntimeError(msg)
        return answer

    async def enqueue(self, lead_ids: Sequence[int]) -> None:
        """Issue one `AttributeLead` per lead, keyed by its id. A lead is never asked about twice at once.

        Args:
            lead_ids: The lead ids.
        """
        for lead_id in lead_ids:
            await self.jobs.enqueue(ATTRIBUTE_LEAD_KIND, str(lead_id), AttributeLead(lead_id=lead_id), policy=ATTRIBUTE_LEAD_POLICY)

    async def inspected(self, watch: str, source: str, *, terminal_event: int | None = None) -> bool:
        """Whether the rules command for `terminal_event`, and any scan it issued, completed. A pending or abandoned scan does not count.

        Args:
            watch: The watch.
            source: The transcript source of the run.
            terminal_event: The terminal event to look for, or None for any.

        Returns:
            True when the inspection completed.
        """
        return await self._covered_inspection(watch, source, terminal_event) is not None

    async def _covered_inspection(self, watch: str, source: str, terminal_event: int | None) -> Job | None:
        done = await self._completed(INSPECT_RUN_KIND, inspect_run_key(watch, source), terminal_event)
        if done is None or terminal_event is None:
            return done
        scans = await self.jobs.recent_jobs(SCAN_RUN_KIND, scan_run_key(watch, source), limit=1)
        if scans and scans[0].payload.get("terminal_event") == terminal_event and scans[0].state != "done":
            return None
        return done

    async def _completed(self, kind: str, key: str, terminal_event: int | None) -> Job | None:
        latest = await self.jobs.recent_jobs(kind, key, limit=1)
        if not latest or latest[0].state != "done":
            return None
        job = latest[0]
        return job if terminal_event is None or job.payload.get("terminal_event") == terminal_event else None
