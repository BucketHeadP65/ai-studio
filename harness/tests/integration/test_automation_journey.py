"""The whole automation journey, end to end, over the real routes and the real workflow engine.

One person asks for an automation in ordinary words, reads what was proposed, starts it themselves,
watches a reviewer refuse a draft and accept its repair, reads what the run left behind, reviews it,
keeps a note out of it, runs it again, and makes the next version out of the run that disappointed.
Eleven rows, one per thing that can go wrong along that walk.

Nothing here reaches a provider or a database. The steps are deterministic functions, the models are
never called, and the budget record is a real file-backed one under the test's own directory. What is
real is everything between: the controllers a browser posts to, the engine that runs the graph, the
archive an ended run rests in, and the grader that reads that archive afterwards.

Every row asserts what a person would see or what the record now holds. A status code on its own
proves nothing about this journey, so no row stops at one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import yaml

from core.ai.evaluation.contracts import (
    Assessment,
    BudgetNotOpenError,
    BudgetPreflightError,
    BudgetScope,
    EvaluationError,
    EvaluationInput,
    FileEvaluationBudgetRecord,
    OwnerReview,
    ReviewedTaskOutcome,
    RunIdentity,
    TaskOutcome,
    cycle_snapshot,
)
from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    InputPresentation,
    JudgeGate,
    MapNode,
    ParameterSpec,
    ResultArtifactSpec,
    RunNode,
    SavedWorkflow,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowContract,
    WorkflowIntent,
    WorkflowPresentation,
    WorkflowResultSpec,
    run_workflow,
)
from core.ai.workflows.domain.presentation import SaveNoteAction
from core.ai.workflows.domain.vocabulary import RequiredGateCheck, Verdict
from core.ai.workflows.infrastructure.folder import FOLDER, folder_toolset
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RequestUsage, RunUsage

from harness.features.authoring.process import STARTS_WORK, WorkflowProcess
from harness.features.evals.domain.pilot import (
    WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS,
    DevelopmentPilotRequest,
    PilotAdmission,
    PilotBaseline,
    PilotInputs,
    PilotParticipant,
    PilotRoleModel,
    PilotTrial,
    UnmeteredPilot,
    pilot_cycle_id,
)
from harness.features.evals.exceptions import UnmeterablePilotError
from harness.features.evals.infrastructure.budgeted_model import BudgetedModel, PilotMeter
from harness.features.evals.infrastructure.files import load_dataset_stamp, load_stamped_dataset
from harness.features.evals.service.pilot import (
    ArchivedRunOutcomeQueries,
    LivePilotTaskRunner,
    PilotCollaborators,
    WorkflowAutomationTaskRunner,
    _open_development_pilot,
    start_development_pilot,
)
from harness.features.running.spend_report import SpendReporter
from harness.features.scheduling.contracts import Scheduler
from harness.session import Runtime, SessionPool
from harness.settings import RunSettings
from tests.conftest import ApproveAll
from tests.support.authoring import a_controller
from tests.support.controllers import a_client_of
from tests.support.environment import world_over
from tests.support.fakes.archive import FakeRunArchive
from tests.support.fakes.broadcaster import FakeBroadcaster
from tests.support.fakes.evaluation_budget import BlindFirstReadLedger
from tests.support.fakes.outcome_runs import FakePilotRuns
from tests.support.fakes.schedules import FakeSchedules
from tests.support.fakes.sessions import APool
from tests.support.fakes.telemetry import FakeTelemetry
from tests.support.running import a_running_controller
from tests.support.runtime import a_runtime
from tests.support.state import a_state, records_under
from tests.support.streaming import streaming_function_model
from tests.unit.test_schedule_ticker import FakeWorkflowStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from core.ai.artifacts import ArtifactRef
    from pydantic import JsonValue
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo

    from harness.features.evals.domain.outcomes import WorkflowAutomationCase
    from harness.features.running.controller import RunningController
    from harness.features.running.domain.ended_run import RunArchiveRecord

REPOSITORY = Path(__file__).resolve().parents[3]
EVALS = REPOSITORY / "templates" / "repo-docs" / "evals"
DATASET = EVALS / "workflow-automation-development.v1.json"
STAMP = EVALS / "workflow-automation-development.v1.stamp.json"

FILING = "wf.file-questions.v1"
REVIEWING = "wf.reviewed-questions.v1"
SWEEPING = "wf.sweep-pages.v1"

REFUSED_BECAUSE = "the page never says which review the question came from"
ASKED_INSTEAD = "name the review beside each question"
TOPIC = "what the reviews left open"
QUESTIONS = ("who owns the retention window", "where the digests are read")


def _filing_arguments(topic: str, questions: Sequence[str]) -> dict[str, JsonValue]:
    """One start's values, spelled as the JSON each parameter declares."""
    asked: list[JsonValue] = list(questions)
    return {"topic": topic, "questions": asked}


def _sweeping_arguments(pages: Sequence[str]) -> dict[str, JsonValue]:
    """One sweep's values, spelled as the JSON its one parameter declares."""
    named: list[JsonValue] = list(pages)
    return {"pages": named}


GOOD = _filing_arguments(TOPIC, QUESTIONS)

ASKED_FOR = WorkflowIntent(
    request="Every week, file each open question from the reviews as its own page.",
    expected_result="One page per open question, and a note I may keep.",
    success_example="Two questions came out of Monday's review and two pages exist.",
    empty_input_behavior="No questions, no pages, and the run says so.",
    failure_behavior="It stops and names the question it could not file.",
)
"""What the person said they wanted, whole, so design can be graded on something they wrote."""


class Draft(BaseModel):
    """What the drafting step produced this attempt."""

    topic: str
    body: str
    attempt: int


class ProposedNote(BaseModel):
    """A note the run proposes and nothing writes: what a person may later choose to keep."""

    page_type: str
    title: str
    summary: str
    sections: tuple[str, ...]


class Filed(BaseModel):
    """What the filing step left behind: the page it wrote, and the note it proposes."""

    page: str
    note_draft: ProposedNote


class Judgement(BaseModel):
    """A reviewer's own verdict: what it decided, why, and what it asked for instead."""

    outcome: Verdict
    feedback: str
    requested_repair: str


class Judgements:
    """How many reviews the deciding step has given, and which of them it refused."""

    def __init__(self, reject_first: int = 0) -> None:
        self.reject_first = reject_first
        self.reviews = 0
        self.saw: list[int] = []

    def decide(self, drafted: Draft) -> bool:
        """Refuse the first `reject_first` reviews and accept every one after them."""
        self.reviews += 1
        self.saw.append(drafted.attempt)
        return self.reviews > self.reject_first


