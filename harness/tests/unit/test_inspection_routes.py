"""The inspection read back over HTTP: the findings, one finding with its evidence, a question, and one run."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING
from uuid import UUID

from core.ai.inspection.contracts import TAXONOMY, Finding, InspectionQueries, JobLanes, Lead, StoredFinding
from core.ai.inspection.domain.taxonomy import Blame, Surface, SurfaceKind
from core.ai.transcripts.contracts import RunFacts, RunSummary, Span, Step, Transcript, TranscriptLoaders
from core.cqrs.contracts import JobQueries, MemoryJobs
from pydantic_ai.messages import ModelRequest, ModelResponse, RetryInfo, TextPart, ToolCallPart, ToolReturnPart, UserPromptPart

from harness.domain.ports import ConversationMessages
from harness.features.inspection.controller import LEADS_WITH_JOBS
from harness.features.inspection.domain import Answered
from harness.features.inspection.internal_contracts import FollowUpAnswerer
from tests.support.fakes.history import FakeConversationMessages
from tests.support.fakes.inspection import FakeFollowUp, FakeInspectionStore, FakeRunFacts
from tests.support.serving import a_served_client

if TYPE_CHECKING:
    from pathlib import Path

AT = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
A_CONVERSATION = UUID("44444444-4444-4444-8444-444444444444")
SOURCE = "record:run-1"
SPANS = (Span(step=0, message=1, part=0),)


def a_transcript() -> Transcript:
    """Two steps: the prompt, a call, its return, the answer, then one more response. `0.1.0` is the call the evidence points at."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="index the notes", timestamp=AT)]),
        ModelResponse(parts=[ToolCallPart(tool_name="write_note", args={"path": "a.md"}, tool_call_id="c1")], timestamp=AT),
        ModelRequest(parts=[ToolReturnPart(tool_name="write_note", content="the page already exists", tool_call_id="c1", timestamp=AT)]),
        ModelResponse(parts=[TextPart(content="done")], timestamp=AT),
    ]
    second_step = [*messages, ModelResponse(parts=[TextPart(content="all indexed")], timestamp=AT)]
    return Transcript(
        source=SOURCE,
        steps=(
            Step(messages=tuple(messages), ordinals=(0, 1, 2, 3), model=None),
            Step(messages=tuple(second_step), ordinals=(0, 1, 2, 3, 4), model="claude-test"),
        ),
        durations_ms={},
    )


class OpensTheRun:
    """A loader that answers this test's one transcript, and nothing else."""

    async def __call__(self, locator: str) -> Transcript:
        if locator != "run-1":
            msg = f"no run {locator!r}"
            raise LookupError(msg)
        return a_transcript()


def a_finding(spans: tuple[Span, ...] = SPANS, *, explanation: str = "") -> Finding:
    return Finding(
        symptom=TAXONOMY.symptom("warning_ignored"),
        evidence=spans,
        blames=(
            Blame(
                what=TAXONOMY.root_cause("tool_result_shape"),
                where=Surface(kind=SurfaceKind.TOOL, name="write_note"),
                why="the return was plain prose with nothing marking it as a warning to act on",
            ),
        ),
        found_by="inspector",
        explanation=explanation,
    )


async def a_store(*, explanation: str = "") -> FakeInspectionStore:
    """One lead, one question about it, and the finding that question decided."""
    store = FakeInspectionStore()
    (lead_id,) = await store.add_leads("every-run", [Lead(source=SOURCE, symptom="warning_ignored", spans=SPANS, found_by="inspector")])
    question_id = await store.add_question(
        kind="attribute",
        conversation_id=A_CONVERSATION,
        source=SOURCE,
        watch="every-run",
        lead_id=lead_id,
        tier="helper",
        model="claude-test",
    )
    await store.add_finding(
        a_finding(explanation=explanation),
        source=SOURCE,
        lead_id=lead_id,
        found_by="inspector",
        model="claude-test",
        question_id=question_id,
    )
    await store.answer_question(question_id, f"lead {lead_id}: finding 1 by claude-test")
    return store


def answering_with(
    store: FakeInspectionStore,
    *,
    messages: FakeConversationMessages | None = None,
    jobs: JobQueries | None = None,
    facts: FakeRunFacts | None = None,
):
    """What the container answers for this test: the inspection's reads, its loaders, the record, the commands, its facts."""
    return {
        InspectionQueries: store,
        TranscriptLoaders: TranscriptLoaders(by_key={"record": OpensTheRun()}),
        ConversationMessages: messages if messages is not None else FakeConversationMessages(),
        JobQueries: jobs if jobs is not None else MemoryJobs(),
        RunFacts: facts if facts is not None else FakeRunFacts(),
    }


