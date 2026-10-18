"""What the inspection slice registers: its pool on the sessions database, the jobs engine, and every part built over them."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from core.ai.history.contracts import PoolDatabase, RequestParametersSink, recording_capabilities
from core.ai.inspection.contracts import (
    INSPECTOR_AGENT_NAME,
    TAXONOMY,
    AttributeLeadHandler,
    Dispatch,
    InspectionQueries,
    InspectionStore,
    InspectorDeps,
    InspectRunHandler,
    JobLanes,
    RecordEvents,
    RecordEventsOverDatabase,
    ScanRunHandler,
    Sweep,
    Taxonomy,
    TierAgent,
    Watch,
    inspector_agent,
    scan_agent,
)
from core.ai.transcripts.contracts import RecordRunsOfConversation, RecordTranscriptLoader, RunFacts, RunsOfConversation, TranscriptLoaders
from core.cqrs.contracts import JobQueries, JobsAndQueries, PostgresJobs
from wireup import injectable

from harness.adapters.contracts import ModelTiers, RecordDatabase, SessionsDatabase, TierModel, sessions_writer_dsn
from harness.domain.contracts import ConversationHistory, SessionTranscript
from harness.features.inspection.infrastructure.postgres import InspectionDatabase, JobsEngine, open_inspection_pool, open_jobs_engine
from harness.features.inspection.internal_contracts import FollowUpAnswerer
from harness.features.inspection.service import InspectorAnswerer
from harness.settings import InspectionSettings


@injectable
async def inspection_database(sessions: SessionsDatabase) -> AsyncIterator[InspectionDatabase]:
    """The inspection's pool, its tables in place, closed with the container.

    Args:
        sessions: The sessions database, whose pool the schema lock is taken on.

    Yields:
        The inspection's pool.
    """
    pool = await open_inspection_pool(sessions.pool, dsn=sessions_writer_dsn())
    try:
        yield InspectionDatabase(pool)
    finally:
        await pool.close()


@injectable
async def jobs_engine(sessions: SessionsDatabase) -> AsyncIterator[JobsEngine]:
    """The engine over the sessions database, the coordination tables in place, disposed with the container.

    Args:
        sessions: The sessions database, whose pool the schema lock is taken on.

    Yields:
        The engine the coordination tables are reached through.
    """
    engine = await open_jobs_engine(sessions.pool, dsn=sessions_writer_dsn())
    try:
        yield JobsEngine(engine)
    finally:
        await engine.dispose()


@injectable
def jobs_and_queries(engine: JobsEngine) -> JobsAndQueries:
    """The commands, in the coordination schema of the sessions database.

    Args:
        engine: The engine the coordination tables are reached through.

    Returns:
        The writes and reads over the commands, in Postgres.
    """
    return PostgresJobs(engine.engine)


@injectable
def job_queries(jobs: JobsAndQueries) -> JobQueries:
    """The reads over the commands, on the same instance that writes them.

    Args:
        jobs: The writes and reads over the commands.

    Returns:
        The same instance, under its read port.
    """
    return jobs


@injectable
def job_lanes(jobs: JobsAndQueries) -> JobLanes:
    """The rules lane and the inspector's lane over the commands.

    Args:
        jobs: The writes and reads over the commands.

    Returns:
        Both lanes.
    """
    return JobLanes(jobs)


@injectable
def inspection_store(database: InspectionDatabase) -> InspectionStore:
    """The cursors, leads and findings.

    Args:
        database: The inspection's pool.

    Returns:
        The inspection's tables, read and written on that pool.
    """
    return InspectionStore(PoolDatabase(database.pool))


@injectable
def inspection_queries(store: InspectionStore) -> InspectionQueries:
    """The same tables, under the narrow read a page answers from and never the writes beside it.

    Args:
        store: The inspection's tables.

    Returns:
        The same instance, under its read port.
    """
    return store


@injectable
def record_events(database: InspectionDatabase) -> RecordEvents:
    """The record's events, read on the inspection's connections.

    Args:
        database: The inspection's pool, whose connections reach the record.

    Returns:
        The record's events.
    """
    return RecordEventsOverDatabase(PoolDatabase(database.pool))


@injectable
def runs_of_conversation(database: InspectionDatabase) -> RunsOfConversation:
    """The conversation's earlier runs, read on the inspection's connections, which reach the record.

    Args:
        database: The inspection's pool.

    Returns:
        The earlier agent runs of a conversation.
    """
    return RecordRunsOfConversation(PoolDatabase(database.pool))


@injectable
def run_facts(database: InspectionDatabase) -> RunFacts:
    """How one run's own facts are read: from the conversation record.

    Args:
        database: The inspection's pool, whose connections reach the record.

    Returns:
        One agent run's own facts.
    """
    return RecordRunsOfConversation(PoolDatabase(database.pool))


@injectable
def inspection_watches(settings: InspectionSettings) -> Sequence[Watch]:
    """The standing rules the run overview looks the inspector's commands up under.

    Args:
        settings: The inspection's settings section.

    Returns:
        The section's watches.
    """
    return settings.watches


@injectable
def taxonomy() -> Taxonomy:
    """The taxonomy of symptoms and root causes the findings route renders its sentences from.

    Returns:
        The taxonomy.
    """
    return TAXONOMY


@injectable
def follow_up(
    tiers: ModelTiers,
    conversation: RunsOfConversation,
    transcript: SessionTranscript,
    parameters: RequestParametersSink,
    history: ConversationHistory,
    loaders: TranscriptLoaders,
) -> FollowUpAnswerer:
    """What answers one more question to the inspector, resolved once.

    Args:
        tiers: How the tier a question was asked on becomes a model now.
        conversation: The conversation's earlier runs.
        transcript: Where the follow-up's own run is recorded.
        parameters: Where each of its requests' tool definitions and settings are recorded.
        history: What a conversation last put in front of the model.
        loaders: How a question's own run opens again.

    Returns:
        The follow-up over those six.
    """
    return InspectorAnswerer(
        tiers=tiers, conversation=conversation, transcript=transcript, parameters=parameters, history=history, loaders=loaders
    )


@injectable
def transcript_loaders(record: RecordDatabase) -> TranscriptLoaders:
    """How a `record:<run id>` source opens: through the record's own pool.

    Args:
        record: The record's database.

    Returns:
        The loaders by scheme, the record's own under `record`.
    """
    return TranscriptLoaders(by_key={"record": RecordTranscriptLoader(PoolDatabase(record.pool))})


@injectable
def inspect_run_handler(
    settings: InspectionSettings, loaders: TranscriptLoaders, store: InspectionStore, lanes: JobLanes
) -> InspectRunHandler:
    """The rules lane's handler: the watches by name, the tool facts, the store, and the inspector's lane for the rest.

    Args:
        settings: The inspection's settings section, which names the watches and the tool facts.
        loaders: How a source opens.
        store: The inspection's tables.
        lanes: The lanes the handler hands the rest on to.

    Returns:
        The rules lane's handler.
    """
    return InspectRunHandler(
        watches={watch.name: watch for watch in settings.watches}, facts=settings.facts(), loaders=loaders, store=store, queue=lanes
    )


@injectable
def attribute_lead_handler(
    settings: InspectionSettings,
    loaders: TranscriptLoaders,
    store: InspectionStore,
    tiers: ModelTiers,
    conversation: RunsOfConversation,
    transcript: SessionTranscript,
    parameters: RequestParametersSink,
) -> AttributeLeadHandler:
    """The inspector's lane's handler: one agent per tier the section names, cheapest first, every run recorded.

    Args:
        settings: The inspection's settings section, which names the inspector's tiers.
        loaders: How a source opens.
        store: The inspection's tables, which also hold the questions.
        tiers: How each tier becomes a model.
        conversation: The conversation's earlier runs.
        transcript: Where each of the inspector's runs is recorded.
        parameters: Where each request's tool definitions and settings are recorded.

    Returns:
        The inspector's lane's handler.
    """
    recording = recording_capabilities(transcript, parameters, agent_name=INSPECTOR_AGENT_NAME, deps_type=InspectorDeps)
    return AttributeLeadHandler(
        store=store,
        loaders=loaders,
        agents=tuple(
            TierAgent(tier=tier, agent=inspector_agent(TierModel(tier, tiers), capabilities=recording)) for tier in settings.inspector_tiers
        ),
        conversation=conversation,
        questions=store,
    )


@injectable
def scan_run_handler(
    settings: InspectionSettings,
    loaders: TranscriptLoaders,
    store: InspectionStore,
    tiers: ModelTiers,
    conversation: RunsOfConversation,
    transcript: SessionTranscript,
    parameters: RequestParametersSink,
) -> ScanRunHandler:
    """The scan lane's handler: the same tiers as the inspector's other lane, over the same watches, every run recorded.

    Args:
        settings: The inspection's settings section, which names the inspector's tiers and the watches.
        loaders: How a source opens.
        store: The inspection's tables, which also hold the questions.
        tiers: How each tier becomes a model.
        conversation: The conversation's earlier runs.
        transcript: Where each of the inspector's runs is recorded.
        parameters: Where each request's tool definitions and settings are recorded.

    Returns:
        The scan lane's handler.
    """
    recording = recording_capabilities(transcript, parameters, agent_name=INSPECTOR_AGENT_NAME, deps_type=InspectorDeps)
    return ScanRunHandler(
        store=store,
        loaders=loaders,
        agents=tuple(
            TierAgent(tier=tier, agent=scan_agent(TierModel(tier, tiers), capabilities=recording)) for tier in settings.inspector_tiers
        ),
        conversation=conversation,
        questions=store,
        watches={watch.name: watch for watch in settings.watches},
    )


@injectable
def dispatch(settings: InspectionSettings, events: RecordEvents, store: InspectionStore, lanes: JobLanes) -> Dispatch:
    """The consumer of the record's events.

    Args:
        settings: The inspection's settings section, which names the watches and the batch size.
        events: The record's events.
        store: The inspection's tables, which hold the cursors.
        lanes: The lanes the dispatch issues commands on.

    Returns:
        The dispatch.
    """
    return Dispatch(watches=settings.watches, events=events, cursors=store, lanes=lanes, batch=settings.batch)


@injectable
def sweep(settings: InspectionSettings, events: RecordEvents, lanes: JobLanes) -> Sweep:
    """The sweep watches over their windows.

    Args:
        settings: The inspection's settings section, which names the watches.
        events: The record's events.
        lanes: The lanes the sweep issues commands on.

    Returns:
        The sweep.
    """
    return Sweep(watches=settings.watches, events=events, lanes=lanes)


REGISTRATIONS: list[object] = [
    inspection_database,
    jobs_engine,
    jobs_and_queries,
    job_queries,
    job_lanes,
    inspection_store,
    inspection_queries,
    record_events,
    runs_of_conversation,
    run_facts,
    inspection_watches,
    taxonomy,
    follow_up,
    transcript_loaders,
    inspect_run_handler,
    attribute_lead_handler,
    scan_run_handler,
    dispatch,
    sweep,
]
"""Everything this slice puts in the container, each under the type its consumers declare."""