def _filing_spec() -> Workflow:
    """Draft the pages, let a reviewer decide, then file what it accepted."""
    return Workflow(
        name="file-questions",
        version=1,
        description="Draft a page for each open question, have it reviewed, and file what the reviewer accepted.",
        parameters={
            "topic": ParameterSpec(description="what the pages are about", json_schema={"type": "string", "minLength": 1}),
            "questions": ParameterSpec(
                description="the open questions to file",
                json_schema={"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1, "maxItems": 5},
            ),
        },
        nodes=(
            RunNode(
                id="draft",
                work="draft_pages",
                inputs={"topic": FromParameter(parameter="topic"), "questions": FromParameter(parameter="questions")},
            ),
            JudgeGate(id="check", work="judge_pages", reviews=FromNode(node="draft"), rejected_to="draft"),
            RunNode(id="filed", work="file_pages", inputs={"drafted": FromNode(node="check")}),
        ),
        output=FromNode(node="filed"),
        result=WorkflowResultSpec(
            summary="the pages this run filed, and the note it proposes",
            primary_work="draft",
            required_gates=(RequiredGateCheck(gate_id="check", accepted_sources=("judge",)),),
            artifacts=(
                ResultArtifactSpec(
                    id="note_draft",
                    label="The note this run proposes",
                    source=FromNode(node="filed"),
                    field="note_draft",
                    media_type="application/json",
                ),
                ResultArtifactSpec(
                    id="page", label="The page this run wrote", source=FromNode(node="filed"), field="page", media_type="text/markdown"
                ),
            ),
        ),
    )


def _reviewing_spec() -> Workflow:
    """The same shape, with a reviewer that answers in words and two reaches to spend."""
    return Workflow(
        name="reviewed-questions",
        version=1,
        description="Draft a page for each open question, and let a reviewer say in words what it wants changed.",
        parameters={
            "topic": ParameterSpec(description="what the pages are about", json_schema={"type": "string", "minLength": 1}),
            "questions": ParameterSpec(
                description="the open questions to file",
                json_schema={"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
            ),
        },
        nodes=(
            RunNode(
                id="draft",
                work="draft_pages",
                inputs={"topic": FromParameter(parameter="topic"), "questions": FromParameter(parameter="questions")},
            ),
            JudgeGate(
                id="check",
                work="weigh_pages",
                reviews=FromNode(node="draft"),
                approved_when="outcome",
                rejected_to="draft",
                max_attempts=2,
            ),
            RunNode(id="filed", work="file_pages", inputs={"drafted": FromNode(node="check")}),
        ),
        output=FromNode(node="filed"),
        result=WorkflowResultSpec(
            summary="the pages this run filed",
            primary_work="draft",
            required_gates=(RequiredGateCheck(gate_id="check", accepted_sources=("judge",)),),
        ),
    )


def _sweeping_spec() -> Workflow:
    """One worker per page, each free to leave its own page out of what the run collected."""
    return Workflow(
        name="sweep-pages",
        version=1,
        description="Sweep every page the caller names, leaving out any page that cannot be swept.",
        parameters={
            "pages": ParameterSpec(
                description="the pages to sweep",
                json_schema={"type": "array", "items": {"type": "string", "minLength": 1}},
            )
        },
        nodes=(
            MapNode(
                id="swept",
                turns=("sweep_page",),
                over=FromParameter(parameter="pages"),
                each="page",
                on_error="skip",
                abort_after_repeats=None,
            ),
        ),
        output=FromNode(node="swept"),
        result=WorkflowResultSpec(summary="the pages this run swept", primary_work="swept", empty_primary="noop"),
    )


FILING_CARD = SavedWorkflow(
    id=FILING,
    title="File the open questions",
    summary="Draft a page for each open question, have it reviewed, and file what the reviewer accepted.",
    presentation=WorkflowPresentation(
        purpose="Keeps the open questions from a review out of anybody's head.",
        produces="One page per question, and a note you may keep.",
        inputs=(
            InputPresentation(key="topic", label="What the pages are about", control="text"),
            InputPresentation(key="questions", label="The open questions", control="textarea"),
        ),
        actions=(SaveNoteAction(id="keep-the-note", label="Keep as a note", artifact_id="note_draft"),),
    ),
)
"""The card a person reads, where keeping the note is offered as a deliberate act of their own."""


class Journey:
    """One test's own deployment: the steps that ran, the library, and the reviewer's decisions."""

    def __init__(self, home: Path, *, reject_first: int = 0, unsweepable: tuple[str, ...] = ()) -> None:
        self.home = home
        self.judgements = Judgements(reject_first)
        self.drafted: list[Draft] = []
        self.filed: list[Filed] = []
        self.swept: list[str] = []
        self.unsweepable = frozenset(unsweepable)
        self.registry = self._registry()
        self.library = FakeWorkflowStore(
            specs={FILING: _filing_spec(), REVIEWING: _reviewing_spec(), SWEEPING: _sweeping_spec()},
            cards={FILING: FILING_CARD},
        )
        self.records = records_under(home)
        self.pool = SessionPool(a_runtime(registry=self.registry, library=self.library, state=a_state(home)))

    def _registry(self) -> StepRegistry:
        registry = StepRegistry()
        attempts = {"count": 0}

        async def draft_pages(topic: str, questions: list[str]) -> Draft:
            attempts["count"] += 1
            # The attempt rides in the body, so a reader of the filed page can tell which draft
            # reached it. Without it, a rework and a first pass leave identical bytes.
            drafted = Draft(topic=topic, body=f"{' / '.join(questions)} (draft {attempts['count']})", attempt=attempts["count"])
            self.drafted.append(drafted)
            return drafted

        async def judge_pages(drafted: Draft) -> bool:
            return self.judgements.decide(drafted)

        async def weigh_pages(drafted: Draft) -> Judgement:
            if self.judgements.decide(drafted):
                return Judgement(outcome="approve", feedback="it names the review beside each question", requested_repair="")
            return Judgement(outcome="reject", feedback=REFUSED_BECAUSE, requested_repair=ASKED_INSTEAD)

        async def file_pages(drafted: Draft) -> Filed:
            filed = Filed(
                page=f"# {drafted.topic}\n\n{drafted.body}\n",
                note_draft=ProposedNote(
                    page_type="question",
                    title=drafted.topic,
                    summary=f"{len(drafted.body.split(' / '))} question(s) came out of this review.",
                    sections=("What was asked", "What settles it"),
                ),
            )
            self.filed.append(filed)
            return filed

        async def sweep_page(page: str) -> str:
            if page in self.unsweepable:
                msg = f"{page} cannot be swept while it is open elsewhere"
                raise RuntimeError(msg)
            self.swept.append(page)
            return page

        for step in (draft_pages, judge_pages, weigh_pages, file_pages, sweep_page):
            registry.register(step)
        return registry

    def running(self) -> RunningController:
        """The controller a browser's run requests reach, writing its records under this home."""
        return a_running_controller(self.pool, self.home, run=RunSettings())

    async def start(self, workflow_id: str, arguments: dict[str, JsonValue], *, run_title: str = "") -> tuple[int, dict[str, object]]:
        """Post one start the way a browser does, and answer the status and body it got back."""
        async with a_client_of(self.running()) as client:
            answer = await client.post("/api/run", json={"workflow_id": workflow_id, "arguments": arguments, "run_title": run_title})
            body: dict[str, object] = answer.json()
            return answer.status_code, body

    async def finish(self, session_id: str) -> str:
        """Let the run launched in `session_id` end, and answer the id it was archived under."""
        session = await self.pool.get(session_id)
        assert session.active.task is not None, "the start left no run to wait on"
        await session.active.task
        return session.active.run_id or ""

    async def ran(self, workflow_id: str, arguments: dict[str, JsonValue], *, run_title: str = "") -> RunArchiveRecord:
        """One completed start, read back out of the archive the way every later reader reads it."""
        status, body = await self.start(workflow_id, arguments, run_title=run_title)
        assert status == 200, body
        record = self.records.archive.get(await self.finish(str(body["session_id"])))
        assert record is not None, "the run ended and nothing was archived"
        return record


@pytest.fixture
def journey(tmp_path: Path) -> Journey:
    """A deployment whose reviewer accepts the first draft it is shown."""
    return Journey(tmp_path)


def _case(case_id: str) -> WorkflowAutomationCase:
    """One development case, out of the template that owns its vocabulary."""
    found = load_stamped_dataset(DATASET, STAMP).case(case_id)
    assert found is not None, f"the development set carries no case {case_id!r}"
    return found


def _graded(record: RunArchiveRecord, case_id: str, *, intent: WorkflowIntent | None = None) -> TaskOutcome:
    """What the grader makes of one archived run, from the record's own facts alone."""
    reader = ArchivedRunOutcomeQueries(FakeRunArchive([record]))
    outcome = reader.outcome_for(record.run_id, _case(case_id), intent=intent)
    assert outcome is not None, "the archive holds the record the grader was pointed at"
    return outcome


def _beside_reviews(outcome: TaskOutcome, reviews: tuple[OwnerReview, ...]) -> ReviewedTaskOutcome:
    """The graded outcome beside the reviews a host actually holds for its run.

    Acceptance is read from those reviews and from nothing else: none at all can only be unknown,
    and the latest one is what the owner last said.
    """
    if not reviews:
        return ReviewedTaskOutcome(outcome=outcome, owner_acceptance=Assessment(status="unknown", reason="nobody has reviewed this run"))
    latest = reviews[-1]
    return ReviewedTaskOutcome(
        outcome=outcome,
        owner_acceptance=Assessment(status="pass" if latest.outcome == "accepted" else "fail", reason=latest.reason),
        reviews=reviews,
    )


def _reviews_in(history: dict[str, object]) -> tuple[OwnerReview, ...]:
    """Every owner review the run's own history route answered with, validated as the events they are."""
    rows = history["owner_reviews"]
    assert isinstance(rows, list), "the history route answers with a list of reviews"
    return tuple(OwnerReview.model_validate(row) for row in rows)


def _ref(record: RunArchiveRecord, artifact_id: str) -> ArtifactRef:
    """The reference this run itself fixed under `artifact_id`."""
    assert record.result is not None, "the run recorded no checked outcome to take a reference from"
    found = next((ref for ref in record.result.artifacts if ref.id == artifact_id), None)
    assert found is not None, f"the run fixed no value under {artifact_id!r}"
    return found


# -- 1. a request to design ------------------------------------------------------------


def _authoring_process(journey: Journey, home: Path, *, design_only: bool) -> tuple[WorkflowProcess, Runtime]:
    """One authoring turn's own surface, and the session it was built for."""
    session = a_runtime(registry=journey.registry, state=a_state(home))
    session.design_only = design_only
    process = WorkflowProcess(
        port=WorkflowContract[Runtime](
            source=folder_toolset(home / "library"), vocabulary=FOLDER, expose=(), registry=journey.registry, saved_by="test"
        ),
        records=journey.records,
        reporter=SpendReporter(FakeTelemetry()),
        scheduler=Scheduler(FakeSchedules(), FakeSchedules(), FakeBroadcaster()),
        sessions=APool(SessionPool(session)),
        topics=FakeBroadcaster(),
        run=RunSettings(),
    )
    return process, session


async def test_a_request_to_design_withholds_exactly_the_verbs_that_start_work(tmp_path: Path) -> None:
    """The same surface, twice: an ordinary turn holds the verbs that start work and a design turn does not."""
    journey = Journey(tmp_path)
    designing, session = _authoring_process(journey, tmp_path / "designing", design_only=True)
    ordinary, other = _authoring_process(journey, tmp_path / "ordinary", design_only=False)
    asked: RunContext[Runtime] = RunContext(deps=session, model=TestModel(), usage=RunUsage())
    unrestricted: RunContext[Runtime] = RunContext(deps=other, model=TestModel(), usage=RunUsage())

    offered = frozenset(await designing.get_toolset().get_tools(asked))
    everything = frozenset(await ordinary.get_toolset().get_tools(unrestricted))

    assert offered, "a design turn was offered no tools at all, so withholding them proves nothing"
    assert everything >= STARTS_WORK, "an ordinary turn is missing a verb that starts work, so there is nothing to withhold"
    assert everything - offered == STARTS_WORK, f"designing withheld {sorted(everything - offered)}"
    assert offered.isdisjoint(STARTS_WORK), f"designing still offers {sorted(offered & STARTS_WORK)}"
    assert session.active.task is None, "designing started a run"


# -- 2. a valid manual automation ------------------------------------------------------


async def test_one_deliberate_start_files_the_work_and_the_record_says_what_settled(journey: Journey) -> None:
    """The person sees the pages, and the record carries counts that add up and a gate that accepted."""
    record = await journey.ran(FILING, GOOD, run_title="File the open questions, this week")

    assert [filed.page for filed in journey.filed] == [f"# {TOPIC}\n\n{' / '.join(QUESTIONS)} (draft 1)\n"], "the page a reader would open"
    assert record.result is not None
    assert record.result.status == "succeeded"
    assert (record.result.attempted, record.result.succeeded, record.result.failed) == (1, 1, 0)
    assert record.result.retry_attempts == 0, "nothing was reworked, so nothing counts as rework"
    assert [check.id for check in record.result.checks if check.passed] == ["check"]
    assert record.run_title == "File the open questions, this week"
    assert record.params == GOOD, "the record holds the values the person actually chose"

    graded = _graded(record, "wa-dev-04-success", intent=ASKED_FOR)
    assert graded.task_success.status == "pass"
    assert graded.structural_validity.status == "pass"
    assert graded.evidence_support.status == "pass"
    assert graded.workflow_design.status == "pass"


# -- 3. a typed input the start refuses ------------------------------------------------


@pytest.mark.parametrize(
    ("case", "supplied", "names"),
    [
        ("a question that is the empty string", _filing_arguments(TOPIC, ("who owns the window", "")), "$[1]"),
        ("no questions at all", _filing_arguments(TOPIC, ()), "questions"),
        ("a topic that is not text", {**_filing_arguments(TOPIC, QUESTIONS), "topic": 7}, "topic"),
    ],
)
async def test_a_value_the_parameter_refuses_starts_nothing(
    case: str, supplied: dict[str, JsonValue], names: str, journey: Journey
) -> None:
    """The refusal names the value, and the person is left with no run and no record of one."""
    del case
    status, body = await journey.start(FILING, supplied)

    assert status == 400, body
    assert names in str(body["message"]), body
    assert journey.drafted == [], "a refused start still ran the work"
    assert journey.records.archive.recent(10) == [], "a refused start still archived a run"


# -- 4. a reviewer that refuses, and the repair it asked for ---------------------------


async def test_a_refused_draft_is_reworked_and_the_rework_is_counted_apart_from_the_work(tmp_path: Path) -> None:
    """The second draft is the one that was filed, and the rework never inflates what settled."""
    journey = Journey(tmp_path, reject_first=1)

    record = await journey.ran(FILING, GOOD)

    assert journey.judgements.saw == [1, 2], "the reviewer saw the first draft and then its repair"
    assert [filed.page for filed in journey.filed] == [
        f"# {TOPIC}\n\n{' / '.join(QUESTIONS)} (draft 2)\n"
    ], "the repaired draft is the one that was filed, and the refused one reached nothing"
    assert record.result is not None
    assert record.result.status == "succeeded"
    assert record.result.retry_attempts == 1, "one rework, recorded as rework"
    assert (record.result.attempted, record.result.succeeded) == (1, 1), "a repair is not a second piece of work"
    accepted = next(check for check in record.result.checks if check.id == "check")
    assert accepted.passed is True
    assert accepted.observed_source == "judge", "a machine decided this gate, and the record says so"

    graded = _graded(record, "wa-dev-08-repair-after-rejection", intent=ASKED_FOR)
    assert graded.task_success.status == "pass"
    assert "1 rework(s)" in graded.task_success.reason


async def test_a_reviewer_that_answers_in_words_carries_them_into_the_record(tmp_path: Path) -> None:
    """The gate spends its last reach, and what the reviewer said and asked for is on the record.

    A reviewer that refuses every reach is the one path where its own words survive into the checked
    outcome, because there is no later acceptance to replace them.
    """
    journey = Journey(tmp_path, reject_first=9)

    record = await journey.ran(REVIEWING, GOOD)

    assert journey.judgements.saw == [1, 2], "the reviewer refused the first draft, and refused its repair too"
    assert journey.filed == [], "nothing was filed, because nothing was ever accepted"
    assert record.result is not None
    assert record.result.status == "failed"
    refused = next(check for check in record.result.checks if check.id == "check")
    assert refused.passed is False
    assert refused.observed_source == "judge"
    assert refused.feedback == REFUSED_BECAUSE, "the reviewer's own words, not a sentence this code composed"
    assert refused.requested_repair == ASKED_INSTEAD, "and what it asked for instead"
    assert ASKED_INSTEAD in record.result.summary, "the run's own summary says what it was asked to change"

    graded = _graded(record, "wa-dev-26-decision-brief-reviewer-refuses-once", intent=ASKED_FOR)
    assert graded.task_success.status == "fail", "a refusal that spent its reaches is not work done"
    assert graded.evidence_support.status == "fail", "and the required gate did not accept the value"


# -- 5. a second process reading the trial a first one left behind ---------------------


_AT = datetime(2026, 9, 16, 9, 0, tzinfo=UTC)
"""One instant every budget call in this module is made at, so a deadline never moves under a row."""

_IDENTITY = RunIdentity(
    model="deterministic",
    model_version="1",
    prompt_fingerprint="a" * 64,
    implementation_fingerprint="b" * 64,
    conditions_fingerprint="c" * 64,
    grader_fingerprint="d" * 64,
)
"""The identity a runner is handed. It carries no case label, and nothing it holds reaches a run."""


def _drafts(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """One designer answer, so the trial has a real request to admit and settle."""
    del messages, info
    return ModelResponse(parts=[TextPart(content="drafted")], usage=RequestUsage(input_tokens=10, output_tokens=5))


def _answers_a_note(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
    """One step agent's answer, in whatever shape the briefing it was built for declared."""
    del messages
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {"note": "noted"})], usage=RequestUsage(input_tokens=10, output_tokens=5))


def _one_step_spec() -> Workflow:
    """One invented step, so the model behind its tier is the only thing under test."""
    task = Briefing(
        name="dig",
        instructions="Answer the question the caller asked in one short note, and add nothing it did not ask for.",
        inputs={"question": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}),
    )
    return Workflow(
        name="dig-once",
        version=1,
        description="One invented step, run on the worker tier, so the model behind it is the only question.",
        parameters={"question": ParameterSpec(description="what to look into", json_schema={"type": "string"})},
        inline=(task,),
        nodes=(RunNode(id="dig", work="dig", inputs={"question": FromParameter(parameter="question")}),),
        output=FromNode(node="dig"),
    )