async def test_the_findings_list_answers_newest_first_with_the_leads_own_detector(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/findings")

    assert response.status_code == HTTPStatus.OK
    (row,) = response.json()["findings"]
    assert (row["symptom"], row["found_by"], row["model"], row["source"]) == ("warning_ignored", "inspector", "claude-test", SOURCE)
    assert row["lead_found_by"] == "inspector"
    assert row["question_id"] == 1
    assert row["symptom_text"] == TAXONOMY.symptom("warning_ignored").what_you_see
    assert row["blames"] == [
        {
            "root_cause": "tool_result_shape",
            "root_cause_text": TAXONOMY.root_cause("tool_result_shape").meaning,
            "surface_kind": "tool",
            "surface_name": "write_note",
            "why": "the return was plain prose with nothing marking it as a warning to act on",
        }
    ]


async def test_the_findings_list_filters_by_symptom_source_and_detector(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        by_symptom = await client.get("/api/inspection/findings", params={"symptom": "duplicate_call"})
        by_source = await client.get("/api/inspection/findings", params={"source": "record:elsewhere"})
        by_detector = await client.get("/api/inspection/findings", params={"found_by": "rule"})

    assert by_symptom.json()["findings"] == []
    assert by_source.json()["findings"] == []
    assert by_detector.json()["findings"] == []


async def test_one_finding_carries_its_lead_and_every_span_rendered_as_text(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/findings/1")

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert body["finding"]["symptom"] == "warning_ignored"
    assert body["finding"]["symptom_text"] == TAXONOMY.symptom("warning_ignored").what_you_see
    assert body["finding"]["blames"][0]["root_cause_text"] == TAXONOMY.root_cause("tool_result_shape").meaning
    assert body["lead"]["watch"] == "every-run"
    assert body["lead"]["spans"] == ["0.1.0"]
    (evidence,) = body["evidence"]
    assert evidence["span"] == "0.1.0"
    assert evidence["label"] == "step 0, the model's call to write_note"
    assert evidence["kind"] == "tool-call"
    assert evidence["text"] == "path: a.md"
    assert "{'" not in evidence["text"]


async def test_the_evidence_of_a_retried_return_says_it_is_a_retry(tmp_path: Path) -> None:
    span = Span(step=0, message=2, part=0)
    messages = [
        ModelRequest(parts=[UserPromptPart(content="look it up", timestamp=AT)]),
        ModelResponse(parts=[ToolCallPart(tool_name="lookup", args={"name": "x"}, tool_call_id="c1")], timestamp=AT),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="lookup",
                    content="no city named x",
                    tool_call_id="c1",
                    timestamp=AT,
                    outcome="retried",
                    retry=RetryInfo(cause="model_retry"),
                )
            ]
        ),
    ]
    retried_run = Transcript(source=SOURCE, steps=(Step(messages=tuple(messages), ordinals=tuple(range(len(messages)))),), durations_ms={})

    class OpensTheRetriedRun:
        async def __call__(self, locator: str) -> Transcript:
            del locator
            return retried_run

    store = FakeInspectionStore()
    (lead_id,) = await store.add_leads("every-run", [Lead(source=SOURCE, symptom="tool_refused_call", spans=(span,), found_by="tool_refused_call")])
    await store.add_finding(a_finding(spans=(span,)), source=SOURCE, lead_id=lead_id, found_by="inspector", model="claude-test")
    answers = {**answering_with(store), TranscriptLoaders: TranscriptLoaders(by_key={"record": OpensTheRetriedRun()})}

    async with a_served_client(state=tmp_path, answering=answers) as client:
        response = await client.get("/api/inspection/findings/1")

    (evidence,) = response.json()["evidence"]
    assert (evidence["label"], evidence["kind"], evidence["text"]) == ("step 0, the retry asked of lookup", "retried-return", "no city named x")


