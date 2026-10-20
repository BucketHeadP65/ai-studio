"""The store against a real Postgres: leads once, findings beside them."""

import asyncio

from dataclasses import dataclass

import pytest

from core.ai.history.contracts import PoolDatabase, row_as
from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.domain.taxonomy import TAXONOMY, Blame, Finding, Surface, SurfaceKind
from core.ai.inspection.infrastructure.postgres import InspectionStore
from core.ai.inspection.read_models import StoredBlame
from core.ai.transcripts.contracts import Span
from tests.support.fakes.bound_database import BoundDatabase

SPANS = (Span(step=0, message=1, part=0), Span(step=0, message=3, part=0))


@dataclass
class BackendProcess:
    """The database process serving a connection."""

    pid: int


@dataclass
class LockState:
    """Whether a database process is blocked on a lock."""

    blocked: bool


def a_lead(source: str = "record:run-a") -> Lead:
    return Lead(source=source, symptom="duplicate_call", spans=SPANS, found_by="duplicate_call")


async def test_the_same_lead_is_stored_once_and_answers_the_same_id(inspection_database):
    store = InspectionStore(inspection_database)

    first = await store.add_leads("author", [a_lead()])
    second = await store.add_leads("author", [a_lead(), a_lead("record:run-b")])

    assert first == [1]
    assert second[0] == 1
    assert second[1] != 1
    stored = await store.leads_of("record:run-a")
    assert [row.symptom for row in stored] == ["duplicate_call"]
    assert stored[0].spans == list(SPANS)
    assert stored[0].watch == "author"


async def test_a_finding_is_stored_beside_its_lead_and_read_back_by_source(inspection_database):
    store = InspectionStore(inspection_database)
    (lead_id,) = await store.add_leads("author", [a_lead()])
    symptom = TAXONOMY.symptom("duplicate_call")
    cause = TAXONOMY.root_cause("instructions_say_nothing")
    finding = Finding(
        symptom=symptom,
        evidence=SPANS,
        blames=(Blame(what=cause, where=Surface(kind=SurfaceKind.SKILL, name="digesting"), why="nothing says to reuse a result"),),
        found_by="inspector",
    )

    finding_id = await store.add_finding(finding, source="record:run-a", lead_id=lead_id, found_by="inspector", model="test-model")

    (stored,) = await store.findings_of("record:run-a")
    assert stored.finding_id == finding_id
    assert stored.lead_id == lead_id
    assert stored.blames == [
        StoredBlame(
            root_cause="instructions_say_nothing", surface_kind="skill", surface_name="digesting", why="nothing says to reuse a result"
        )
    ]
    assert stored.model == "test-model"
    assert stored.question_id is None
    assert stored.explanation == ""


async def test_a_findings_explanation_round_trips(inspection_database):
    store = InspectionStore(inspection_database)
    (lead_id,) = await store.add_leads("author", [a_lead()])
    symptom = TAXONOMY.symptom("duplicate_call")
    cause = TAXONOMY.root_cause("instructions_say_nothing")
    finding = Finding(
        symptom=symptom,
        evidence=SPANS,
        blames=(Blame(what=cause, where=Surface(kind=SurfaceKind.SKILL, name="digesting"), why="nothing says to reuse a result"),),
        found_by="inspector",
        explanation="The agent already had the answer. It called the tool again anyway. Reuse what it already saw.",
    )

    await store.add_finding(finding, source="record:run-a", lead_id=lead_id, found_by="inspector", model="test-model")

    (stored,) = await store.findings_of("record:run-a")
    assert stored.explanation == "The agent already had the answer. It called the tool again anyway. Reuse what it already saw."


async def test_a_settled_lead_answers_the_finding_it_already_has(inspection_database):
    store = InspectionStore(inspection_database)
    (lead_id,) = await store.add_leads("author", [a_lead()])
    finding = Finding(
        symptom=TAXONOMY.symptom("duplicate_call"),
        evidence=SPANS,
        blames=(
            Blame(
                what=TAXONOMY.root_cause("instructions_say_nothing"),
                where=Surface(kind=SurfaceKind.SKILL, name="digesting"),
                why="nothing says to reuse a result",
            ),
        ),
        found_by="rule",
    )

    first = await store.add_finding(finding, source="record:run-a", lead_id=lead_id, found_by="rule", model=None)
    second = await store.add_finding(finding, source="record:run-a", lead_id=lead_id, found_by="rule", model=None)

    assert first == second
    assert len(await store.findings_of("record:run-a")) == 1


async def test_settled_answers_the_ids_that_have_a_finding(inspection_database):
    store = InspectionStore(inspection_database)
    lead_a, lead_b = await store.add_leads("author", [a_lead(), a_lead("record:run-b")])
    finding = Finding(
        symptom=TAXONOMY.symptom("duplicate_call"),
        evidence=SPANS,
        blames=(
            Blame(
                what=TAXONOMY.root_cause("instructions_say_nothing"),
                where=Surface(kind=SurfaceKind.SKILL, name="digesting"),
                why="nothing says to reuse a result",
            ),
        ),
        found_by="rule",
    )
    await store.add_finding(finding, source="record:run-a", lead_id=lead_a, found_by="rule", model=None)

    assert await store.settled([lead_a, lead_b]) == {lead_a}
    assert await store.settled([]) == set()


