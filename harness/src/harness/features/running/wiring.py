"""What the running slice registers: every record a run writes, each record on its own, what a run reaches, and the spend reporter."""

from __future__ import annotations

from core.ai.workflows.contracts import EvidenceLog
from core.cqrs.contracts import TopicSink
from wireup import injectable

from harness.adapters.contracts import StudioApi, Telemetry, knowledge_base_names
from harness.composition import deployment_brakes
from harness.domain.contracts import KnowledgeBaseNames
from harness.features.running.internal_contracts import RunArchive, RunArchiveQueries, SpendLog, SpendLogQueries
from harness.features.running.records import RunCollaborators, RunEndObserver, RunRecords
from harness.features.running.spend_report import SpendReporter
from harness.session import PooledSessions
from harness.settings import RunSettings, StateSettings


@injectable
def run_records(state: StateSettings, run: RunSettings, observer: RunEndObserver) -> RunRecords:
    """Every record a run writes, all under this deployment's state home, kept as long as `run` says.

    Args:
        state: Where this deployment keeps its state.
        run: The run settings, whose `archive_days` says how long archived runs are kept.
        observer: Told about each run's ending once its evidence is written.

    Returns:
        The run archive, the spend log, the evidence log and the reviews together.
    """
    return RunRecords.under(state, retention_days=run.archive_days, observer=observer)


@injectable
def run_archive(records: RunRecords) -> RunArchive:
    """The archive of ended runs, one file per run under its workflow's own folder.

    Args:
        records: Every record a run writes.

    Returns:
        The run archive.
    """
    return records.archive


@injectable
def run_archive_queries(records: RunRecords) -> RunArchiveQueries:
    """The reads over the run archive, the same instance `run_archive` returns.

    Args:
        records: Every record a run writes.

    Returns:
        The run archive, typed as its read port.
    """
    return records.archive


@injectable
def spend_log(records: RunRecords) -> SpendLog:
    """What every run cost, one file per run.

    Args:
        records: Every record a run writes.

    Returns:
        The spend log.
    """
    return records.spend


@injectable
def spend_log_queries(records: RunRecords) -> SpendLogQueries:
    """The reads over the spend log, the same instance `spend_log` returns.

    Args:
        records: Every record a run writes.

    Returns:
        The spend log, typed as its read port.
    """
    return records.spend


@injectable
def evidence_log(records: RunRecords) -> EvidenceLog:
    """What every run proved, append-only and readable by content address.

    Args:
        records: Every record a run writes.

    Returns:
        The evidence log.
    """
    return records.evidence


@injectable
def knowledge_base_selection(client: StudioApi) -> KnowledgeBaseNames:
    """Which knowledge bases a launch may admit a selection of, read off this deployment's studio.

    Args:
        client: The studio API client.

    Returns:
        The reader of the registered knowledge base names.
    """
    return knowledge_base_names(client)


@injectable
def run_collaborators(
    records: RunRecords, topics: TopicSink, run: RunSettings, sessions: PooledSessions, knowledge_bases: KnowledgeBaseNames
) -> RunCollaborators:
    """What every run reaches beyond the workflow and its arguments, as the one thing a launcher takes.

    It always carries the session pool and the registry reader, so every launcher takes the hold a
    run's settlement turn needs and checks a selected knowledge base. Its spend limits are the ones
    this deployment mounts, so a preview states the figures that actually refuse.

    Args:
        records: Every record a run writes.
        topics: Where a run announces that it moved.
        run: Which engine runs workflows, and where to reach it.
        sessions: Every session this process holds.
        knowledge_bases: The registered knowledge base names a launch checks a selection against.

    Returns:
        What every launcher is handed.
    """
    return RunCollaborators(
        records=records,
        topics=topics,
        run_settings=run,
        sessions=sessions,
        knowledge_bases=knowledge_bases,
        spend_limits=deployment_brakes(),
    )


@injectable
def spend_reporter(telemetry: Telemetry) -> SpendReporter:
    """This deployment's spend reporter over the telemetry backend.

    Args:
        telemetry: The telemetry query client.

    Returns:
        The spend reporter.
    """
    return SpendReporter(telemetry)


REGISTRATIONS: list[object] = [
    run_records,
    run_archive,
    run_archive_queries,
    spend_log,
    spend_log_queries,
    evidence_log,
    knowledge_base_selection,
    run_collaborators,
    spend_reporter,
]
"""Everything this slice puts in the container, each under the type its consumers declare."""
