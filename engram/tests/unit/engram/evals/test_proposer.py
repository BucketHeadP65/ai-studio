"""Unit tests for the tuning loop's policy proposer.

The proposer is the loop's controller brain: given the current `RetrievalPolicy` and a `Diagnosis`
of where retrieval is weak, it proposes one targeted revision (hill-climb) plus a rationale. The
output embeds a full `RetrievalPolicy`, so a proposal is always a valid policy and its `strategy` is
auto-validated against `EVAL_STRATEGY_NAMES` (pydantic-ai retries the model on an invalid emit).

The agent is driven network-free: `TestModel` for the output-shape and tool-surface assertions, a
`FunctionModel` that captures `(messages, info)` to assert the assembled prompt carries the current
policy and the diagnosis's weak buckets plus worst cases, and a `FunctionModel` that emits a bad
strategy then a valid one to pin the nested-validator retry behaviour.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai import Agent, ModelResponse, ToolCallPart, UnexpectedModelBehavior, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from engram.evals.rca.contracts import Diagnosis, FailingCase
from engram.evals.rca.domain.diagnosis import BucketMetrics
from engram.evals.shared.domain import Archetype
from engram.evals.tuning.domain.proposal import ProposedPolicy
from engram.evals.tuning.infrastructure.proposer import PydanticAiProposer, _proposer_prompt, build_proposer_agent
from engram.evals.tuning.policy import RetrievalPolicy, baseline_policy
from engram.evals.tuning.strategy import EVAL_STRATEGY_NAMES, RetrievalStrategy
from engram.retrieval.tools import FETCH, SEARCH

if TYPE_CHECKING:
    from pydantic_ai.messages import ModelMessage

models_block = pytest.importorskip("pydantic_ai.models")
models_block.ALLOW_MODEL_REQUESTS = False  # hard-block stray real-provider calls; the doubles stay exempt

# --- fixtures: a current policy and a diagnosis with a weak bucket + a worst case ---------------


def _weak_diagnosis() -> Diagnosis:
    """A diagnosis with one weak `(synthesis, hard)` bucket and one low-recall worst case."""
    return Diagnosis(
        overall={"recall@5": 0.4, "precision@5": 0.3},
        buckets=(
            BucketMetrics(archetype=Archetype.SYNTHESIS, difficulty="hard", n_cases=3, metrics={"recall@5": 0.2, "precision@5": 0.15}),
        ),
        worst_cases=(
            FailingCase(
                question="How does LightRAG's cost compare to GraphRAG's?",
                gold_ids=("paper.lightrag-2025", "paper.graphrag-2024"),
                cited_ids=("paper.unrelated",),
                recall=0.0,
                n_tool_calls=2.0,
                archetype=Archetype.SYNTHESIS,
                difficulty="hard",
            ),
        ),
        mean_duration_s=1.5,
        mean_total_tokens=2200.0,
    )


def _revised_policy() -> RetrievalPolicy:
    """A valid revised policy the model is scripted to emit (one knob — strategy — changed)."""
    return RetrievalPolicy(
        instructions=baseline_policy().instructions, strategy=RetrievalStrategy.KEYWORD_GRAPH.value, tool_descriptions={}
    )


def _proposed(
    *, policy: RetrievalPolicy | None = None, rationale: str = "switch to the graph leg for cross-page synthesis"
) -> ProposedPolicy:
    return ProposedPolicy(policy=policy if policy is not None else _revised_policy(), rationale=rationale)


# --- the agent builder --------------------------------------------------------------------------


class TestBuildProposerAgent:
    def test_returns_agent_whose_output_is_a_proposed_policy(self):
        agent = build_proposer_agent("test")

        assert isinstance(agent, Agent)
        assert agent.output_type is ProposedPolicy
        # A TestModel run proves the output tool is wired from `ProposedPolicy` and validates.
        test_model = TestModel(custom_output_args=_proposed().model_dump())
        with agent.override(model=test_model):
            result = agent.run_sync(_proposer_prompt(baseline_policy(), _weak_diagnosis()))

        assert isinstance(result.output, ProposedPolicy)
        assert result.output.policy.strategy == RetrievalStrategy.KEYWORD_GRAPH.value

    def test_exposes_no_retrieval_tools(self):
        # A proposer reasons over the diagnosis; it never retrieves, so the tool surface is empty.
        agent = build_proposer_agent("test")
        test_model = TestModel(custom_output_args=_proposed().model_dump())

        with agent.override(model=test_model):
            agent.run_sync(_proposer_prompt(baseline_policy(), _weak_diagnosis()))

        params = test_model.last_model_request_parameters
        assert params is not None  # populated by the run above (None only before the first request)
        assert params.function_tools == []


# --- the assembled prompt -----------------------------------------------------------------------


class TestProposerPrompt:
    def test_renders_current_policy_and_strategy_and_tool_names(self):
        prompt = _proposer_prompt(baseline_policy(), _weak_diagnosis())

        # The current policy's two non-description knobs are present verbatim.
        assert baseline_policy().instructions in prompt
        assert baseline_policy().strategy in prompt
        # The in-set strategy names and tool names are listed so the model stays in-set.
        for name in EVAL_STRATEGY_NAMES:
            assert name in prompt
        assert SEARCH in prompt
        assert FETCH in prompt

    def test_renders_weak_buckets_and_worst_cases(self):
        diagnosis = _weak_diagnosis()

        prompt = _proposer_prompt(baseline_policy(), diagnosis)

        worst = diagnosis.worst_cases[0]
        # The worst case's question, its gold vs cited ids, and its recall reach the model.
        assert worst.question in prompt
        assert worst.gold_ids[0] in prompt
        assert worst.cited_ids[0] in prompt
        # The weak bucket's labels are surfaced so the proposer sees which shape is weak.
        assert "synthesis" in prompt
        assert "hard" in prompt


# --- the pydantic-ai proposer (network-free) ----------------------------------------------------


class TestPydanticAiProposer:
    async def test_propose_returns_a_valid_proposed_policy(self):
        proposed = _proposed()
        model = TestModel(custom_output_args=proposed.model_dump())
        proposer = PydanticAiProposer(model)

        out = await proposer.propose(baseline_policy(), _weak_diagnosis())

        assert isinstance(out, ProposedPolicy)
        assert isinstance(out.policy, RetrievalPolicy)
        # The proposed policy's strategy is in the eval-valid set (the nested validator enforces it).
        assert out.policy.strategy in EVAL_STRATEGY_NAMES
        assert out.policy.strategy == RetrievalStrategy.KEYWORD_GRAPH.value
        assert out.rationale

    async def test_propose_feeds_policy_and_diagnosis_into_the_prompt(self):
        diagnosis = _weak_diagnosis()
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
            return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=_proposed().model_dump())])

        proposer = PydanticAiProposer(FunctionModel(script))

        await proposer.propose(baseline_policy(), diagnosis)

        user_text = captured["user_text"]
        assert isinstance(user_text, str)
        # The current policy and the diagnosis's weak spots travel in the user prompt.
        assert baseline_policy().strategy in user_text
        assert diagnosis.worst_cases[0].question in user_text
        assert diagnosis.worst_cases[0].gold_ids[0] in user_text
        assert "synthesis" in user_text
        # The stable optimizer contract is in the system instructions, not the user prompt.
        assert isinstance(captured["instructions"], str)
        assert "optimiz" in captured["instructions"].lower() or "optimize" in captured["instructions"].lower()
        # No retrieval tools are exposed to the proposer.
        assert captured["function_tools"] == []

    async def test_invalid_strategy_is_rejected_then_a_valid_emit_recovers(self):
        # The nested `RetrievalPolicy` validator gates the output: an out-of-set strategy raises a
        # ValidationError that pydantic-ai surfaces as an output retry, and a valid re-emit succeeds.
        calls = {"n": 0}

        def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            calls["n"] += 1
            strategy = "made-up-strategy" if calls["n"] == 1 else RetrievalStrategy.SEARCH_KEYWORD.value
            args = ProposedPolicy.model_construct(policy=_revised_policy(), rationale="r").model_dump()
            args["policy"]["strategy"] = strategy
            return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])

        proposer = PydanticAiProposer(FunctionModel(script))

        out = await proposer.propose(baseline_policy(), _weak_diagnosis())

        assert calls["n"] == 2  # the first (invalid) emit was retried
        assert out.policy.strategy == RetrievalStrategy.SEARCH_KEYWORD.value

    async def test_a_persistently_invalid_strategy_exhausts_the_retries(self):
        # When the model never recovers, the exhausted output retries surface as model misbehaviour
        # rather than a silently-accepted invalid policy.
        def script(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
            args = ProposedPolicy.model_construct(policy=_revised_policy(), rationale="r").model_dump()
            args["policy"]["strategy"] = "code-mode"  # a real MCP profile, but excluded from the eval set
            return ModelResponse(parts=[ToolCallPart(tool_name=info.output_tools[0].name, args=args)])

        proposer = PydanticAiProposer(FunctionModel(script))

        with pytest.raises(UnexpectedModelBehavior):
            await proposer.propose(baseline_policy(), _weak_diagnosis())