async def test_the_evidence_of_a_tool_return_holding_json_renders_it_as_a_tree(tmp_path: Path) -> None:
    span = Span(step=0, message=2, part=0)
    content = {
        "output": '{"summary": "research-digest v13", "fit": "STALE: re-check\\nFITS THIS RUN", '
        '"fits_here": true, "spec_yaml": "name: research-digest\\nversion: 13"}'
    }
    messages = [
        ModelRequest(parts=[UserPromptPart(content="fetch the spec", timestamp=AT)]),
        ModelResponse(parts=[ToolCallPart(tool_name="fetch_workflow_spec", args={}, tool_call_id="c1")], timestamp=AT),
        ModelRequest(parts=[ToolReturnPart(tool_name="fetch_workflow_spec", content=content, tool_call_id="c1", timestamp=AT)]),
    ]
    a_json_return_transcript = Transcript(
        source=SOURCE, steps=(Step(messages=tuple(messages), ordinals=tuple(range(len(messages)))),), durations_ms={}
    )

    class OpensTheJsonReturnRun:
        async def __call__(self, locator: str) -> Transcript:
            if locator != "run-1":
                msg = f"no run {locator!r}"
                raise LookupError(msg)
            return a_json_return_transcript

    store = FakeInspectionStore()
    (lead_id,) = await store.add_leads("every-run", [Lead(source=SOURCE, symptom="warning_ignored", spans=(span,), found_by="inspector")])
    await store.add_finding(a_finding(spans=(span,)), source=SOURCE, lead_id=lead_id, found_by="inspector", model="claude-test")
    answers = {**answering_with(store), TranscriptLoaders: TranscriptLoaders(by_key={"record": OpensTheJsonReturnRun()})}

    async with a_served_client(state=tmp_path, answering=answers) as client:
        response = await client.get("/api/inspection/findings/1")

    (evidence,) = response.json()["evidence"]
    assert evidence["label"] == "step 0, the return of fetch_workflow_spec"
    assert evidence["kind"] == "tool-return"
    lines = evidence["text"].splitlines()
    for expected in (
        "output:",
        "  summary: research-digest v13",
        "  fit:",
        "    STALE: re-check",
        "    FITS THIS RUN",
        "  fits_here: true",
        "  spec_yaml:",
        "    name: research-digest",
        "    version: 13",
    ):
        assert expected in lines


async def test_a_span_the_run_no_longer_has_says_so_rather_than_failing(tmp_path: Path) -> None:
    store = await a_store()
    store.findings = [
        StoredFinding(
            finding_id=1,
            lead_id=1,
            source=SOURCE,
            symptom="warning_ignored",
            spans=[Span(step=0, message=99, part=0)],
            blames=[],
            found_by="inspector",
            model="claude-test",
            decided_at=AT,
            question_id=1,
        )
    ]

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/findings/1")

    assert response.json()["evidence"] == [
        {"span": "0.99.0", "label": "step 0", "kind": "unreadable", "text": "this span is not in the run"}
    ]


async def test_the_findings_explanation_is_the_stored_one_when_present(tmp_path: Path) -> None:
    store = await a_store(explanation="The agent saw a plain-prose warning and filed the note anyway. Check the return before filing.")

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/findings/1")

    assert (
        response.json()["finding"]["explanation"]
        == "The agent saw a plain-prose warning and filed the note anyway. Check the return before filing."
    )


