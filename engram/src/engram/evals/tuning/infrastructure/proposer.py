"""The pydantic-ai proposer that drafts one targeted retrieval-policy revision per tuning round.

Given the current `RetrievalPolicy` and its `Diagnosis`, the agent emits exactly one targeted edit plus a
rationale, so each round changes a single knob and the loop can attribute a metric move to it. Its output is a
`ProposedPolicy`, whose policy is validated, so an out-of-set strategy is an output retry and never a bad policy.
The agent carries no retrieval tools, only its one output tool.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import Agent

from engram.evals.shared.contracts import TOOL_NAMES
from engram.evals.tuning.domain.proposal import ProposedPolicy
from engram.evals.tuning.strategy import EVAL_STRATEGY_NAMES

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic_ai.models import Model

    from engram.evals.rca.contracts import BucketPrediction, Diagnosis, ReflectiveFinding
    from engram.evals.tuning.policy import RetrievalPolicy

# The valid edit targets named in the system prompt: the strategy names and the tool names, each
# joined into one comma-separated string at module load.
_VALID_STRATEGIES = ", ".join(EVAL_STRATEGY_NAMES)
_VALID_TOOLS = ", ".join(sorted(TOOL_NAMES))

_PROPOSER_INSTRUCTIONS = (
    "You are a retrieval-policy optimizer for a knowledge base retrieval agent. You are given the current "
    "retrieval policy and a diagnosis of where the agent-under-test is weak. Propose exactly ONE "
    "targeted change per round (hill-climb) that should raise the primary accuracy metric (recall) "
    "without blowing the cost budget. The single change must edit ONE of: the agent `instructions`, "
    "the retrieval `strategy`, or one tool's description. Return the COMPLETE revised policy with "
    "every unchanged knob carried over verbatim, plus a rationale naming the one knob you changed "
    f"and the diagnostic evidence for it. The `strategy` must be one of: {_VALID_STRATEGIES}. A tool "
    f"description override may only key one of: {_VALID_TOOLS}."
)


def build_proposer_agent(model: Model | str, *, instructions: str = _PROPOSER_INSTRUCTIONS) -> Agent[None, ProposedPolicy]:
    """Build the policy-proposer agent (structured output, no retrieval tools).

    The agent exposes no tools, only the `ProposedPolicy` output tool.

    Args:
        model: The model that proposes revisions, Azure OpenAI in production, a network-free
            `TestModel` / `FunctionModel` in tests.
        instructions: The optimizer system prompt (defaults to `_PROPOSER_INSTRUCTIONS`).

    Returns:
        An `Agent` whose output is a validated `ProposedPolicy`.
    """
    return Agent(model, instructions=instructions, output_type=ProposedPolicy)


def _proposer_prompt(policy: RetrievalPolicy, diagnosis: Diagnosis, *, findings: tuple[ReflectiveFinding, ...] = ()) -> str:
    """The user prompt: the current policy, the diagnosis, any gated findings and the in-set edit targets."""
    reflective = f"--- reflective findings (they only suggest, the measurements decide) ---\n{_render_findings(findings)}\n" if findings else ""
    return (
        "Propose exactly one targeted revision to the current retrieval policy that should raise "
        "recall without blowing the cost budget, then explain it.\n\n"
        f"--- current policy ---\n{_render_policy(policy)}\n"
        f"--- diagnosis ---\n{_render_diagnosis(diagnosis)}\n"
        f"{reflective}"
        f"--- available edit targets ---\n"
        f"strategy (pick one): {_VALID_STRATEGIES}\n"
        f"tool descriptions (key one): {_VALID_TOOLS}\n"
        "--- end ---"
    )


def _render_findings(findings: tuple[ReflectiveFinding, ...]) -> str:
    """Render the gated findings, ranked, each with its blamed surface, error type, and predictions."""
    return "\n".join(
        f"  {rank}. [{finding.surface}/{finding.error_type}] {finding.narrative}\n"
        f"     predicts: {_render_predictions(finding.manifest.predictions) or '(none)'}"
        f"{f', at risk: {_render_predictions(finding.manifest.at_risk)}' if finding.manifest.at_risk else ''}"
        for rank, finding in enumerate(findings, start=1)
    )


def _render_predictions(predictions: tuple[BucketPrediction, ...]) -> str:
    """Render bucket predictions compactly (`archetype/difficulty metric up|down`, comma-joined)."""
    return ", ".join(
        f"{prediction.archetype.value if prediction.archetype is not None else 'unlabelled'}/{prediction.difficulty} "
        f"{prediction.metric} {prediction.direction}"
        for prediction in predictions
    )


def _render_policy(policy: RetrievalPolicy) -> str:
    """Render the current policy's three knobs (instructions, strategy, tool-description overrides)."""
    overrides = ", ".join(sorted(policy.tool_descriptions)) if policy.tool_descriptions else "(none: shipped defaults)"
    return f"instructions: {policy.instructions}\nstrategy: {policy.strategy}\ntool description overrides: {overrides}"


