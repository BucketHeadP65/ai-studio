"""The spec for grading an answer from the evidence a trial retained.

These tests pin what the grader guarantees on its own, independently of how well any grading model
reasons: which dimensions a criterion asks for, how a verdict folds, what happens when the retained
record cannot be read back, and what the grading model is allowed to see.

A real `FileDeliveredEvidenceStore` over `tmp_path` stands in for the reader everywhere, the same
adapter a live guarded trial writes through, so a refused read here is the refusal a reader would
actually raise. The grading model is a `FunctionModel` scripted to return one verdict, which keeps
the model's own judgment out of tests about the machinery around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import pytest

from core.ai.evaluation.contracts import Assessment, DeliveredEvidenceRef, DeliveredSource, FileDeliveredEvidenceStore, content_fingerprint
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, ToolCallPart, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from engram.evals.outcomes.domain.answers import (
    ANSWER_DIMENSIONS,
    AnswerAssessment,
    AnswerCriterion,
    AnswerDimension,
    AnswerGradeDeps,
    AssessmentStatus,
    ClaimAssessment,
    EvidenceRequirement,
)
from engram.evals.outcomes.infrastructure.answer_grader import (
    ANSWER_GRADER_INSTRUCTIONS,
    PydanticAnswerGrader,
    answer_grade_request,
    build_answer_grader_agent,
    claim_key,
)
from tests.support.fakes.outcome_grader import FakeAnswerGrader, assessment_at, claim_at

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic_ai.messages import ModelMessage

_TRIAL = "case.hybrid-retrieval:0:baseline"
_PAPER = "page:research/paper.hybrid"
_COUNTER = "page:research/paper.coldstart"


# -- the record a trial retained --------------------------------------------


@dataclass
class _Record:
    """One trial's stored evidence: the store that holds it and the references it filed."""

    store: FileDeliveredEvidenceStore
    refs: list[DeliveredEvidenceRef] = field(default_factory=list)

    async def deliver(self, payload: str, *sources: DeliveredSource) -> DeliveredEvidenceRef:
        """File one more payload for this trial and return the reference naming it."""
        ref = await self.store.append(_TRIAL, len(self.refs), payload, sources=tuple(sources))
        self.refs.append(ref)
        return ref

    @property
    def evidence(self) -> tuple[DeliveredEvidenceRef, ...]:
        """Every reference this trial retained, in ordinal order."""
        return tuple(self.refs)

    def payload_path(self, ref: DeliveredEvidenceRef) -> Path:
        """Where the bytes `ref` names actually sit under the state root."""
        return next((self.store.state_root / "delivered-evidence").rglob(f"{ref.payload_sha256}.json"))


def _record(tmp_path: Path) -> _Record:
    """An empty record over a real file-backed evidence store."""
    return _Record(store=FileDeliveredEvidenceStore(tmp_path))


def _source(ref: str, *, version: str | None = None, digest: str | None = None) -> DeliveredSource:
    """One delivered source, with as much integrity as the result itself carried."""
    return DeliveredSource(source_ref=ref, source_version=version, source_content_sha256=digest)


# -- the grading model -------------------------------------------------------


@dataclass
class _Grading:
    """What the grading model was asked, and what it was scripted to answer.

    Attributes:
        verdict: The verdict returned as the output tool's arguments.
        text_turns: How many plain-text (invalid-structure) turns to emit before the verdict.
        fail_on_turn: The turn index that raises `TimeoutError` instead of answering.
        requests: Every request the model received, in order.
        tools: The function tools offered on each request.
    """

    verdict: AnswerAssessment | None = None
    text_turns: int = 0
    fail_on_turn: int | None = None
    requests: list[Sequence[ModelMessage]] = field(default_factory=list)
    tools: list[tuple[str, ...]] = field(default_factory=list)

    def model(self) -> FunctionModel:
        """The `FunctionModel` running this script."""

        def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            turn = len(self.requests)
            self.requests.append(tuple(messages))
            self.tools.append(tuple(tool.name for tool in info.function_tools))
            if self.fail_on_turn == turn:
                msg = "the grading model ran out of time"
                raise TimeoutError(msg)
            if turn < self.text_turns or self.verdict is None:
                return ModelResponse(parts=[TextPart("I think it is probably fine.")])
            return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, self.verdict.model_dump(mode="json"))])

        return FunctionModel(respond)

    @property
    def prompt(self) -> str:
        """The user-prompt text of the first request, empty when no request was made."""
        if not self.requests:
            return ""
        return "\n".join(
            part.content
            for message in self.requests[0]
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, UserPromptPart) and isinstance(part.content, str)
        )


