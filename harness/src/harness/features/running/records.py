"""Every record a run writes, and the collaborators a launch reaches beyond its own arguments.

A launch writes three records that always travel together: what the run cost, where it rests
when it ends, and what it proved. Owner reviews travel beside them, in one append-only file for
every run this deployment archives. All four share one home. A run's own event file is not among
them. It lives under the home of the session that launched the run.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol

from core.ai.evaluation.contracts import FileOwnerReviewStore
from core.ai.workflows.contracts import FileEvidenceLog, RunEvidence
from core.observability import get_logger
from pydantic import BaseModel, ConfigDict
from pydantic_ai_harness.spend import SpendLimits, Window

from harness.features.running.infrastructure.archive import FileRunArchive
from harness.features.running.infrastructure.spend_log import FileSpendLog

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.evaluation.contracts import OwnerReviewStore
    from core.ai.workflows.contracts import Approval, EvidenceLog, Grade
    from core.cqrs.contracts import TopicSink

    from harness.domain.contracts import KnowledgeBaseNames
    from harness.features.running.internal_contracts import RunArchive, SpendLog
    from harness.session import PooledSessions
    from harness.settings import RunSettings, StateSettings

_logger = get_logger(__name__)


class RunEndObserver(Protocol):
    """Told about each workflow run's ending once its evidence is written."""

    async def run_ended(self, evidence: RunEvidence) -> None:
        """React to one run's ending. A failure here never undoes the evidence.

        Args:
            evidence: What the run's ending recorded.
        """
        ...


class ObservedEvidenceLog(FileEvidenceLog):
    """The evidence log file, telling an observer about each run's ending after writing it.

    An observer that raises is logged. The evidence line stands, and the run's own ending carries on.
    """

    def __init__(self, path: Path, observer: RunEndObserver) -> None:
        """Keep the log at `path` and tell `observer` about each run's ending.

        Args:
            path: The log file. It and its folder are created on the first append.
            observer: Told after each `RunEvidence` is written.
        """
        super().__init__(path)
        self._observer = observer

    async def append(self, event: RunEvidence | Approval | Grade) -> None:
        """Write `event`, then tell the observer when it is a run's ending.

        Args:
            event: The run evidence, approval or grade to add.
        """
        await super().append(event)
        if not isinstance(event, RunEvidence):
            return
        try:
            await self._observer.run_ended(event)
        except Exception:
            _logger.exception("run_end_observer_failed: the ending of {workflow} was written and reacting to it failed", workflow=event.workflow_id)


@dataclass(frozen=True)
class RunRecords:
    """Where a run's own records land."""

    spend: SpendLog
    """What every run cost, one file per run."""
    archive: RunArchive
    """Where an ended run rests, whole."""
    evidence: EvidenceLog
    """What mechanically happened, append-only and readable by content address."""
    reviews: OwnerReviewStore
    """Every owner review committed against a run, keyed by its own review id."""

    @classmethod
    def under(cls, state: StateSettings, *, retention_days: int, observer: RunEndObserver | None = None) -> RunRecords:
        """All four records as files under `state`'s home.

        Args:
            state: The state settings whose home holds the records.
            retention_days: How long the archive keeps an ended run. `0` turns archiving off.
            observer: Told about each run's ending once its evidence is written. `None` tells nobody.

        Returns:
            The records.
        """
        return cls(
            spend=FileSpendLog(state.spend),
            archive=FileRunArchive(state.archive, retention_days=retention_days),
            evidence=FileEvidenceLog(state.evidence_log) if observer is None else ObservedEvidenceLog(state.evidence_log, observer),
            reviews=FileOwnerReviewStore(state.home / "runs" / "reviews.jsonl"),
        )


class SpendLimit(BaseModel):
    """One ceiling this deployment holds a run to, as a preview states it before anybody starts one."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window: Window
    """What the ceiling is counted over: one run, one conversation, a day, a month, or everything."""
    usd: Decimal
    """The figure past which a run is refused."""
    warn_at: float | None = None
    """The fraction of `usd` at which a warning is given on the way, `None` for a limit that only refuses."""


def spend_limits(limits: SpendLimits[object] | None) -> tuple[SpendLimit, ...]:
    """What `limits` holds a run to, in the windows a person is shown.

    A budget that only counts carries no ceiling, so it is left out rather than shown as one.

    Args:
        limits: The deployment's spend limits, or `None` when it has none.

    Returns:
        One entry per ceiling. Empty is a deployment that configured no ceiling at all, which is
        never a ceiling of zero: a reader that sees none must say none was configured.
    """
    if limits is None:
        return ()
    return tuple(
        SpendLimit(window=budget.window, usd=budget.usd, warn_at=budget.warn_at) for budget in limits.budgets if budget.usd is not None
    )


@dataclass(frozen=True)
class RunCollaborators:
    """What a run reaches beyond the workflow and its arguments, handed to a launcher as one parameter.

    They travel together from the entry that starts a run down to the run's own observer.
    """

    records: RunRecords
    """Where this run's own records land."""
    topics: TopicSink
    """Where the run says it moved."""
    run_settings: RunSettings
    """Which engine runs it, and where to reach that engine."""
    sessions: PooledSessions | None = None
    """Every session this process holds, for a run whose own turns take a session's hold. `None` for
    a process that holds no pool, which decides and settles unguarded."""
    knowledge_bases: KnowledgeBaseNames | None = None
    """Which knowledge bases are registered here, for a launch admitting an argument that selects
    one. `None` for a process that cannot read the registry, which then refuses a run naming a
    knowledge base rather than admitting a name nothing checked."""
    spend_limits: tuple[SpendLimit, ...] = ()
    """Every money ceiling this deployment holds a run to, as a preview states them. Empty is a
    deployment that configured none, and a preview says so rather than naming a figure of zero."""