def _a_quarter(response: ModelResponse) -> Decimal:
    """A fixed price, standing in for the resolver a live trial settles against."""
    del response
    return Decimal("0.25")


PRICED_ROLES = (
    PilotRoleModel(role="designer", model_name="gpt-4o", provider_name="openai"),
    PilotRoleModel(role="worker", model_name="claude-sonnet-4-5", provider_name="anthropic"),
)
"""Two roles on models the deployment's own price resolver can actually name a figure for."""

UNPRICEABLE_ROLE = PilotRoleModel(role="reviewer", model_name="a-model-no-table-carries", provider_name="nowhere")
"""A third role on a model no table and no provider prices, which is what the preflight has to catch."""


def _admission(
    ledger: FileEvaluationBudgetRecord,
    *,
    roles: tuple[PilotRoleModel, ...] = PRICED_ROLES,
    unbudgeted: tuple[str, ...] = (),
) -> PilotAdmission:
    """What a one-process pilot is checked against, read off the record the cycle will open on."""
    identity = ledger.state_identity()
    return PilotAdmission(
        role_models=roles,
        participants=(
            PilotParticipant(
                name="the process driving this trial",
                canonical_state_root=identity.canonical_state_root,
                device_id=identity.device_id,
                cooperative_locking=True,
            ),
        ),
        unbudgeted_model_paths=unbudgeted,
    )