async def test_the_findings_explanation_falls_back_to_the_blames_whys_when_there_is_none(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/findings/1")

    assert response.json()["finding"]["explanation"] == "the return was plain prose with nothing marking it as a warning to act on"


async def test_the_turn_route_answers_one_turn_per_cited_step_with_its_cited_indices_and_messages(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/findings/1/turn")

    assert response.status_code == HTTPStatus.OK
    (turn,) = response.json()["turns"]
    assert turn["step"] == 0
    assert turn["first"] == 0
    assert turn["cited"] == [1]
    assert len(turn["messages"]) == 5
    assert {"run_id", "idx", "kind", "payload", "text"} == set(turn["messages"][0].keys())
    assert turn["messages"][1]["payload"]["parts"][0]["tool_name"] == "write_note"


async def test_the_turn_route_for_an_unknown_finding_is_404(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/findings/99/turn")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["message"] == "no finding with that id here"


async def test_the_turn_route_is_404_with_run_gone_when_the_run_cannot_be_read(tmp_path: Path) -> None:
    store = await a_store()
    store.findings = [replace(store.findings[0], source="record:gone")]

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/findings/1/turn")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert "run gone" in response.json()["message"]


async def test_a_question_answers_its_row_and_its_conversation_as_messages(tmp_path: Path) -> None:
    store = await a_store()
    recorded = FakeConversationMessages(
        {
            str(A_CONVERSATION): [
                ModelRequest(parts=[UserPromptPart(content="Symptom: warning_ignored.", timestamp=AT)]),
                ModelResponse(parts=[ToolCallPart(tool_name="read", args={"step": 0}, tool_call_id="t1")], timestamp=AT),
                ModelRequest(parts=[ToolReturnPart(tool_name="read", content="0.1.0 [tool-call]", tool_call_id="t1", timestamp=AT)]),
                ModelResponse(parts=[TextPart(content="the tool description")], timestamp=AT),
            ]
        }
    )

    async with a_served_client(state=tmp_path, answering=answering_with(store, messages=recorded)) as client:
        response = await client.get("/api/inspection/questions/1")

    body = response.json()
    assert body["question"]["kind"] == "attribute"
    assert body["question"]["tier"] == "helper"
    assert body["question"]["conversation_id"] == str(A_CONVERSATION)
    assert body["question"]["answer"] == "lead 1: finding 1 by claude-test"
    kinds = [message["kind"] for message in body["messages"]]
    assert kinds == ["request", "response", "request", "response"]
    assert body["messages"][0]["idx"] == 0
    assert body["messages"][3]["text"] == "the tool description"
    assert body["messages"][1]["payload"]["parts"][0]["tool_name"] == "read"


async def test_one_run_answers_its_leads_findings_questions_and_the_jobs_it_issued(tmp_path: Path) -> None:
    store = await a_store()
    jobs = MemoryJobs()
    lanes = JobLanes(jobs)
    await lanes.inspect_run(SOURCE, "every-run")
    await lanes.scan_run(SOURCE, "every-run")
    await lanes.enqueue([1])

    async with a_served_client(state=tmp_path, answering=answering_with(store, jobs=jobs)) as client:
        response = await client.get("/api/inspection/runs/run-1")

    body = response.json()
    assert body["source"] == SOURCE
    assert [row["symptom"] for row in body["leads"]] == ["warning_ignored"]
    assert [row["finding_id"] for row in body["findings"]] == [1]
    assert [row["question_id"] for row in body["questions"]] == [1]
    assert sorted(row["kind"] for row in body["jobs"]) == ["attribute_lead", "inspect_run", "scan_run"]


async def test_the_run_overview_keeps_the_lanes_of_the_newest_leads(tmp_path: Path) -> None:
    store = FakeInspectionStore()
    leads = [
        Lead(source=SOURCE, symptom="warning_ignored", spans=(Span(step=step, message=1, part=0),), found_by="inspector")
        for step in range(LEADS_WITH_JOBS + 1)
    ]
    ids = await store.add_leads("every-run", leads)
    jobs = MemoryJobs()
    await JobLanes(jobs).enqueue(ids)

    async with a_served_client(state=tmp_path, answering=answering_with(store, jobs=jobs)) as client:
        response = await client.get("/api/inspection/runs/run-1")

    keys = [row["key"] for row in response.json()["jobs"] if row["kind"] == "attribute_lead"]
    assert len(keys) == LEADS_WITH_JOBS
    assert str(ids[0]) not in keys
    assert str(ids[-1]) in keys


async def test_the_run_outline_answers_the_run_in_one_screen_with_its_facts(tmp_path: Path) -> None:
    store = await a_store()
    facts = FakeRunFacts(
        by_source={
            SOURCE: RunSummary(
                source=SOURCE,
                agent="harness-author",
                started_at=AT,
                first_prompt="index the notes",
                last_event_type="run_completed",
                ended_at=AT + timedelta(minutes=4, seconds=12),
            )
        }
    )

    async with a_served_client(state=tmp_path, answering=answering_with(store, facts=facts)) as client:
        response = await client.get("/api/inspection/runs/run-1/outline")

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert (body["source"], body["agent"], body["ended"], body["model"], body["tool_calls"]) == (SOURCE, "harness-author", "run_completed", "claude-test", 1)
    assert datetime.fromisoformat(body["started_at"]) == AT
    assert datetime.fromisoformat(body["ended_at"]) == AT + timedelta(minutes=4, seconds=12)
    assert body["steps"] == [
        {"step": 0, "messages": 4, "summary": "asked 'index the notes', called write_note (answered), said 'done'"},
        {"step": 1, "messages": 1, "summary": "said 'all indexed'"},
    ]


async def test_the_run_outline_has_no_facts_when_the_record_holds_none(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/runs/run-1/outline")

    body = response.json()
    assert (body["agent"], body["started_at"], body["ended_at"], body["ended"]) == (None, None, None, None)
    assert len(body["steps"]) == 2


async def test_the_run_outline_is_404_with_run_gone_when_the_run_cannot_be_read(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/runs/gone/outline")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["message"] == "record:gone: run gone"


async def test_one_step_answers_the_messages_the_run_added_there_in_the_turns_shape(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/runs/run-1/steps/1")

    assert response.status_code == HTTPStatus.OK
    body = response.json()
    assert (body["step"], body["first"], body["cited"]) == (1, 4, [])
    (message,) = body["messages"]
    assert {"run_id", "idx", "kind", "payload", "text"} == set(message.keys())
    assert message["text"] == "all indexed"


async def test_a_step_the_run_does_not_have_is_404_in_words(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        response = await client.get("/api/inspection/runs/run-1/steps/7")

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["message"] == "no step 7 in record:run-1: it has 2 steps"


async def test_a_finding_a_question_and_a_run_that_are_not_there_answer_404(tmp_path: Path) -> None:
    store = await a_store()

    async with a_served_client(state=tmp_path, answering=answering_with(store)) as client:
        missing_finding = await client.get("/api/inspection/findings/99")
        missing_question = await client.get("/api/inspection/questions/99")
        empty_run = await client.get("/api/inspection/runs/nobody-wrote-this")

    assert missing_finding.status_code == HTTPStatus.NOT_FOUND
    assert missing_finding.json()["message"] == "no finding with that id here"
    assert missing_question.status_code == HTTPStatus.NOT_FOUND
    assert empty_run.status_code == HTTPStatus.OK
    assert empty_run.json()["leads"] == []


async def test_asking_the_inspector_answers_the_follow_up_and_names_who_answered(tmp_path: Path) -> None:
    store = await a_store()
    follow_up = FakeFollowUp(
        answered=Answered(
            answer="Because the description promised a check the tool never made.", conversation_id=A_CONVERSATION, model="chatty"
        )
    )
    answers = {**answering_with(store), FollowUpAnswerer: follow_up}

    async with a_served_client(state=tmp_path, answering=answers) as client:
        response = await client.post(
            "/api/inspection/questions/1/ask", json={"text": "why did you blame the tool description and not the instructions?"}
        )

    assert response.status_code == HTTPStatus.OK
    assert response.json() == {
        "answer": "Because the description promised a check the tool never made.",
        "conversation_id": str(A_CONVERSATION),
        "model": "chatty",
    }
    (asked, text) = follow_up.asked[0]
    assert asked.question_id == 1
    assert text == "why did you blame the tool description and not the instructions?"


async def test_asking_a_question_that_is_not_there_answers_404(tmp_path: Path) -> None:
    store = await a_store()
    follow_up = FakeFollowUp()
    answers = {**answering_with(store), FollowUpAnswerer: follow_up}

    async with a_served_client(state=tmp_path, answering=answers) as client:
        response = await client.post("/api/inspection/questions/99/ask", json={"text": "why?"})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["message"] == "no question with that id here"
    assert follow_up.asked == []


async def test_asking_about_a_run_the_record_no_longer_holds_says_so(tmp_path: Path) -> None:
    store = await a_store()
    store.questions = [replace(store.questions[0], source="record:gone")]
    follow_up = FakeFollowUp(raising=LookupError("record:gone"))
    answers = {**answering_with(store), FollowUpAnswerer: follow_up}

    async with a_served_client(state=tmp_path, answering=answers) as client:
        response = await client.post("/api/inspection/questions/1/ask", json={"text": "why?"})

    assert response.status_code == HTTPStatus.NOT_FOUND
    assert response.json()["message"] == "record:gone: run gone"


async def test_an_empty_question_is_refused_before_the_follow_up_is_asked(tmp_path: Path) -> None:
    store = await a_store()
    follow_up = FakeFollowUp()
    answers = {**answering_with(store), FollowUpAnswerer: follow_up}

    async with a_served_client(state=tmp_path, answering=answers) as client:
        response = await client.post("/api/inspection/questions/1/ask", json={"text": ""})

    assert response.status_code == HTTPStatus.UNPROCESSABLE_ENTITY
    assert follow_up.asked == []
