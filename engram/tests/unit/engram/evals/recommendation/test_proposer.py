"""Unit specs for the recommendation loop's proposer.

The proposer is the recommendation's controller brain: given a `StructuralDiagnosis` and the knowledge base's current
`KnowledgeBaseSchema`, it drafts a `DraftRecommendation` (typed spec-edit ops plus a free-form findings tail).
The draft carries only LLM-authored content, no measured `ProposalEvidence`, so the judge-free
constraint holds and the gate composes the evidence mechanically.

The agent is driven network-free: `TestModel` for the output-shape and tool-surface assertions, a
`FunctionModel` that captures `(messages, info)` to assert the assembled prompt carries the schema
vocabulary and the diagnosis's case identifiers, and a `FunctionModel` that emits an invalid op
discriminator then a valid one to pin the discriminated-union retry behaviour. The instruction prose
itself is never asserted (house rule: prompt wording is validated by live sweeps, not unit tests).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import Agent, ModelResponse, ToolCallPart, UnexpectedModelBehavior, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from engram.evals.rca.contracts import Diagnosis, FailingCase
from engram.evals.recommendation.contracts import CandidateEdge, DraftRecommendation, MinedLink, SectionStat, StructuralDiagnosis
from engram.evals.recommendation.domain.detectors import MissingEdgeSignal, SectionSplitSignal
from engram.evals.recommendation.domain.recommendation import (
    AddRelationOp,
    CounterfactualResult,
    DraftProposal,
    PageWorkOrder,
    Prediction,
    StructuralFinding,
)
from engram.evals.recommendation.infrastructure.proposer import (
    PydanticAiRecommendationProposer,
    _recommendation_prompt,
    build_recommendation_agent,
)
from engram.evals.shared.domain import Archetype
from engram.kb.contracts import CanonicalSection, FrontmatterFieldSpec, KnowledgeBaseSchema, PageTypeSpec, RelationDecl

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

models_block = pytest.importorskip("pydantic_ai.models")
models_block.ALLOW_MODEL_REQUESTS = False  # hard-block stray real-provider calls; the doubles stay exempt

_TRACE_QUESTION = "How does LightRAG extend GraphRAG?"

# --- fixtures: a current spec, a diagnosis, and the draft the model is scripted to emit ----------


def _spec() -> KnowledgeBaseSchema:
    """A minimal two-vocabulary spec: one `paper` page type and one `cites` relation."""
    return KnowledgeBaseSchema(
        slug="test-knowledge-base",
        page_types=(
            PageTypeSpec(
                page_type="paper",
                id_prefix="paper",
                canonical_sections=(CanonicalSection(slug="intro"), CanonicalSection(slug="findings")),
                frontmatter_fields=(FrontmatterFieldSpec(name="cites", type="link-list"),),
                edge_fields=("cites",),
            ),
        ),
        relations=(RelationDecl(slug="cites", domain="paper", range="paper"),),
    )


def _diagnosis() -> StructuralDiagnosis:
    """A structural diagnosis with a section-split and a missing-edge signal on one failing trace case."""
    base = Diagnosis(
        overall={"recall@5": 0.4},
        buckets=(),
        worst_cases=(
            FailingCase(
                question=_TRACE_QUESTION,
                gold_ids=("p:1", "p:2"),
                cited_ids=("p:9",),
                recall=0.0,
                n_tool_calls=2.0,
                archetype=Archetype.TRACE,
                difficulty="hard",
            ),
        ),
        mean_duration_s=1.0,
        mean_total_tokens=None,
    )
    return StructuralDiagnosis(
        base=base,
        evidence_grade="raw-baseline",
        section_splits=(
            SectionSplitSignal(
                page_type="paper", case_names=(_TRACE_QUESTION,), offenders=(SectionStat(page_id="p:1", canon="findings", tokens=900),)
            ),
        ),
        missing_edges=(
            MissingEdgeSignal(
                case_names=(_TRACE_QUESTION,),
                unreachable_pairs=(("p:1", "p:2"),),
                mined_candidates=(
                    MinedLink(
                        src_page_id="p:1", src_type="paper", raw_target="GraphRAG", dst_page_id="p:2", dst_type="paper", rel="mentions"
                    ),
                ),
                counterfactual=CounterfactualResult(
                    n_pairs=1,
                    reachable_before=0,
                    reachable_after=1,
                    max_hops=3,
                    candidate_edges=(CandidateEdge(src_id="p:1", dst_id="p:2", rel="mentions"),),
                ),
            ),
        ),
        over_expansion=None,
    )


def _draft(*, rationale: str = "add a derives-from relation to connect the unreachable gold pair") -> DraftRecommendation:
    """A valid draft the model is scripted to emit (one add-relation proposal plus a finding)."""
    return DraftRecommendation(
        proposals=(
            DraftProposal(
                id="add-relation-derives-from-on-paper",
                op=AddRelationOp(
                    relation=RelationDecl(slug="derives_from", domain="paper", range="paper"),
                    on_page_type="paper",
                    edge_field=FrontmatterFieldSpec(name="derives_from", type="link-list"),
                ),
                case_names=(_TRACE_QUESTION,),
                rationale="the unlifted p:1 -> p:2 link explains the miss",
                predictions=(
                    Prediction(
                        description="the trace case recovers", target_cases=(_TRACE_QUESTION,), metric="recall@5", direction="increase"
                    ),
                ),
                migration=(PageWorkOrder(page_id="p:1", note_path="Notes/a.md", action="add a derives_from link to p:2"),),
            ),
        ),
        findings=(
            StructuralFinding(
                finding_type="machinery", title="Finer chunks", body="consider smaller embedding units", related_cases=(_TRACE_QUESTION,)
            ),
        ),
        rationale=rationale,
    )


# --- the agent builder --------------------------------------------------------------------------


class TestBuildRecommendationAgent:
    def test_returns_agent_whose_output_is_a_draft_recommendation(self):
        agent = build_recommendation_agent("test")

        assert isinstance(agent, Agent)
        assert agent.output_type is DraftRecommendation
        # A TestModel run proves the output tool is wired from `DraftRecommendation` and validates.
        test_model = TestModel(custom_output_args=_draft().model_dump())
        with agent.override(model=test_model):
            result = agent.run_sync(_recommendation_prompt(_diagnosis(), _spec()))

        assert isinstance(result.output, DraftRecommendation)
        assert result.output.proposals[0].op.op == "add_relation"

    def test_exposes_no_retrieval_tools(self):
        # A recommendation reasons over the diagnosis; it never retrieves, so the tool surface is empty.
        agent = build_recommendation_agent("test")
        test_model = TestModel(custom_output_args=_draft().model_dump())

        with agent.override(model=test_model):
            agent.run_sync(_recommendation_prompt(_diagnosis(), _spec()))

        params = test_model.last_model_request_parameters
        assert params is not None  # populated by the run above (None only before the first request)
        assert params.function_tools == []


# --- the assembled prompt -----------------------------------------------------------------------


class TestRecommendationPrompt:
    def test_renders_current_schema_vocabulary(self):
        prompt = _recommendation_prompt(_diagnosis(), _spec())

        # The existing page-type kind, its section slugs, and its relation slug reach the model.
        assert "paper" in prompt
        assert "intro" in prompt
        assert "findings" in prompt
        assert "cites" in prompt

    def test_renders_diagnosis_case_identifiers_and_signal_data(self):
        diagnosis = _diagnosis()

        prompt = _recommendation_prompt(diagnosis, _spec())

        # The failing case's identifier, its gold ids, the evidence grade, and the over-budget tokens travel.
        assert _TRACE_QUESTION in prompt
        assert "p:1" in prompt
        assert "p:2" in prompt
        assert "raw-baseline" in prompt
        assert "900" in prompt


# --- the pydantic-ai proposer (network-free) ----------------------------------------------------


class TestPydanticAiRecommendationProposer:
    async def test_propose_returns_a_valid_draft_recommendation(self):
        draft = _draft()
        model = TestModel(custom_output_args=draft.model_dump())
        proposer = PydanticAiRecommendationProposer(model)

        out = await proposer.propose(_diagnosis(), _spec())

        assert isinstance(out, DraftRecommendation)
        assert len(out.proposals) == 1
        assert out.proposals[0].op.op == "add_relation"
        assert out.proposals[0].case_names == (_TRACE_QUESTION,)
        assert out.findings[0].finding_type == "machinery"
        assert out.rationale

    async def test_propose_feeds_diagnosis_and_spec_into_the_prompt(self):
        diagnosis = _diagnosis()
        captured: dict[str, object] = {}

        def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            captured["instructions"] = info.instructions
            captured["function_tools"] = [tool.name for tool in info.function_tools]
            captured["user_text"] = "\n".join(
                part.content
                for message in messages
                for part in message.parts
                if isinstance(part, UserPromptPart) and isinstance(part.content, str)
            )
            return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=_draft().model_dump())])

        proposer = PydanticAiRecommendationProposer(FunctionModel(script))

        await proposer.propose(diagnosis, _spec())

        user_text = captured["user_text"]
        assert isinstance(user_text, str)
        # The diagnosis's failing case and the current schema vocabulary travel in the user prompt.
        assert diagnosis.base.worst_cases[0].question in user_text
        assert "paper" in user_text
        assert "cites" in user_text
        # No retrieval tools are exposed to the recommendation.
        assert captured["function_tools"] == []

    async def test_invalid_op_is_rejected_then_a_valid_emit_recovers(self):
        # A bad `op` discriminator fails the discriminated-union validator; pydantic-ai retries, and a
        # valid re-emit succeeds rather than a fabricated op leaking into the draft.
        calls = {"n": 0}

        def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            calls["n"] += 1
            args = _draft().model_dump()
            if calls["n"] == 1:
                args["proposals"][0]["op"]["op"] = "delete_relation"  # not a valid SchemaEditOp discriminator
            return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])

        proposer = PydanticAiRecommendationProposer(FunctionModel(script))

        out = await proposer.propose(_diagnosis(), _spec())

        assert calls["n"] == 2  # the first (invalid) emit was retried
        assert out.proposals[0].op.op == "add_relation"

    async def test_a_persistently_invalid_op_exhausts_the_retries(self):
        # When the model never recovers, the exhausted output retries surface as model misbehaviour
        # rather than a silently-accepted invalid op.
        def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            args = _draft().model_dump()
            args["proposals"][0]["op"]["op"] = "delete_relation"
            return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])

        proposer = PydanticAiRecommendationProposer(FunctionModel(script))

        with pytest.raises(UnexpectedModelBehavior):
            await proposer.propose(_diagnosis(), _spec())