async def test_a_second_budget_record_reads_the_trial_the_first_one_left_behind(tmp_path: Path) -> None:
    """The limits, the trial and the spend live on disk, so a process opening the record again continues it.

    Nothing is in flight and no work resumes here. What is proven is that one trial's counters survive
    the process that wrote them, which is what lets a pilot spread over several of them add up.
    """
    root = tmp_path / "budget"
    ledger = FileEvaluationBudgetRecord(root)
    scope = BudgetScope(cycle_id="cycle-journey", trial_id="trial-1")
    at = _AT

    await _open_development_pilot(ledger, cycle_id=scope.cycle_id, admission=_admission(ledger), at=at)
    await ledger.begin_trial(scope, at=at)
    model = BudgetedModel(
        FunctionModel(function=_drafts), record=ledger, scope=scope, role="designer", charge=_a_quarter, clock=lambda: at
    )

    answered = await model.request([], None, model.customize_request_parameters(ModelRequestParameters()))

    assert answered.parts, "the wrapped model answered through the record"

    restarted = FileEvaluationBudgetRecord(root)
    state = await restarted.read(scope.cycle_id)

    assert state is not None, "a restarted worker found no cycle to continue"
    assert state.limits == WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS, "the limits the trial opened under, unchanged"
    snapshot = cycle_snapshot(state, scope)
    assert snapshot.trial_requests == 1, "the request the process before it made is still counted"
    assert snapshot.trial_measured_model_spent_usd == Decimal("0.25")
    assert snapshot.in_flight_request_id is None, "a settled request leaves nothing in flight"
    assert snapshot.unresolved_charge is False


# -- a live pilot: refused before it spends, and measured once it has --------------------


async def test_a_role_the_deployments_own_resolver_cannot_price_refuses_the_pilot(tmp_path: Path) -> None:
    """The refusal is the function's own, taken from the price resolver rather than from a caller.

    Nobody tells `_open_development_pilot` which roles are unpriceable. It asks the same resolver that
    settles a real response, so a caller that has not noticed cannot open a cycle by staying quiet.
    """
    ledger = FileEvaluationBudgetRecord(tmp_path / "budget")
    admission = _admission(ledger, roles=(*PRICED_ROLES, UNPRICEABLE_ROLE))

    with pytest.raises(BudgetPreflightError) as refused:
        await _open_development_pilot(ledger, cycle_id="cycle-unpriced", admission=admission)

    assert refused.value.refusal == "pricing_unknown"
    assert "reviewer" in str(refused.value), "the refusal names the role nothing could price"
    assert "designer" not in str(refused.value), "and not the roles it could"
    assert await ledger.read("cycle-unpriced") is None, "a refused pilot opened no cycle to spend under"


async def test_a_model_path_this_deployment_does_not_budget_refuses_the_pilot(tmp_path: Path) -> None:
    """Coverage is derived from the configured roles, so a path outside them has to be declared, and refuses."""
    ledger = FileEvaluationBudgetRecord(tmp_path / "budget")
    admission = _admission(ledger, unbudgeted=("an external agent this deployment shells out to",))

    assert admission.coverage.budgeted_model_paths == ("designer:openai:gpt-4o", "worker:anthropic:claude-sonnet-4-5")
    with pytest.raises(BudgetPreflightError) as refused:
        await _open_development_pilot(ledger, cycle_id="cycle-uncovered", admission=admission)

    assert refused.value.refusal == "unbudgeted_model_path"
    assert "an external agent this deployment shells out to" in str(refused.value)
    assert await ledger.read("cycle-uncovered") is None, "a refused pilot opened no cycle to spend under"


async def test_a_deployment_whose_roles_are_all_priced_opens_its_cycle(tmp_path: Path) -> None:
    """The positive half of the same check, against the production resolver and no injected price."""
    ledger = FileEvaluationBudgetRecord(tmp_path / "budget")

    preflight = await _open_development_pilot(ledger, cycle_id="cycle-priced", admission=_admission(ledger))

    assert preflight.pricing_known is True
    assert preflight.roles_without_price == ()
    assert preflight.limits == WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
    state = await ledger.read("cycle-priced")
    assert state is not None, "a pilot that passed its preflight has a cycle to spend under"
    assert state.limits == WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS


