"""The inspection slice's wiring: what the container builds from the settings and the pools."""

from types import FunctionType

from core.ai.history.contracts import recording_capabilities
from core.ai.history.request_parameters_recorder import RequestParametersRecorder
from core.ai.inspection.contracts import (
    INSPECTOR_AGENT_NAME,
    Dispatch,
    InspectionStore,
    InspectorDeps,
    InspectRunHandler,
    ScanRunHandler,
    Sweep,
    Watch,
)
from core.ai.transcripts.contracts import TranscriptLoaders
from core.cqrs.contracts import MemoryJobs
from pydantic_ai_harness.step_persistence import InMemoryStepStore, StepPersistence

from harness.adapters.model_tiers import TierModel
from harness.features.inspection.infrastructure.postgres import INSPECTION_NAMESPACE
from harness.features.inspection.wiring import (
    REGISTRATIONS,
    attribute_lead_handler,
    dispatch,
    inspect_run_handler,
    job_lanes,
    scan_run_handler,
    sweep,
)
from harness.settings import InspectionSettings
from tests.support.fakes.inspection import FakeRecordEvents, FakeRunsOfConversation
from tests.support.fakes.pools import FakePool
from tests.support.fakes.request_parameters import FakeRequestParametersSink
from tests.support.tiers import tiers_under

WATCHES = (
    Watch(name="every-run", rules=("duplicate_call",)),
    Watch(name="nightly", rules=("duplicate_call",), when="sweep", sweep_hours=24),
)


def transcript_loaders_of() -> TranscriptLoaders:
    return TranscriptLoaders(by_key={})


def test_the_slice_registers_its_parts() -> None:
    assert INSPECTION_NAMESPACE == "inspection"
    names = {fn.__name__ for fn in REGISTRATIONS if isinstance(fn, FunctionType)}
    assert names >= {
        "inspection_database",
        "jobs_engine",
        "jobs_and_queries",
        "job_lanes",
        "inspection_store",
        "record_events",
        "runs_of_conversation",
        "transcript_loaders",
        "inspect_run_handler",
        "attribute_lead_handler",
        "scan_run_handler",
        "dispatch",
        "sweep",
    }


def test_the_handler_takes_the_watches_by_name_and_the_facts_from_the_section() -> None:
    settings = InspectionSettings(watches=WATCHES, world_changing=("write_note",))
    lanes = job_lanes(MemoryJobs())

    handler = inspect_run_handler(settings, transcript_loaders_of(), InspectionStore(FakePool()), lanes)

    assert isinstance(handler, InspectRunHandler)
    assert set(handler.watches) == {"every-run", "nightly"}
    assert handler.facts.world_changing == frozenset({"write_note"})
    assert handler.queue is lanes


def test_the_inspectors_lane_asks_one_agent_per_tier_cheapest_first(tmp_path) -> None:
    settings = InspectionSettings(inspector_tiers=("helper", "worker"))

    handler = attribute_lead_handler(
        settings,
        transcript_loaders_of(),
        InspectionStore(FakePool()),
        tiers_under(tmp_path),
        FakeRunsOfConversation(),
        InMemoryStepStore(),
        FakeRequestParametersSink(),
    )

    tiers = []
    for rung in handler.agents:
        assert isinstance(rung.agent.model, TierModel)
        tiers.append(rung.tier)
    assert tiers == ["helper", "worker"]
    assert handler.questions is handler.store


def test_the_scan_lane_asks_one_agent_per_tier_cheapest_first_over_the_same_watches(tmp_path) -> None:
    settings = InspectionSettings(watches=WATCHES, inspector_tiers=("helper", "worker"))

    handler = scan_run_handler(
        settings,
        transcript_loaders_of(),
        InspectionStore(FakePool()),
        tiers_under(tmp_path),
        FakeRunsOfConversation(),
        InMemoryStepStore(),
        FakeRequestParametersSink(),
    )

    assert isinstance(handler, ScanRunHandler)
    assert set(handler.watches) == {"every-run", "nightly"}
    tiers = []
    for rung in handler.agents:
        assert isinstance(rung.agent.model, TierModel)
        tiers.append(rung.tier)
    assert tiers == ["helper", "worker"]
    assert handler.questions is handler.store


def test_the_dispatch_and_the_sweep_read_the_same_events_and_fill_the_same_lane() -> None:
    settings = InspectionSettings(watches=WATCHES, batch=50)
    events = FakeRecordEvents()
    store = InspectionStore(FakePool())
    lanes = job_lanes(MemoryJobs())

    built_dispatch = dispatch(settings, events, store, lanes)
    built_sweep = sweep(settings, events, lanes)

    assert isinstance(built_dispatch, Dispatch)
    assert built_dispatch.batch == 50
    assert built_dispatch.cursors is store
    assert isinstance(built_sweep, Sweep)
    assert built_sweep.lanes is lanes
    assert built_sweep.watches == WATCHES


def test_the_inspectors_runs_are_recorded_in_the_record_under_one_agent_name() -> None:
    transcript = InMemoryStepStore()
    parameters = FakeRequestParametersSink()

    persistence, recorder = recording_capabilities(transcript, parameters, agent_name=INSPECTOR_AGENT_NAME, deps_type=InspectorDeps)

    assert INSPECTOR_AGENT_NAME == "inspector"
    assert isinstance(persistence, StepPersistence)
    assert persistence.store is transcript
    assert persistence.agent_name == "inspector"
    assert persistence.parent_run_id is None
    assert isinstance(recorder, RequestParametersRecorder)
    assert recorder.sink is parameters
