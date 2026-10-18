"""The metrics of one evaluated case: recall@k, precision@k, MRR, tool-call counts and answer accuracy.

The agent under test returns a `RetrievalAnswer` whose ordered `cited_source_ids` are scored against
the expected page ids, and the captured span tree gives the tool-call counts.
A case stores its expected page ids as a `RetrievalAnswer` too, because pydantic-evals gives the answer
and the expected answer one type. Both sides are reduced to page ids before scoring, so a claim anchor
and its page count as the same page.
The recall, precision and MRR keys carry no layer prefix. The layered metrics are computed separately
by `layered_evidence_metrics` in `engram.evals.outcomes`.
`AnswerAccuracy` scores the answer text against the expected answer, with no model grading it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_evals.evaluators import Evaluator

from engram.evals.harness.domain.answers import is_official_match, is_strict_match
from engram.evals.outcomes.contracts import DEFAULT_KS, citation_source_metrics
from engram.evals.shared.contracts import MRR_KEY, PRECISION_PREFIX, RECALL_PREFIX, QaCaseMeta, QaInput, RetrievalAnswer

if TYPE_CHECKING:
    from pydantic_evals.evaluators import EvaluatorContext
    from pydantic_evals.otel.span_tree import SpanNode, SpanTree

_EXECUTE_TOOL_OPERATION = "execute_tool"
_TOOL_OPERATION_ATTRIBUTE = "gen_ai.operation.name"

# A disclosure exchange carries `disclosure.ref` only when its result was a conformant envelope,
# so that attribute marks a real exchange.
_DISCLOSURE_REF_ATTRIBUTE = "disclosure.ref"
_DISCLOSURE_BUDGET_ATTRIBUTE = "disclosure.budget_reached"

_ANSWER_STRICT_KEY = "answer_strict"
_ANSWER_OFFICIAL_KEY = "answer_official"


def _is_tool_span(node: SpanNode) -> bool:
    """Report whether a span is a tool call, by its `gen_ai.operation.name` attribute."""
    return node.attributes.get(_TOOL_OPERATION_ATTRIBUTE) == _EXECUTE_TOOL_OPERATION


def _count_tool_spans(span_tree: SpanTree) -> int:
    """Count the tool-call spans in the span tree."""
    return len(span_tree.find(_is_tool_span))


def _is_disclosure_span(node: SpanNode) -> bool:
    """Report whether a span records a conformant progressive-disclosure exchange."""
    return _DISCLOSURE_REF_ATTRIBUTE in node.attributes


def _count_disclosure_spans(span_tree: SpanTree) -> int:
    """Count the conformant disclosure exchanges in the span tree."""
    return len(span_tree.find(_is_disclosure_span))


def _budget_reached(span_tree: SpanTree) -> bool:
    """Report whether any disclosure exchange reached the run's envelope-token budget."""
    return any(node.attributes.get(_DISCLOSURE_BUDGET_ATTRIBUTE) is True for node in span_tree.find(_is_disclosure_span))


@dataclass
class RetrievalMetrics(Evaluator[QaInput, RetrievalAnswer, QaCaseMeta]):
    """Score retrieval quality from the agent's ordered citations and the captured span tree.

    For each `k` in `ks` it reports `recall@{k}` and `precision@{k}` at page level, plus `mrr` and three
    values from the span tree: `n_tool_calls`, `n_disclosure_calls` and `disclosure_budget_reached`.
    Citations are reduced to page ids first, so the scores compare with the dataset's expected page ids.
    A case with no expected page ids gets no recall, precision or MRR, so an average leaves it out.
    The two disclosure values are `0.0` for a run without the disclosure capability.

    Attributes:
        ks: The recall and precision cutoffs to report.
    """

    ks: tuple[int, ...] = field(default=DEFAULT_KS)

    def evaluate(self, ctx: EvaluatorContext[QaInput, RetrievalAnswer, QaCaseMeta]) -> dict[str, float]:
        """Compute the retrieval metrics for one evaluated case.

        Args:
            ctx: The pydantic-evals context holding the agent's `output`, the `expected_output` (a
                `RetrievalAnswer` whose `cited_source_ids` are the expected page ids) and the captured `span_tree`.

        Returns:
            A mapping with `recall@{k}` and `precision@{k}` for every `k` in `ks`, plus `mrr`,
            `n_tool_calls`, `n_disclosure_calls` and `disclosure_budget_reached` (`1.0` or `0.0`).
            With no expected page ids, from no `expected_output` or an empty one, the recall, precision
            and MRR entries are left out, and the span tree values are still scored.
        """
        expected = ctx.expected_output.cited_source_ids if ctx.expected_output is not None else ()

        scores: dict[str, float] = {}
        if expected:
            citation = citation_source_metrics(ctx.output.cited_source_ids, expected, ks=self.ks)
            for k in self.ks:
                scores[f"{RECALL_PREFIX}{k}"] = citation.metrics.recall[k]
                scores[f"{PRECISION_PREFIX}{k}"] = citation.metrics.precision[k]
            scores[MRR_KEY] = citation.metrics.reciprocal_rank
        scores["n_tool_calls"] = float(_count_tool_spans(ctx.span_tree))
        scores["n_disclosure_calls"] = float(_count_disclosure_spans(ctx.span_tree))
        scores["disclosure_budget_reached"] = float(_budget_reached(ctx.span_tree))
        return scores


@dataclass
class AnswerAccuracy(Evaluator[QaInput, RetrievalAnswer, QaCaseMeta]):
    """Score the agent's answer against the case's expected answer, strictly and by the benchmark's word-overlap rule.

    It reports `answer_strict` and `answer_official`, each `1.0` for a match and `0.0` for none. A case with
    no expected answer gets neither, so an average leaves it out.
    """

    def evaluate(self, ctx: EvaluatorContext[QaInput, RetrievalAnswer, QaCaseMeta]) -> dict[str, float]:
        """Compare the agent's answer with the case's expected answer.

        Args:
            ctx: The pydantic-evals context holding the agent's `output` and the `expected_output`, whose
                `answer` is the expected answer.

        Returns:
            `answer_strict` and `answer_official`, or an empty mapping when the expected answer is missing or blank.
        """
        expected = ctx.expected_output.answer if ctx.expected_output is not None else ""
        if not expected.strip():
            return {}
        return {
            _ANSWER_STRICT_KEY: float(is_strict_match(ctx.output.answer, expected)),
            _ANSWER_OFFICIAL_KEY: float(is_official_match(ctx.output.answer, expected)),
        }