async def test_a_live_trial_reports_what_the_record_measured_beside_the_run_it_read(journey: Journey) -> None:
    """The trial's own requests and charge come from the cycle, and the run comes from the archive."""
    record = await journey.ran(FILING, GOOD)
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    scope = BudgetScope(cycle_id="cycle-journey", trial_id="trial-1")
    await _open_development_pilot(ledger, cycle_id=scope.cycle_id, admission=_admission(ledger), at=_AT)
    await ledger.begin_trial(scope, at=_AT)
    admitted = await ledger.admit(scope, "request-1", "designer", at=_AT)
    await ledger.settle(admitted, Decimal("0.40"), at=_AT)
    runner = LivePilotTaskRunner(
        scope.cycle_id,
        ledger,
        FakePilotRuns([PilotTrial(case_id="wa-dev-04-success", repetition=0, run_id=record.run_id, trial_id=scope.trial_id)]),
        FakeRunArchive([record]),
    )

    execution = await runner(EvaluationInput(case_id="wa-dev-04-success", input=TOPIC), _IDENTITY, 0)

    assert execution.run_id == record.run_id
    assert execution.usage.requests == 1, "the one request the record admitted"
    assert execution.usage.cost_usd == 0.40
    seen = {row.code: row.detail for row in execution.diagnostics}
    assert seen["trial_requests"] == "1"
    assert seen["token_source_run"] == record.run_id, "the run the token counts belong to, named apart from the trial's"
    assert "unresolved_charge" not in seen, "every charge on this cycle was priced"
    assert "trial_cost_overshoot_usd" not in seen, "nothing crossed a stop threshold"


async def test_a_live_trial_whose_charge_nothing_could_price_reports_an_unknown_cost(journey: Journey) -> None:
    """An unresolved charge leaves the cost unknown and says so, rather than reporting the priced part."""
    record = await journey.ran(FILING, GOOD)
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    scope = BudgetScope(cycle_id="cycle-unresolved", trial_id="trial-1")
    await _open_development_pilot(ledger, cycle_id=scope.cycle_id, admission=_admission(ledger), at=_AT)
    await ledger.begin_trial(scope, at=_AT)
    admitted = await ledger.admit(scope, "request-1", "designer", at=_AT)
    await ledger.settle(admitted, None, at=_AT)
    runner = LivePilotTaskRunner(
        scope.cycle_id,
        ledger,
        FakePilotRuns([PilotTrial(case_id="wa-dev-04-success", repetition=0, run_id=record.run_id, trial_id=scope.trial_id)]),
        FakeRunArchive([record]),
    )

    execution = await runner(EvaluationInput(case_id="wa-dev-04-success", input=TOPIC), _IDENTITY, 0)

    assert execution.usage.cost_usd is None, "part of an unresolvable charge is not the charge"
    assert execution.usage.requests == 1
    assert "unresolved_charge" in {row.code for row in execution.diagnostics}


async def test_a_live_trial_the_cycle_never_opened_is_a_failed_slot(journey: Journey) -> None:
    """Nothing measured it, so nothing scores it, and what the run itself recorded goes out with the failure."""
    record = await journey.ran(FILING, GOOD)
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    runner = LivePilotTaskRunner(
        "cycle-nobody-opened",
        ledger,
        FakePilotRuns([PilotTrial(case_id="wa-dev-04-success", repetition=0, run_id=record.run_id, trial_id="trial-1")]),
        FakeRunArchive([record]),
    )

    with pytest.raises(EvaluationError, match="was never opened"):
        await runner(EvaluationInput(case_id="wa-dev-04-success", input=TOPIC), _IDENTITY, 0)


# -- the one call that starts a live pilot ---------------------------------------------


PILOT_INPUTS = PilotInputs(
    model="gpt-4o",
    model_version="2024-08-06",
    prompt="Design the automation the person asked for, and say what a finished run leaves them holding.",
    implementation="harness.features.evals.outcome_runner",
    tools=("search", "fetch"),
    configuration="in-process engine, gates held for a person, one priced call in flight",
    grader="harness.features.evals.service.pilot.ArchivedRunOutcomeQueries",
)
BASELINE = PilotBaseline(description="The same work done by hand, from the reviews, once a week.")
LIMITATIONS = ("Author-visible cases only, so nothing here generalizes beyond the set they were written against.",)


def _pilot_request_on(ledger: FileEvaluationBudgetRecord) -> DevelopmentPilotRequest:
    """One whole live pilot, as the person starting it names it, checked against this record's root."""
    return DevelopmentPilotRequest(
        dataset_path=DATASET,
        stamp_path=STAMP,
        inputs=PILOT_INPUTS,
        admission=_admission(ledger),
        baseline=BASELINE,
        limitations=LIMITATIONS,
    )


def _mounted_meter(ledger: FileEvaluationBudgetRecord, asked: DevelopmentPilotRequest) -> PilotMeter:
    """A meter for this pilot's own cycle, with an environment already built through it.

    Mounting is what the environment builder does when a deployment hands it this hook, and building
    one here is what makes the meter hold models rather than an intention to hold them.
    """
    meter = PilotMeter(
        ledger, pilot_cycle_id(load_stamped_dataset(asked.dataset_path, asked.stamp_path), asked.inputs), charge=_a_quarter, clock=lambda: _AT
    )
    for tier in ("worker", "orchestrator", "helper"):
        meter(FunctionModel(function=_drafts), role=tier)
    return meter


async def test_a_metered_environment_puts_every_step_agents_model_through_the_record(tmp_path: Path) -> None:
    """The mount is the harness's own hook, and a step run through it lands a request on the record.

    This is the seam a deployment installs the meter at: the environment builder asks the hook once
    per tier at build time and the step agents of that tier run on whatever it answered.
    """
    ledger = FileEvaluationBudgetRecord(tmp_path / "budget")
    meter = PilotMeter(ledger, "wa-pilot-metered-environment", charge=_a_quarter, clock=lambda: _AT)
    scope = BudgetScope(cycle_id=meter.cycle_id, trial_id="trial-1")
    await ledger.begin_cycle(meter.cycle_id, WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS, pricing_known=True, at=_AT)
    await ledger.begin_trial(scope, at=_AT)

    environment = await world_over(streaming_function_model(_answers_a_note), state=tmp_path, wrap_model=meter)
    meter.for_trial("trial-1")
    answered = await run_workflow(_one_step_spec(), environment, params={"question": "which queue"}, approval=ApproveAll())

    assert sorted(meter.metered_roles) == ["designer", "reviewer", "worker"], "every tier the builder asked about was mounted"
    assert "noted" in str(answered), "the step ran on the model the meter handed back"
    state = await ledger.read(meter.cycle_id)
    assert state is not None
    snapshot = cycle_snapshot(state, scope)
    assert snapshot.trial_requests >= 1, "the step's own model request was admitted by the record"
    assert snapshot.trial_measured_model_spent_usd > 0, "and settled for what it cost"


async def test_a_second_caller_opening_one_cycle_at_the_same_moment_continues_it(tmp_path: Path) -> None:
    """A race is a continuation, not a disagreement about limits, and the message says so by not appearing."""
    real = FileEvaluationBudgetRecord(tmp_path / "budget")
    admission = _admission(real)
    await _open_development_pilot(real, cycle_id="cycle-raced", admission=admission, at=_AT)
    blind = BlindFirstReadLedger(real)

    preflight = await _open_development_pilot(blind, cycle_id="cycle-raced", admission=_admission(real))

    assert preflight.limits == WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
    state = await real.read("cycle-raced")
    assert state is not None
    assert state.started_at == _AT, "the cycle kept the start the first caller gave it"