def _grader(grading: _Grading, record: _Record) -> PydanticAnswerGrader:
    """The grader under test: the scripted model, reading back the trial's real record."""
    return PydanticAnswerGrader(agent=build_answer_grader_agent(grading.model()), reader=record.store)


# -- criteria ----------------------------------------------------------------


def _answerable(*requirements: EvidenceRequirement, conditions: tuple[str, ...] = ()) -> AnswerCriterion:
    return AnswerCriterion(
        answerability="answerable",
        required_conclusions=("hybrid retrieval beats keyword search",),
        forbidden_conclusions=("keyword search beats hybrid retrieval",),
        required_conditions=conditions,
        evidence=requirements,
    )


def _unsupported() -> AnswerCriterion:
    return AnswerCriterion(
        answerability="unsupported", abstention_requirement="say the knowledge base holds no evidence either way, and why"
    )


_SUPPORT = EvidenceRequirement(source_ref=_PAPER, role="support", claim="hybrid retrieval beats keyword search")
_COUNTEREVIDENCE = EvidenceRequirement(source_ref=_COUNTER, role="counterevidence", claim="the hybrid index degrades on cold start")


class TestRequiredDimensions:
    """Which dimensions a criterion asks for is fixed by the criterion, never by the answer."""

    def test_an_answerable_criterion_asks_for_correctness_and_support(self):
        assert _answerable().required_dimensions == frozenset({"correctness", "support"})

    def test_required_conditions_add_coverage(self):
        assert "coverage" in _answerable(conditions=("only over 10k pages",)).required_dimensions

    def test_a_counterevidence_requirement_adds_contradiction_handling(self):
        assert "contradiction_handling" in _answerable(_SUPPORT, _COUNTEREVIDENCE).required_dimensions

    def test_a_support_only_criterion_does_not_ask_for_contradiction_handling(self):
        assert "contradiction_handling" not in _answerable(_SUPPORT).required_dimensions

    def test_an_unsupported_criterion_asks_for_abstention_and_not_correctness(self):
        asked = _unsupported().required_dimensions
        assert asked == frozenset({"support", "abstention"})

    def test_an_abstention_requirement_asks_for_abstention_on_an_ambiguous_question(self):
        criterion = AnswerCriterion(answerability="ambiguous", abstention_requirement="name the reading you cannot pick")
        assert "abstention" in criterion.required_dimensions

    def test_support_is_asked_for_by_every_criterion(self):
        for criterion in (_answerable(), _unsupported(), AnswerCriterion(answerability="ambiguous")):
            assert "support" in criterion.required_dimensions


# -- the criterion-folding table ---------------------------------------------


@dataclass(frozen=True)
class _Row:
    """One named grading scenario and the verdict it must reach."""

    criterion: AnswerCriterion
    answer: str
    graded: dict[AnswerDimension, AssessmentStatus]
    expected: dict[AnswerDimension, AssessmentStatus]
    overall: AssessmentStatus
    claim_status: AssessmentStatus = "pass"


def _verdict(row: _Row, ref: DeliveredEvidenceRef) -> AnswerAssessment:
    """The verdict the scripted model returns for `row`, citing the trial's own evidence."""
    graded = {
        name: Assessment(status=row.graded.get(name, "pass"), reason=f"scripted {row.graded.get(name, 'pass')}")
        for name in ANSWER_DIMENSIONS
    }
    return AnswerAssessment(
        overall=Assessment(status="pass", reason="the model's own fold, which the grader recomputes"),
        correctness=graded["correctness"],
        support=graded["support"],
        coverage=graded["coverage"],
        contradiction_handling=graded["contradiction_handling"],
        abstention=graded["abstention"],
        claims=(claim_at(_SUPPORT.claim, row.claim_status, (ref,)),) if row.criterion.evidence else (),
    )


