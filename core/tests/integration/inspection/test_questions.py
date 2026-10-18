"""The questions table against a real Postgres: one row per question, its answer, and the finding that points at it."""

from uuid import uuid4

from core.ai.inspection.domain.leads import Lead
from core.ai.inspection.domain.taxonomy import TAXONOMY, Blame, Finding, Surface, SurfaceKind
from core.ai.inspection.infrastructure.postgres import InspectionStore
from core.ai.transcripts.contracts import Span

SPANS = (Span(step=0, message=1, part=0),)


def a_lead(source: str = "record:run-a") -> Lead:
    return Lead(source=source, symptom="duplicate_call", spans=SPANS, found_by="duplicate_call")


def a_finding() -> Finding:
    return Finding(
        symptom=TAXONOMY.symptom("duplicate_call"),
        evidence=SPANS,
        blames=(
            Blame(
                what=TAXONOMY.root_cause("instructions_say_nothing"),
                where=Surface(kind=SurfaceKind.SKILL, name="digesting"),
                why="nothing says to reuse a result",
            ),
        ),
        found_by="inspector",
    )


async def test_a_question_is_stored_answered_and_read_back_by_id_and_by_source(inspection_database):
    store = InspectionStore(inspection_database)
    (lead_id,) = await store.add_leads("author", [a_lead()])
    conversation_id = uuid4()

    question_id = await store.add_question(
        kind="attribute",
        conversation_id=conversation_id,
        source="record:run-a",
        watch="author",
        lead_id=lead_id,
        tier="helper",
        model="test-model",
    )
    await store.answer_question(question_id, f"lead {lead_id}: finding 1 by test-model")

    stored = await store.question(question_id)
    assert stored is not None
    assert (stored.kind, stored.source, stored.watch, stored.lead_id) == ("attribute", "record:run-a", "author", lead_id)
    assert (stored.conversation_id, stored.tier, stored.model) == (conversation_id, "helper", "test-model")
    assert stored.answer == f"lead {lead_id}: finding 1 by test-model"
    assert [row.question_id for row in await store.questions_of("record:run-a")] == [question_id]
    assert await store.question(question_id + 500) is None


async def test_a_finding_carries_the_question_that_decided_it(inspection_database):
    store = InspectionStore(inspection_database)
    (lead_id,) = await store.add_leads("author", [a_lead()])
    question_id = await store.add_question(
        kind="attribute", conversation_id=uuid4(), source="record:run-a", watch="author", lead_id=lead_id, tier="worker", model="test-model"
    )

    finding_id = await store.add_finding(
        a_finding(), source="record:run-a", lead_id=lead_id, found_by="inspector", model="test-model", question_id=question_id
    )

    assert finding_id is not None
    read = await store.finding(finding_id)
    assert read is not None
    assert read.question_id == question_id
    assert read.symptom == "duplicate_call"


async def test_a_rule_s_finding_has_no_question_and_the_newest_read_filters_and_orders(inspection_database):
    store = InspectionStore(inspection_database)
    (first,) = await store.add_leads("author", [a_lead("record:run-a")])
    (second,) = await store.add_leads("author", [a_lead("record:run-b")])
    oldest = await store.add_finding(a_finding(), source="record:run-a", lead_id=first, found_by="rule", model=None)
    newest = await store.add_finding(a_finding(), source="record:run-b", lead_id=second, found_by="inspector", model="m")

    every = await store.findings_newest(symptom=None, source=None, found_by=None, limit=10)
    only_inspector = await store.findings_newest(symptom=None, source=None, found_by="inspector", limit=10)
    only_run_a = await store.findings_newest(symptom=None, source="record:run-a", found_by=None, limit=10)
    no_such_symptom = await store.findings_newest(symptom="unbatched_calls", source=None, found_by=None, limit=10)

    assert [row.finding_id for row in every] == [newest, oldest]
    assert every[0].question_id is None
    assert [row.found_by for row in only_inspector] == ["inspector"]
    assert [row.source for row in only_run_a] == ["record:run-a"]
    assert no_such_symptom == []