async def test_a_meter_built_over_another_cycle_does_not_open_this_pilot(journey: Journey) -> None:
    """The meter has to be this pilot's own, and being a meter at all is not enough."""
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    asked = _pilot_request_on(ledger)
    elsewhere = PilotMeter(ledger, "wa-pilot-some-other-measurement")
    elsewhere(FunctionModel(function=_drafts), role="worker")

    with pytest.raises(UnmeterablePilotError, match="measures cycle 'wa-pilot-some-other-measurement'"):
        await start_development_pilot(ledger, asked, PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive()), elsewhere)

    assert list((journey.home / "budget").glob("**/*.json")) == [], "nothing opened"


async def test_a_meter_reporting_to_another_record_does_not_open_this_pilot(journey: Journey) -> None:
    """Two records in one directory tree are two places to count, and a pilot reads only its own."""
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    somewhere_else = FileEvaluationBudgetRecord(journey.home / "other-budget")
    asked = _pilot_request_on(ledger)
    meter = _mounted_meter(somewhere_else, asked)

    with pytest.raises(UnmeterablePilotError, match="different budget record"):
        await start_development_pilot(ledger, asked, PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive()), meter)


async def test_a_written_reason_for_running_unmetered_is_one_somebody_wrote(journey: Journey) -> None:
    """The reason lands in a permanent seal, so whitespace is refused rather than recorded as a reason."""
    del journey

    with pytest.raises(ValueError, match="whitespace is not one"):
        UnmeteredPilot(reason="   ")

    assert UnmeteredPilot(reason="  read by hand  ").reason == "read by hand", "as written, without the typing around it"


async def test_a_meter_nothing_has_ever_asked_for_a_model_does_not_open_a_pilot(journey: Journey) -> None:
    """The one thing the mounted check catches, and the whole of what it proves, driven both ways.

    A meter nothing has asked refuses. A meter one hand call has touched passes, which is the limit
    of what this side can establish: nothing here can see which environment did the asking.
    """
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    asked = _pilot_request_on(ledger)
    cycle = pilot_cycle_id(load_stamped_dataset(asked.dataset_path, asked.stamp_path), asked.inputs)
    by_hand = PilotMeter(ledger, cycle, charge=_a_quarter, clock=lambda: _AT)

    with pytest.raises(UnmeterablePilotError, match="nothing has asked this meter"):
        await start_development_pilot(ledger, asked, PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive()), by_hand)
    assert list((journey.home / "budget").glob("**/*.json")) == [], "a pilot nothing measures opened no cycle to spend under"

    by_hand(FunctionModel(function=_drafts), role="helper")
    opened = await start_development_pilot(ledger, asked, PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive()), by_hand)

    assert opened.handoff.frozen.metered_roles == ("designer",), "one hand call is enough, and the seal names only it"


async def test_running_with_nothing_measuring_has_to_be_asked_for_in_writing(journey: Journey) -> None:
    """The unmetered path exists, it is never a default, and the seal it produces says so itself."""
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    asked = _pilot_request_on(ledger)

    opened = await start_development_pilot(
        ledger,
        asked,
        PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive()),
        UnmeteredPilot(reason="reading the journey by hand, with no model reached at all"),
    )

    assert opened.handoff.frozen.metered is False
    assert opened.handoff.frozen.metered_roles == ()
    assert opened.handoff.frozen.unmetered_reason == "reading the journey by hand, with no model reached at all"
    assert opened.handoff.frozen.limits == WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS, "the thresholds are recorded, and nothing enforces them"


async def test_a_metered_pilot_seals_the_roles_it_measured_and_its_models_reach_the_record(journey: Journey) -> None:
    """The mounted models are the record's own, and a request through one lands on this pilot's cycle."""
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    asked = _pilot_request_on(ledger)
    meter = _mounted_meter(ledger, asked)

    opened = await start_development_pilot(ledger, asked, PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive()), meter)

    assert opened.handoff.frozen.metered is True
    assert sorted(opened.handoff.frozen.metered_roles) == ["designer", "reviewer", "worker"]
    assert opened.handoff.frozen.unmetered_reason == ""

    scope = BudgetScope(cycle_id=opened.cycle_id, trial_id="trial-1")
    await ledger.begin_trial(scope, at=_AT)
    meter.for_trial("trial-1")
    mounted = meter.mounted[0]
    answered = await mounted.request([], None, mounted.customize_request_parameters(ModelRequestParameters()))

    assert answered.parts, "the mounted model answered"
    state = await ledger.read(opened.cycle_id)
    assert state is not None
    assert cycle_snapshot(state, scope).trial_requests == 1, "and the request it made is on this pilot's own cycle"


async def test_a_request_made_before_any_trial_began_is_refused_rather_than_charged_to_one(journey: Journey) -> None:
    """A meter names no real trial until it is moved onto one, so an early request lands nowhere."""
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    asked = _pilot_request_on(ledger)
    meter = _mounted_meter(ledger, asked)
    await start_development_pilot(ledger, asked, PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive()), meter)
    mounted = meter.mounted[0]

    with pytest.raises(BudgetNotOpenError, match="was never begun"):
        await mounted.request([], None, mounted.customize_request_parameters(ModelRequestParameters()))


async def test_moving_the_meter_on_points_every_mounted_model_at_the_new_trial(journey: Journey) -> None:
    """One environment performs many trials, so the models it holds move with the pilot."""
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")
    meter = _mounted_meter(ledger, _pilot_request_on(ledger))

    meter.for_trial("trial-7")

    assert {mounted.scope.trial_id for mounted in meter.mounted} == {"trial-7"}
    assert {mounted.scope.cycle_id for mounted in meter.mounted} == {meter.cycle_id}
    assert sorted(meter.metered_roles) == ["designer", "reviewer", "worker"]


async def test_one_call_opens_a_live_pilot_and_hands_back_the_runner_over_it(journey: Journey) -> None:
    """Everything a person starting a measured pilot has to call, in one call, opening one cycle."""
    record = await journey.ran(FILING, GOOD)
    ledger = FileEvaluationBudgetRecord(journey.home / "budget")

    meter = _mounted_meter(ledger, _pilot_request_on(ledger))
    opened = await start_development_pilot(
        ledger, _pilot_request_on(ledger), PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive()), meter
    )

    assert opened.dataset.name == "workflow-automation-development", "the stamped dataset, loaded by the call itself"
    assert opened.handoff.frozen.limits == WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS
    assert opened.handoff.frozen.identity.model == "gpt-4o"
    assert opened.handoff.measured is False, "opening a pilot measures nothing"
    assert opened.preflight.pricing_known is True
    state = await ledger.read(opened.cycle_id)
    assert state is not None, "the one cycle this pilot spends under is open"
    assert state.limits == WORKFLOW_AUTOMATION_DEVELOPMENT_LIMITS

    trial = PilotTrial(case_id="wa-dev-04-success", repetition=0, run_id=record.run_id, trial_id="trial-1")
    bound = LivePilotTaskRunner(opened.cycle_id, ledger, FakePilotRuns([trial]), FakeRunArchive([record]))
    await ledger.begin_trial(BudgetScope(cycle_id=opened.cycle_id, trial_id="trial-1"), at=_AT)
    execution = await bound(EvaluationInput(case_id="wa-dev-04-success", input=TOPIC), _IDENTITY, 0)

    assert execution.run_id == record.run_id, "the runner the call hands back reads the journey's own runs"


async def test_the_same_pilot_opened_again_continues_its_one_cycle(tmp_path: Path) -> None:
    """The cycle id is the pilot's own content, so the cycle stop threshold bounds the pilot, not a sitting."""
    ledger = FileEvaluationBudgetRecord(tmp_path / "budget")
    journey = PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive())

    first = await start_development_pilot(ledger, _pilot_request_on(ledger), journey, _mounted_meter(ledger, _pilot_request_on(ledger)))
    scope = BudgetScope(cycle_id=first.cycle_id, trial_id="trial-1")
    await ledger.begin_trial(scope, at=_AT)
    admitted = await ledger.admit(scope, "request-1", "designer", at=_AT)
    await ledger.settle(admitted, Decimal("3.00"), at=_AT)

    again = await start_development_pilot(ledger, _pilot_request_on(ledger), journey, _mounted_meter(ledger, _pilot_request_on(ledger)))

    assert again.cycle_id == first.cycle_id, "the same pilot is the same cycle"
    state = await ledger.read(again.cycle_id)
    assert state is not None
    assert cycle_snapshot(state, scope).cycle_measured_model_spent_usd == Decimal("3.00"), "and the same accrued spend"