async def test_a_lead_is_read_back_by_id_and_a_refusal_lands_on_it(inspection_database: PoolDatabase) -> None:
    store = InspectionStore(inspection_database)
    (lead_id,) = await store.add_leads("watch-a", [a_lead()])

    before = await store.lead(lead_id)
    await store.resolve_lead(lead_id, "undecided", "undecided: cannot tell")
    after = await store.lead(lead_id)

    assert before is not None
    assert before.refusal is None
    assert before.attempted_at is None
    assert after is not None
    assert after.refusal == "undecided: cannot tell"
    assert after.attempted_at is not None
    assert await store.lead(lead_id + 1000) is None


async def test_a_consumer_without_a_row_has_no_position_and_advancing_upserts(inspection_database: PoolDatabase) -> None:
    store = InspectionStore(inspection_database)

    assert await store.position("inspection.dispatch") is None
    await store.advance("inspection.dispatch", 41)
    await store.advance("inspection.dispatch", 42)

    assert await store.position("inspection.dispatch") == 42


async def test_uncertainty_and_invalid_answers_do_not_settle_a_lead(inspection_database: PoolDatabase) -> None:
    store = InspectionStore(inspection_database)
    first, second = await store.add_leads("author", [a_lead(), a_lead("record:run-b")])

    await store.resolve_lead(first, "undecided", "The relevant instructions were not recorded.")
    await store.resolve_lead(second, "invalid", "Both answers named a nonexistent root cause.")

    uncertain, invalid = await store.lead(first), await store.lead(second)
    assert uncertain is not None
    assert uncertain.resolution == "undecided"
    assert invalid is not None
    assert invalid.resolution == "invalid"
    assert uncertain.attempted_at is not None
    assert await store.settled([first, second]) == set()


async def test_an_explicit_dismissal_is_settled_without_inventing_a_finding(inspection_database: PoolDatabase) -> None:
    store = InspectionStore(inspection_database)
    (lead_id,) = await store.add_leads("author", [a_lead()])

    await store.resolve_lead(lead_id, "dismissed", "The owner requested a fresh read after editing the file.")

    stored = await store.lead(lead_id)
    assert stored is not None
    assert stored.resolution == "dismissed"
    assert stored.refusal == "The owner requested a fresh read after editing the file."
    assert await store.settled([lead_id]) == {lead_id}
    assert await store.findings_of(stored.source) == []


async def test_stale_resolutions_preserve_an_explicit_dismissal(inspection_database: PoolDatabase) -> None:
    store = InspectionStore(inspection_database)
    (lead_id,) = await store.add_leads("author", [a_lead()])
    await store.resolve_lead(lead_id, "dismissed", "The owner requested a fresh read.")
    settled = await store.lead(lead_id)

    await store.resolve_lead(lead_id, "undecided", "A stale attempt lacked evidence.")
    await store.resolve_lead(lead_id, "invalid", "A stale attempt named an unknown cause.")

    assert await store.lead(lead_id) == settled
    assert await store.settled([lead_id]) == {lead_id}


@pytest.mark.parametrize("finding_wins", [True, False])
async def test_final_verdicts_serialize_on_the_lead_even_when_the_loser_started_before_commit(
    inspection_database: PoolDatabase, finding_wins: bool
) -> None:
    store = InspectionStore(inspection_database)
    (lead_id,) = await store.add_leads("author", [a_lead()])
    finding = Finding(
        symptom=TAXONOMY.symptom("duplicate_call"), evidence=SPANS,
        blames=(Blame(what=TAXONOMY.root_cause("agent_reasoning"), where=Surface(kind=SurfaceKind.MODEL_TIER, name="worker"), why="Repeated."),),
        found_by="inspector",
    )
    async with inspection_database.acquire() as winner, inspection_database.acquire() as loser:
        winning_store, losing_store = InspectionStore(BoundDatabase(winner)), InspectionStore(BoundDatabase(loser))
        backend = await loser.fetchrow("SELECT pg_backend_pid() AS pid")
        assert backend is not None
        pid = row_as(BackendProcess, backend).pid
        async with winner.transaction():
            await winner.fetchrow("SELECT lead_id FROM leads WHERE lead_id = $1 FOR UPDATE", lead_id)
            pending = asyncio.create_task(
                losing_store.resolve_lead(lead_id, "dismissed", "No issue.") if finding_wins else
                losing_store.add_finding(finding, source="record:run-a", lead_id=lead_id, found_by="inspector", model="late")
            )
            try:
                async with asyncio.timeout(2):
                    while True:
                        waiting = await winner.fetchrow("SELECT EXISTS (SELECT 1 FROM pg_locks WHERE pid = $1 AND NOT granted) AS blocked", pid)
                        if waiting is not None and row_as(LockState, waiting).blocked:
                            break
                        await asyncio.sleep(0.001)
                if finding_wins:
                    await winning_store.add_finding(finding, source="record:run-a", lead_id=lead_id, found_by="inspector", model="winner")
                else:
                    await winning_store.resolve_lead(lead_id, "dismissed", "No issue.")
            except BaseException:
                pending.cancel()
                await asyncio.gather(pending, return_exceptions=True)
                raise
        async with asyncio.timeout(2):
            result = await pending
    stored = await store.lead(lead_id)
    assert stored is not None
    assert result is (False if finding_wins else None)
    assert len(await store.findings_of("record:run-a")) == (1 if finding_wins else 0)
    assert (stored.resolution == "dismissed") is not finding_wins