_ROWS: dict[str, _Row] = {
    "right citations and opposite conclusion": _Row(
        criterion=_answerable(_SUPPORT),
        answer="Keyword search beats hybrid retrieval.",
        graded={"correctness": "fail", "support": "fail"},
        expected={"correctness": "fail", "support": "fail", "coverage": "not_applicable"},
        overall="fail",
        claim_status="fail",
    ),
    "counterevidence delivered and ignored": _Row(
        criterion=_answerable(_SUPPORT, _COUNTEREVIDENCE),
        answer="Adopt the hybrid index everywhere.",
        graded={"contradiction_handling": "fail"},
        expected={"correctness": "pass", "support": "pass", "contradiction_handling": "fail"},
        overall="fail",
    ),
    "conditional evidence and unconditional answer": _Row(
        criterion=_answerable(_SUPPORT, conditions=("only for corpora over 10k pages",)),
        answer="Hybrid retrieval beats keyword search.",
        graded={"coverage": "fail"},
        expected={"coverage": "fail", "correctness": "pass", "contradiction_handling": "not_applicable"},
        overall="fail",
    ),
    "unsupported knowledge base and reasoned abstention": _Row(
        criterion=_unsupported(),
        answer="The knowledge base holds nothing on this, so I cannot recommend either way.",
        graded={},
        expected={"abstention": "pass", "support": "pass", "correctness": "not_applicable"},
        overall="pass",
    ),
    "unsupported knowledge base and recommendation": _Row(
        criterion=_unsupported(),
        answer="Adopt the hybrid index.",
        graded={"abstention": "fail", "support": "fail"},
        expected={"abstention": "fail", "support": "fail", "correctness": "not_applicable"},
        overall="fail",
    ),
}