async def test_a_pilot_whose_content_changed_is_a_different_pilot_with_a_cycle_of_its_own(tmp_path: Path) -> None:
    """Change a word of the prompt and the stop threshold is another pilot's, which is the honest reading."""
    ledger = FileEvaluationBudgetRecord(tmp_path / "budget")
    journey = PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive())
    asked = _pilot_request_on(ledger)
    reworded = asked.model_copy(update={"inputs": PILOT_INPUTS.model_copy(update={"prompt": "Design it, and say nothing else."})})

    first = await start_development_pilot(ledger, asked, journey, _mounted_meter(ledger, asked))
    second = await start_development_pilot(ledger, reworded, journey, _mounted_meter(ledger, reworded))

    assert first.cycle_id != second.cycle_id
    assert first.handoff.frozen.identity.prompt_fingerprint != second.handoff.frozen.identity.prompt_fingerprint
    assert first.handoff.frozen.dataset_fingerprint == second.handoff.frozen.dataset_fingerprint, "the cases did not move"


async def test_a_pilot_whose_stamp_does_not_cover_its_dataset_opens_no_cycle(tmp_path: Path) -> None:
    """The stamp is checked by the same call, before anything is sealed or opened."""
    ledger = FileEvaluationBudgetRecord(tmp_path / "budget")
    moved = tmp_path / "moved.stamp.json"
    moved.write_text(load_dataset_stamp(STAMP).model_copy(update={"version": "v2"}).model_dump_json(), encoding="utf-8")
    asked = _pilot_request_on(ledger).model_copy(update={"stamp_path": moved})

    with pytest.raises(ValueError, match="the stamp names"):
        await start_development_pilot(
            ledger, asked, PilotCollaborators(runs=FakePilotRuns(), archive=FakeRunArchive()), UnmeteredPilot(reason="the stamp is wrong")
        )

    assert list((tmp_path / "budget").glob("**/*.json")) == [], "nothing opened a cycle for a pilot that was refused"


# -- 6. a batch that skipped every item ------------------------------------------------


async def test_a_batch_that_skipped_every_item_did_none_of_the_work(tmp_path: Path) -> None:
    """Every page was left out, so the run reads as work not done rather than as a quiet success."""
    pages = ("retention.md", "digests.md", "questions.md")
    journey = Journey(tmp_path, unsweepable=pages)

    record = await journey.ran(SWEEPING, _sweeping_arguments(pages))

    assert journey.swept == [], "nothing was swept"
    assert record.outcome == "completed", "the graph reached its end, which is a separate fact"
    assert record.result is not None
    assert (record.result.attempted, record.result.skipped, record.result.succeeded) == (3, 3, 0)
    assert record.result.status == "failed", "a run that skipped everything did not do the work"

    graded = _graded(record, "wa-dev-07-majority-skips")
    assert graded.task_success.status == "fail"
    assert "3 skipped" in graded.task_success.reason
    assert graded.structural_validity.status == "pass", "the counts add up, which is a different question"


# -- the two kinds of work the development set covers ----------------------------------


async def test_a_run_handed_nothing_to_do_passes_only_the_case_that_permits_no_work(tmp_path: Path) -> None:
    """One run, two cases. Doing nothing is right for the case that says so and wrong for every other."""
    journey = Journey(tmp_path)

    record = await journey.ran(SWEEPING, _sweeping_arguments(()))

    assert journey.swept == [], "there was nothing to sweep"
    assert record.result is not None
    assert record.result.status == "noop"
    assert record.result.attempted == 0

    permitted = _graded(record, "wa-dev-24-documentation-analysis-nothing-to-fold")
    assert permitted.task_success.status == "pass"
    assert "permits no work" in permitted.task_success.reason

    asked_for_work = _graded(record, "wa-dev-22-documentation-analysis")
    assert asked_for_work.task_success.status == "fail"
    assert "did no work" in asked_for_work.task_success.reason


async def test_both_kinds_of_work_are_graded_from_the_same_run_facts(journey: Journey) -> None:
    """The kind of work a case names changes nothing about how a run is read, and never reaches one."""
    record = await journey.ran(FILING, GOOD)
    dataset = load_stamped_dataset(DATASET, STAMP)

    assert dataset.task_families, "the set declares no kinds of work, so there is nothing to grade across"
    for family in dataset.task_families:
        named = [case for case in dataset.cases if case.task_family == family]
        assert named, f"{family} names no case"
        for case in named:
            assert set(case.execution_input.model_dump()) == {"case_id", "input"}
            assert family not in case.execution_input.input, "the kind of work reached a run as part of the request"

    analysis = _graded(record, "wa-dev-22-documentation-analysis", intent=ASKED_FOR)
    brief = _graded(record, "wa-dev-25-decision-brief-writes-nothing", intent=ASKED_FOR)
    assert {_case(analysis.task_id).task_family, _case(brief.task_id).task_family} == set(dataset.task_families)

    assert analysis.task_success == brief.task_success, "one run, one reading of what it did"
    assert analysis.evidence_support == brief.evidence_support
    assert (analysis.task_id, brief.task_id) == ("wa-dev-22-documentation-analysis", "wa-dev-25-decision-brief-writes-nothing")


# -- 7. evidence that moved on, or was never there -------------------------------------


async def test_a_reference_the_run_no_longer_holds_is_refused_and_records_no_review(journey: Journey) -> None:
    """A digest that has moved on cannot be reviewed, and the refusal leaves the record as it was."""
    record = await journey.ran(FILING, GOOD)
    held = _ref(record, "note_draft")
    moved = held.model_copy(update={"value_digest": "f" * 64})

    async with a_client_of(journey.running()) as client:
        resolved = await client.post(
            f"/api/runs/history/{record.run_id}/artifacts/resolve", json={"ref": moved.model_dump(mode="json")}
        )
        refused = await client.post(
            f"/api/runs/history/{record.run_id}/review",
            json={
                "review_id": "review-over-a-value-that-moved",
                "actor": "the owner",
                "outcome": "accepted",
                "reason": "it read well when I saw it",
                "artifact_refs": [moved.model_dump(mode="json")],
            },
        )
        missing = await client.post(
            f"/api/runs/history/{record.run_id}/artifacts/resolve",
            json={"ref": held.model_copy(update={"id": "no-such-artifact"}).model_dump(mode="json")},
        )
        history = await client.get(f"/api/runs/history/{record.run_id}")

    assert resolved.status_code == 409, resolved.text
    assert "digest" in resolved.json()["message"]
    assert refused.status_code == 409, refused.text
    assert missing.status_code == 404, missing.text
    assert history.json()["owner_reviews"] == [], "a refused review left nothing behind"


async def test_the_owner_review_names_the_exact_value_it_read(journey: Journey) -> None:
    """What the person accepted is the reference the run fixed, and it stays on the record as that."""
    record = await journey.ran(FILING, GOOD)
    held = _ref(record, "page")

    async with a_client_of(journey.running()) as client:
        recorded = await client.post(
            f"/api/runs/history/{record.run_id}/review",
            json={
                "review_id": "review-1",
                "actor": "the owner",
                "outcome": "accepted",
                "reason": "both questions are on the page, in the words the review used",
                "artifact_refs": [held.model_dump(mode="json")],
            },
        )
        history = await client.get(f"/api/runs/history/{record.run_id}")

    assert recorded.status_code == 200, recorded.text
    assert recorded.json()["created"] is True
    reviews = history.json()["owner_reviews"]
    assert [review["outcome"] for review in reviews] == ["accepted"]
    assert reviews[0]["artifact_refs"][0]["value_digest"] == held.value_digest, "the exact bytes, not a newer spelling of them"