def _render_diagnosis(diagnosis: Diagnosis) -> str:
    """Render the diagnosis: overall metrics, weak buckets, worst cases, and the cost summary."""
    tokens = "unknown" if diagnosis.mean_total_tokens is None else f"{diagnosis.mean_total_tokens:.0f}"
    sections = (
        f"overall: {_render_metrics(diagnosis.overall)}",
        "weak buckets (by archetype/difficulty):\n" + _render_buckets(diagnosis),
        "worst cases (lowest recall):\n" + _render_worst_cases(diagnosis),
        f"cost: mean duration {diagnosis.mean_duration_s:.2f}s, mean total tokens {tokens}",
    )
    return "\n".join(sections)


def _render_metrics(metrics: Mapping[str, float]) -> str:
    """Render a metric mapping as a compact `name=value` list (sorted for a stable prompt)."""
    return ", ".join(f"{name}={value:.3f}" for name, value in sorted(metrics.items())) or "(none)"


def _render_buckets(diagnosis: Diagnosis) -> str:
    """Render each per-bucket slice as a labelled line of its case count and mean metrics."""
    if not diagnosis.buckets:
        return "  (none)"
    return "\n".join(
        f"  - {bucket.archetype.value if bucket.archetype is not None else 'unlabelled'}/{bucket.difficulty} "
        f"(n={bucket.n_cases}): {_render_metrics(bucket.metrics)}"
        for bucket in diagnosis.buckets
    )


def _render_worst_cases(diagnosis: Diagnosis) -> str:
    """Render each worst case as its question, gold vs cited ids, recall, and tool-call count."""
    if not diagnosis.worst_cases:
        return "  (none)"
    return "\n".join(
        f"  - {case.archetype.value if case.archetype is not None else 'unlabelled'}/{case.difficulty} "
        f"recall={case.recall:.3f} tool_calls={case.n_tool_calls:.0f}\n"
        f"    question: {case.question}\n"
        f"    gold ids: {', '.join(case.gold_ids) or '(none)'}\n"
        f"    cited ids: {', '.join(case.cited_ids) or '(none)'}"
        for case in diagnosis.worst_cases
    )


class PydanticAiProposer:
    """A `PolicyProposer` that drives a pydantic-ai agent over an injected model.

    It builds the agent afresh from the stored model on every call.
    """

    def __init__(self, model: Model | str) -> None:
        """Bind the model the per-call proposer agent is built from.

        Args:
            model: The model the agent proposes with, a model instance or a model name.
        """
        self._model = model

    async def propose(
        self, policy: RetrievalPolicy, diagnosis: Diagnosis, *, findings: tuple[ReflectiveFinding, ...] = ()
    ) -> ProposedPolicy:
        """Run the proposer agent on a prompt built from `policy`, `diagnosis` and `findings`.

        The returned policy is always in-set, because pydantic-ai retries the model on an invalid strategy.

        Args:
            policy: The current policy to revise.
            diagnosis: The structured error signal to reason about.
            findings: The round's gated reflective findings, rendered as a findings section when
                present. Empty leaves the prompt without one.

        Returns:
            The model's proposed revision (a full revised policy plus a rationale).
        """
        agent = build_proposer_agent(self._model)
        result = await agent.run(_proposer_prompt(policy, diagnosis, findings=findings))
        return result.output