class TestCriterionFolding:
    """Every dimension the criterion asks for is graded, every other one is `not_applicable`."""

    @pytest.mark.parametrize("name", list(_ROWS))
    async def test_row(self, name: str, tmp_path: Path):
        row = _ROWS[name]
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))
        grading = _Grading(verdict=_verdict(row, ref))

        assessment = await _grader(grading, record)(row.criterion, row.answer, record.evidence)

        assert assessment.overall.status == row.overall
        for dimension, status in row.expected.items():
            assert assessment.dimension(dimension).status == status, dimension

    @pytest.mark.parametrize("name", list(_ROWS))
    async def test_every_unasked_dimension_is_not_applicable(self, name: str, tmp_path: Path):
        row = _ROWS[name]
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))

        assessment = await _grader(_Grading(verdict=_verdict(row, ref)), record)(row.criterion, row.answer, record.evidence)

        unasked = set(ANSWER_DIMENSIONS) - row.criterion.required_dimensions
        assert {name for name in unasked if assessment.dimension(name).status != "not_applicable"} == set()

    async def test_the_model_s_own_overall_is_recomputed_rather_than_trusted(self, tmp_path: Path):
        row = _ROWS["right citations and opposite conclusion"]
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))

        assessment = await _grader(_Grading(verdict=_verdict(row, ref)), record)(row.criterion, row.answer, record.evidence)

        assert assessment.overall.status == "fail"
        assert "correctness failed" in assessment.overall.reason

    async def test_a_skipped_asked_dimension_becomes_unknown(self, tmp_path: Path):
        record = _record(tmp_path)
        await record.deliver('{"hits": []}', _source(_PAPER))
        criterion = _answerable()
        verdict = assessment_at(criterion, "pass").model_copy(
            update={"correctness": Assessment(status="not_applicable", reason="the model declined to grade it")}
        )

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.correctness.status == "unknown"
        assert assessment.overall.status == "unknown"

    async def test_a_critical_claim_that_failed_fails_the_answer(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "fail", (ref,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.overall.status == "fail"
        assert f"critical claim {_SUPPORT.claim!r} failed" in assessment.overall.reason


# -- an unreadable record ----------------------------------------------------


class TestUnreadableEvidence:
    """A record that cannot be read back exactly is graded by nobody: unknown, and no request."""

    @pytest.mark.parametrize("damage", ["missing evidence file", "hash mismatch"])
    async def test_the_criterion_s_dimensions_go_unknown_without_a_model_request(self, damage: str, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": ["paper.hybrid"]}', _source(_PAPER))
        path = record.payload_path(ref)
        if damage == "missing evidence file":
            path.unlink()
        else:
            path.write_bytes(b"z" * ref.payload_bytes)
        criterion = _answerable(_SUPPORT)
        grading = _Grading(verdict=assessment_at(criterion, "pass"))

        assessment = await _grader(grading, record)(criterion, "Hybrid wins.", record.evidence)

        assert grading.requests == []
        assert assessment.overall.status == "unknown"
        assert {name: assessment.dimension(name).status for name in criterion.required_dimensions} == {
            "correctness": "unknown",
            "support": "unknown",
        }

    async def test_a_dimension_the_criterion_does_not_ask_for_stays_not_applicable(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))
        record.payload_path(ref).unlink()
        criterion = _answerable(_SUPPORT)

        assessment = await _grader(_Grading(), record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.abstention.status == "not_applicable"
        assert assessment.claims == ()

    async def test_the_reason_names_what_the_reader_refused(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))
        record.payload_path(ref).unlink()

        assessment = await _grader(_Grading(), record)(_answerable(), "Hybrid wins.", record.evidence)

        assert "no stored payload" in assessment.overall.reason


# -- source integrity --------------------------------------------------------


_PINNED = EvidenceRequirement(
    source_ref=_PAPER, role="support", claim="hybrid retrieval beats keyword search", source_version="2025-06-02"
)


class TestSourceIntegrity:
    """A claim pinned to a source version is graded only when the record observed that version."""

    async def test_a_missing_hash_and_version_makes_the_pinned_claim_unknown(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))
        criterion = _answerable(_PINNED)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_PINNED.claim, "pass", (ref,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [claim.support.status for claim in assessment.claims] == ["unknown"]
        assert "2025-06-02" in assessment.claims[0].support.reason
        assert assessment.overall.status == "unknown"

    async def test_a_matching_version_with_a_content_hash_leaves_the_claim_graded(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver(
            '{"hits": []}', _source(_PAPER, version="2025-06-02", digest=content_fingerprint("the paper as it stood"))
        )
        criterion = _answerable(_PINNED)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_PINNED.claim, "pass", (ref,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [claim.support.status for claim in assessment.claims] == ["pass"]
        assert assessment.overall.status == "pass"

    async def test_a_version_that_differs_makes_the_claim_unknown(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER, version="2024-01-01", digest=content_fingerprint("older")))
        criterion = _answerable(_PINNED)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_PINNED.claim, "pass", (ref,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [claim.support.status for claim in assessment.claims] == ["unknown"]

    async def test_an_unpinned_requirement_is_graded_from_the_payload_alone(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "pass", (ref,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [claim.support.status for claim in assessment.claims] == ["pass"]
        assert assessment.overall.status == "pass"

    async def test_a_pinned_requirement_the_model_never_claimed_is_recorded_unknown(self, tmp_path: Path):
        record = _record(tmp_path)
        await record.deliver('{"hits": []}', _source(_PAPER))
        criterion = _answerable(_PINNED)

        assessment = await _grader(_Grading(verdict=assessment_at(criterion, "pass")), record)(
            criterion, "Hybrid wins.", record.evidence
        )

        assert [(claim.claim, claim.support.status) for claim in assessment.claims] == [(_PINNED.claim, "unknown")]
        assert assessment.overall.status == "unknown"


# -- the retained payload, not the source as it stands now --------------------


class TestRetainedPayload:
    """A source that changed after the trial cannot reach the grader, because nothing rereads it."""

    async def test_the_request_carries_the_retained_payload_and_not_the_later_one(self, tmp_path: Path):
        record = _record(tmp_path)
        delivered = await record.deliver('{"finding": "the cache warms in 40 ms"}', _source(_PAPER))
        # The source changed after the trial: a later read of it was filed, and nothing hands it over.
        await record.deliver('{"finding": "the cache warms in 900 ms"}', _source(_PAPER))
        criterion = _answerable(_SUPPORT)
        grading = _Grading(verdict=assessment_at(criterion, "pass"))

        await _grader(grading, record)(criterion, "The cache warms fast.", (delivered,))

        assert "40 ms" in grading.prompt
        assert "900 ms" not in grading.prompt

    async def test_the_grading_agent_is_offered_no_tools_at_all(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"finding": "the cache warms in 40 ms"}', _source(_PAPER))
        grading = _Grading(verdict=assessment_at(_answerable(), "pass"))

        await _grader(grading, record)(_answerable(), "The cache warms fast.", (ref,))

        assert grading.tools == [()]

    async def test_payloads_reach_the_request_in_ordinal_order(self, tmp_path: Path):
        record = _record(tmp_path)
        first = await record.deliver('{"finding": "alpha"}', _source(_PAPER))
        second = await record.deliver('{"finding": "beta"}', _source(_COUNTER))
        grading = _Grading(verdict=assessment_at(_answerable(), "pass"))

        await _grader(grading, record)(_answerable(), "Both.", (second, first))

        assert grading.prompt.index("alpha") < grading.prompt.index("beta")


# -- claims must rest on this trial's own record ------------------------------


class TestClaimEvidenceBinding:
    """A claim is only as good as the retained payload it actually rests on."""

    async def test_a_claim_citing_a_reference_outside_the_trial_is_unknown(self, tmp_path: Path):
        record = _record(tmp_path)
        await record.deliver('{"nodes": ["paper.hybrid"]}', _source(_PAPER))
        invented = DeliveredEvidenceRef(
            evidence_id=f"{content_fingerprint('another trial')}:0",
            ordinal=0,
            payload_sha256=content_fingerprint("never stored"),
            payload_bytes=12,
            sources=(_source(_PAPER),),
        )
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "pass", (invented,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.claims[0].support.status == "unknown"
        assert "did not retain" in assessment.claims[0].support.reason
        assert assessment.overall.status == "unknown"

    async def test_a_claim_citing_the_trial_s_own_graph_evidence_stays_graded(self, tmp_path: Path):
        record = _record(tmp_path)
        graph = await record.deliver('{"nodes": ["paper.hybrid"], "edges": []}', _source(_PAPER))
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "pass", (graph,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.claims[0].support.status == "pass"
        assert assessment.overall.status == "pass"

    async def test_the_same_bytes_under_a_different_hash_do_not_count_as_retained(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"nodes": []}', _source(_PAPER))
        relabelled = ref.model_copy(update={"payload_sha256": content_fingerprint("a hash nobody filed")})
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "pass", (relabelled,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.claims[0].support.status == "unknown"

    async def test_asserted_support_needs_evidence_carrying_the_requirement_s_own_source(self, tmp_path: Path):
        record = _record(tmp_path)
        elsewhere = await record.deliver('{"nodes": ["paper.coldstart"]}', _source(_COUNTER))
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "pass", (elsewhere,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.claims[0].support.status == "unknown"
        assert _PAPER in assessment.claims[0].support.reason

    async def test_a_rejected_claim_needs_no_citation_to_stay_rejected(self, tmp_path: Path):
        # Demanding evidence for a failure would erase the finding that the answer was unsupported.
        record = _record(tmp_path)
        await record.deliver('{"nodes": []}', _source(_COUNTER))
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "fail"),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.claims[0].support.status == "fail"
        assert assessment.overall.status == "fail"

    async def test_a_claim_answering_no_requirement_needs_no_source_match(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"nodes": []}', _source(_COUNTER))
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at("an aside the criterion never asked about", "pass", (ref,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.claims[0].support.status == "pass"


# -- a grading model that cannot answer --------------------------------------


class TestGraderFailure:
    """A grader that times out or breaks its own output contract is unknown, not a crash."""

    @pytest.mark.parametrize("failure", ["grader timeout", "invalid structure"])
    async def test_the_answer_is_unknown_and_the_usage_is_kept(self, failure: str, tmp_path: Path):
        record = _record(tmp_path)
        await record.deliver('{"hits": []}', _source(_PAPER))
        criterion = _answerable(_SUPPORT)
        grading = _Grading(text_turns=1, fail_on_turn=1) if failure == "grader timeout" else _Grading(verdict=None)

        assessment = await _grader(grading, record)(criterion, "Hybrid wins.", record.evidence)

        assert assessment.overall.status == "unknown"
        assert {assessment.dimension(name).status for name in criterion.required_dimensions} == {"unknown"}
        assert assessment.usage.requests >= 1
        assert assessment.usage.input_tokens > 0

    async def test_a_successful_grading_also_reports_what_it_spent(self, tmp_path: Path):
        record = _record(tmp_path)
        await record.deliver('{"hits": []}', _source(_PAPER))
        criterion = _answerable()

        assessment = await _grader(_Grading(verdict=assessment_at(criterion, "pass")), record)(
            criterion, "Hybrid wins.", record.evidence
        )

        assert assessment.usage.requests == 1
        assert assessment.usage.cost_usd is None

    async def test_an_unreadable_record_spends_nothing(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))
        record.payload_path(ref).unlink()

        assessment = await _grader(_Grading(), record)(_answerable(), "Hybrid wins.", record.evidence)

        assert assessment.usage.requests == 0


# -- what the grading model is allowed to see ---------------------------------


class TestGraderTransport:
    """The request carries the criterion, the answer and the exact payloads, and nothing else."""

    async def test_the_request_carries_the_criterion_the_answer_and_the_exact_payloads(self, tmp_path: Path):
        record = _record(tmp_path)
        payload = '{"hits": [{"id": "paper.hybrid", "snippet": "hybrid beats keyword on recall"}]}'
        ref = await record.deliver(payload, _source(_PAPER))
        criterion = _answerable(_SUPPORT)
        grading = _Grading(verdict=assessment_at(criterion, "pass"))

        await _grader(grading, record)(criterion, "Hybrid retrieval beats keyword search.", (ref,))

        assert len(grading.requests) == 1
        assert payload in grading.prompt
        assert "Hybrid retrieval beats keyword search." in grading.prompt
        assert "hybrid retrieval beats keyword search" in grading.prompt
        assert ref.evidence_id in grading.prompt

    async def test_the_request_names_no_variant_and_no_discovery_score(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))
        criterion = _answerable(_SUPPORT)
        grading = _Grading(verdict=assessment_at(criterion, "pass"))

        await _grader(grading, record)(criterion, "Hybrid wins.", (ref,))

        whole = grading.prompt + ANSWER_GRADER_INSTRUCTIONS
        for forbidden in ("baseline", "candidate", "recall@", "discovery"):
            assert forbidden not in whole

    async def test_the_grader_receives_no_function_tools(self, tmp_path: Path):
        record = _record(tmp_path)
        ref = await record.deliver('{"hits": []}', _source(_PAPER))
        grading = _Grading(verdict=assessment_at(_answerable(), "pass"))

        await _grader(grading, record)(_answerable(), "Hybrid wins.", (ref,))

        assert grading.tools == [()]

    def test_the_request_text_is_built_from_the_deps_alone(self):
        deps = AnswerGradeDeps(criterion=_answerable(_SUPPORT), answer="Hybrid wins.", evidence=())

        request = answer_grade_request(deps)

        assert "Hybrid wins." in request
        assert "This trial retained no delivered evidence." in request

    def test_the_grade_deps_carry_only_the_criterion_the_answer_and_the_evidence(self):
        assert set(AnswerGradeDeps.model_fields) == {"criterion", "answer", "evidence"}


# -- the fake a caller drives --------------------------------------------------


class TestFakeAnswerGrader:
    """The shared fake records what it was asked and answers from public state."""

    async def test_it_records_every_call_and_passes_by_default(self):
        fake = FakeAnswerGrader()
        criterion = _answerable(_SUPPORT)

        assessment = await fake(criterion, "Hybrid wins.", ())

        assert [call.answer for call in fake.calls] == ["Hybrid wins."]
        assert assessment.overall.status == "pass"

    async def test_a_scripted_verdict_is_answered_verbatim(self):
        criterion = _unsupported()
        scripted = assessment_at(criterion, "fail")
        fake = FakeAnswerGrader(assessment=scripted)

        assert await fake(criterion, "Adopt it.", ()) is scripted

    async def test_a_scripted_error_is_raised(self):
        fake = FakeAnswerGrader(error=TimeoutError("the grader stalled"))

        with pytest.raises(TimeoutError, match="stalled"):
            await fake(_answerable(), "Hybrid wins.", ())

    def test_claim_at_builds_a_claim_assessment(self):
        claim = claim_at(_SUPPORT.claim, "pass", ())

        assert isinstance(claim, ClaimAssessment)
        assert claim.critical is True


# -- one claim assessment per requirement -------------------------------------


_PAPER_ONLY = "the payload carrying only the supporting source"
_COUNTER_ONLY = "the payload carrying only the counterevidence source"


class TestRequirementBinding:
    """A requirement is answered by a claim, or it is unknown. Prose wording never decides it."""

    def test_the_claim_key_folds_whitespace_and_case(self):
        assert claim_key("  Hybrid   Retrieval\nBeats Keyword Search ") == claim_key("hybrid retrieval beats keyword search")

    async def test_a_verbatim_claim_binds_and_is_held_to_the_requirement_s_source(self, tmp_path: Path):
        record = _record(tmp_path)
        elsewhere = await record.deliver(f'{{"note": "{_COUNTER_ONLY}"}}', _source(_COUNTER))
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "pass", (elsewhere,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [claim.support.status for claim in assessment.claims] == ["unknown"]
        assert assessment.overall.status == "unknown"

    @pytest.mark.parametrize("wording", ["Hybrid Retrieval Beats Keyword Search", "hybrid retrieval  beats\n keyword search "])
    async def test_spacing_and_capitalization_alone_never_unbind_a_requirement(self, wording: str, tmp_path: Path):
        record = _record(tmp_path)
        elsewhere = await record.deliver(f'{{"note": "{_COUNTER_ONLY}"}}', _source(_COUNTER))
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(wording, "pass", (elsewhere,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [claim.support.status for claim in assessment.claims] == ["unknown"]
        assert assessment.overall.status == "unknown"

    async def test_a_paraphrase_leaves_the_requirement_unanswered_rather_than_satisfied(self, tmp_path: Path):
        # A reworded claim answers no requirement, so it is graded on its own and the requirement
        # stays unanswered rather than being counted as met by prose that resembles it.
        record = _record(tmp_path)
        elsewhere = await record.deliver(f'{{"note": "{_COUNTER_ONLY}"}}', _source(_COUNTER))
        criterion = _answerable(_SUPPORT)
        paraphrase = "Hybrid retrieval outperforms keyword search."
        verdict = assessment_at(criterion, "pass", claims=(claim_at(paraphrase, "pass", (elsewhere,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [(claim.claim, claim.support.status) for claim in assessment.claims] == [
            (paraphrase, "pass"),
            (_SUPPORT.claim, "unknown"),
        ]
        assert assessment.overall.status == "unknown"

    async def test_a_required_requirement_the_grader_never_answered_is_unknown_and_critical(self, tmp_path: Path):
        record = _record(tmp_path)
        await record.deliver(f'{{"note": "{_PAPER_ONLY}"}}', _source(_PAPER))
        criterion = _answerable(_SUPPORT)

        assessment = await _grader(_Grading(verdict=assessment_at(criterion, "pass")), record)(
            criterion, "Hybrid wins.", record.evidence
        )

        assert [(claim.claim, claim.support.status, claim.critical) for claim in assessment.claims] == [
            (_SUPPORT.claim, "unknown", True)
        ]
        assert _SUPPORT.source_ref in assessment.claims[0].support.reason
        assert assessment.overall.status == "unknown"

    async def test_an_unanswered_requirement_that_does_not_decide_the_case_is_recorded_without_moving_the_fold(
        self, tmp_path: Path
    ):
        # It has to be visible in the verdict, and it must not turn a passing answer into an unknown one.
        record = _record(tmp_path)
        supporting = await record.deliver(f'{{"note": "{_PAPER_ONLY}"}}', _source(_PAPER))
        optional = EvidenceRequirement(source_ref=_COUNTER, role="support", claim="a nice-to-have", required=False)
        criterion = _answerable(_SUPPORT, optional)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "pass", (supporting,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [(claim.claim, claim.support.status, claim.critical) for claim in assessment.claims] == [
            (_SUPPORT.claim, "pass", True),
            ("a nice-to-have", "unknown", False),
        ]
        assert _COUNTER in assessment.claims[1].support.reason
        assert assessment.overall.status == "pass"

    async def test_an_unanswered_requirement_that_does_decide_the_case_still_folds_the_answer_to_unknown(self, tmp_path: Path):
        record = _record(tmp_path)
        supporting = await record.deliver(f'{{"note": "{_PAPER_ONLY}"}}', _source(_PAPER))
        deciding = EvidenceRequirement(source_ref=_COUNTER, role="support", claim="a must-have")
        criterion = _answerable(_SUPPORT, deciding)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "pass", (supporting,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [(claim.claim, claim.critical) for claim in assessment.claims] == [(_SUPPORT.claim, True), ("a must-have", True)]
        assert assessment.overall.status == "unknown"

    async def test_two_requirements_sharing_a_claim_text_bind_in_their_declared_order(self, tmp_path: Path):
        # The reason on each claim must name the source and version of the requirement it answers.
        record = _record(tmp_path)
        supporting = await record.deliver(f'{{"note": "{_PAPER_ONLY}"}}', _source(_PAPER))
        pinned = EvidenceRequirement(source_ref=_PAPER, role="support", claim="C", source_version="2025-06-02")
        counter = EvidenceRequirement(source_ref=_COUNTER, role="counterevidence", claim="C")
        criterion = _answerable(pinned, counter)
        verdict = assessment_at(
            criterion, "pass", claims=(claim_at("C", "pass", (supporting,)), claim_at("C", "pass", (supporting,)))
        )

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [claim.support.status for claim in assessment.claims] == ["unknown", "unknown"]
        assert _PAPER in assessment.claims[0].support.reason
        assert "2025-06-02" in assessment.claims[0].support.reason
        assert _COUNTER in assessment.claims[1].support.reason

    async def test_one_requirement_stated_twice_takes_two_assessments(self, tmp_path: Path):
        # Binding and the unanswered backstop both count positions, so two requirements alike on
        # every field are two requirements to each of them.
        record = _record(tmp_path)
        supporting = await record.deliver(f'{{"note": "{_PAPER_ONLY}"}}', _source(_PAPER))
        criterion = _answerable(_SUPPORT, _SUPPORT)
        verdict = assessment_at(criterion, "pass", claims=(claim_at(_SUPPORT.claim, "pass", (supporting,)),))

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [(claim.claim, claim.support.status, claim.critical) for claim in assessment.claims] == [
            (_SUPPORT.claim, "pass", True),
            (_SUPPORT.claim, "unknown", True),
        ]
        assert assessment.overall.status == "unknown"

    async def test_a_claim_binding_to_no_requirement_is_still_held_to_the_record(self, tmp_path: Path):
        record = _record(tmp_path)
        answering = await record.deliver(f'{{"note": "{_PAPER_ONLY}"}}', _source(_PAPER))
        invented = DeliveredEvidenceRef(
            evidence_id=f"{content_fingerprint('another trial')}:0",
            ordinal=0,
            payload_sha256=content_fingerprint("never stored"),
            payload_bytes=12,
            sources=(_source(_PAPER),),
        )
        criterion = _answerable(_SUPPORT)
        verdict = assessment_at(
            criterion,
            "pass",
            claims=(claim_at(_SUPPORT.claim, "pass", (answering,)), claim_at("an aside", "pass", (invented,))),
        )

        assessment = await _grader(_Grading(verdict=verdict), record)(criterion, "Hybrid wins.", record.evidence)

        assert [claim.support.status for claim in assessment.claims] == ["pass", "unknown"]
        assert "did not retain" in assessment.claims[1].support.reason

    def test_the_instructions_ask_for_one_assessment_per_requirement(self):
        assert "one assessment for every entry of the criterion's `evidence`" in ANSWER_GRADER_INSTRUCTIONS