# -- 8. a run nobody has said anything about -------------------------------------------


async def test_acceptance_is_unknown_until_a_review_exists_and_is_that_review_afterwards(journey: Journey) -> None:
    """Finishing is not being wanted. Acceptance is read off the host's own reviews, never off the run."""
    record = await journey.ran(FILING, GOOD)
    held = _ref(record, "page")
    graded = _graded(record, "wa-dev-04-success", intent=ASKED_FOR)
    assert graded.task_success.status == "pass", "the run did the work by its own facts"

    async with a_client_of(journey.running()) as client:
        before = await client.get(f"/api/runs/history/{record.run_id}")
        unreviewed = _beside_reviews(graded, _reviews_in(before.json()))

        await client.post(
            f"/api/runs/history/{record.run_id}/review",
            json={
                "review_id": "review-1",
                "actor": "the owner",
                "outcome": "rejected",
                "reason": "it filed the questions and never said which review they came from",
                "artifact_refs": [held.model_dump(mode="json")],
            },
        )
        after = await client.get(f"/api/runs/history/{record.run_id}")
        reviewed = _beside_reviews(graded, _reviews_in(after.json()))

    assert unreviewed.owner_acceptance.status == "unknown", "nobody had said whether it was any use"
    assert unreviewed.reviews == ()
    assert reviewed.owner_acceptance.status == "fail", "and then somebody said it was not"
    assert reviewed.owner_acceptance.reason == "it filed the questions and never said which review they came from"
    assert reviewed.outcome.task_success.status == "pass", "what the run did is untouched by what anybody made of it"

    with pytest.raises(ValueError, match="owner acceptance without any review must stay unknown"):
        ReviewedTaskOutcome(outcome=graded, owner_acceptance=Assessment(status="pass", reason="it looked fine"))


# -- 9. keeping what the run worked out, as a note -------------------------------------


async def test_the_note_a_run_proposed_is_readable_and_offered_for_a_person_to_keep(journey: Journey) -> None:
    """The run leaves the note as a value to read and an action to press, never as a page it wrote.

    That nothing was written anywhere is the other half, and it is asserted where a destination
    exists to write into: `studio-client/e2e/automation-journey.spec.ts`.
    """
    record = await journey.ran(FILING, GOOD)
    proposed = _ref(record, "note_draft")

    async with a_client_of(journey.running()) as client:
        resolved = await client.post(
            f"/api/runs/history/{record.run_id}/artifacts/resolve", json={"ref": proposed.model_dump(mode="json")}
        )

    assert resolved.status_code == 200, resolved.text
    note = ProposedNote.model_validate(resolved.json()["value"])
    assert note.page_type == "question", "the destination's own page type, as the run proposed it"
    assert note.title == TOPIC
    assert note.sections == ("What was asked", "What settles it")
    assert resolved.json()["ref"] == proposed.model_dump(mode="json"), "the answer is about the reference that was asked for"

    card = await journey.library.fetch_card(FILING)
    assert card is not None
    assert card.presentation is not None
    offered = [action for action in card.presentation.actions if action.kind == "save_note"]
    assert [action.artifact_id for action in offered] == ["note_draft"], "keeping it is offered over that value"
    assert [action.destination for action in offered] == ["user_selects"], "and the person chooses where it goes"
    assert record.result is not None
    assert record.result.status == "succeeded", "the run is complete whether or not anybody keeps the note"


# -- 10. running it again --------------------------------------------------------------


async def test_a_rerun_is_a_second_deliberate_start_and_each_record_keeps_its_own_values(journey: Journey) -> None:
    """Two records, each holding the values its own run was given, and neither rewritten by the other."""
    first = await journey.ran(FILING, GOOD)
    changed = _filing_arguments(TOPIC, ("who reads the flags",))

    second = await journey.ran(FILING, changed)

    assert first.run_id != second.run_id
    assert journey.records.archive.get(first.run_id) is not None
    kept = journey.records.archive.get(first.run_id)
    assert kept is not None
    assert kept.params == GOOD, "the first run's own values, untouched by the second"
    assert second.params == changed
    assert [filed.page for filed in journey.filed] == [
        f"# {TOPIC}\n\n{' / '.join(QUESTIONS)} (draft 1)\n",
        f"# {TOPIC}\n\nwho reads the flags (draft 2)\n",
    ]


# -- 11. the next version, out of the run that disappointed ----------------------------


async def test_an_adaptation_cites_the_run_behind_it_and_leaves_the_parent_alone(journey: Journey) -> None:
    """The child names the run and the owner's own reason, and the workflow it came from is unchanged."""
    record = await journey.ran(FILING, GOOD)
    created = await journey.pool.create(None, session_kind="runner")
    session_id = created.session_id or ""
    authoring = a_controller(journey.pool, state=journey.home, archive=journey.records.archive)

    async with a_client_of(authoring) as client:
        adapted = await client.post(
            f"/api/sessions/{session_id}/adapt",
            json={
                "workflow_id": FILING,
                "ops": [{"op": "set_description", "to": "File each open question, and say which review it came from."}],
                "source_run_id": record.run_id,
                "feedback": "it never said which review a question came from",
            },
        )

    assert adapted.status_code == 200, adapted.text
    lineage = adapted.json()["lineage"]
    assert lineage["parent_name"] == "file-questions"
    assert lineage["parent_version"] == 1
    assert record.run_id in lineage["evidence"], "the run behind the change is named"
    assert "it never said which review a question came from" in lineage["evidence"], "and the owner's own reason"

    child = yaml.safe_load(created.conversation.draft.spec_yaml)
    assert child["version"] == 2
    parent = await journey.library.fetch_workflow(FILING)
    assert parent.version == 1, "the parent is never written back"
    assert parent.description == _filing_spec().description
    still_there = journey.records.archive.get(record.run_id)
    assert still_there is not None
    assert still_there.result == record.result, "the run the child cites is left exactly as it was"


# -- the labels a case holds never cross into a run ------------------------------------


def test_nothing_a_run_is_handed_carries_the_grading_criteria() -> None:
    """Execution sees a case id and the request. The criterion, the expected status and the notes stay behind."""
    dataset = load_stamped_dataset(DATASET, STAMP)

    given = dataset.execution_inputs

    assert {field for row in given for field in row.model_dump()} == {"case_id", "input"}
    criteria = {case.labels.success_criterion for case in dataset.cases}
    assert all(row.input not in criteria for row in given), "a criterion reached execution as a request"


async def test_a_case_whose_journey_never_ran_is_a_failed_trial_rather_than_an_empty_one(journey: Journey) -> None:
    """A slot with no run behind it fails. Nothing scores a journey that did not happen."""
    runner = WorkflowAutomationTaskRunner(FakePilotRuns(), FakeRunArchive())

    with pytest.raises(EvaluationError, match="no pilot trial is recorded"):
        await runner(EvaluationInput(case_id="wa-dev-04-success", input=TOPIC), _IDENTITY, 0)

    record = await journey.ran(FILING, GOOD)
    filed = WorkflowAutomationTaskRunner(
        FakePilotRuns([PilotTrial(case_id="wa-dev-04-success", repetition=0, run_id=record.run_id, trial_id="trial-1")]),
        FakeRunArchive([record]),
    )

    execution = await filed(EvaluationInput(case_id="wa-dev-04-success", input=TOPIC), _IDENTITY, 0)

    assert execution.run_id == record.run_id
    assert execution.result is not None
    assert execution.result.status == "succeeded"
    assert execution.output == execution.result.summary
