"""Hand-computed correctness tests for the retrieval evaluators.

These tests *are* the spec for the metric math: every expected value is computed by hand from
the definitions in the plan (recall@k / precision@k / MRR over the ordered page ids the agent
cited, scored against the expected page ids). The
:class:`~engram.evals.harness.domain.evaluators.RetrievalMetrics` evaluator is exercised through a
hand-built :class:`~pydantic_evals.evaluators.EvaluatorContext` so we cover the real
pydantic-evals surface (`ctx.output` / `ctx.expected_output` / `ctx.span_tree`) without a
live agent run. pydantic-evals shares one `OutputT` between actual and expected, so the expected
ground truth here is a :class:`~engram.evals.shared.domain.RetrievalAnswer` whose ``cited_source_ids``
hold the expected page ids (the shape the harness stores in each `Case`).

The underlying recall/precision/MRR arithmetic and the page-id normalizer are the citation
layer's shared core (`engram.evals.outcomes.domain.evaluators`). Their hand-computed specs live in
`tests/unit/engram/evals/outcomes/test_evaluators.py` alongside the other two layers.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pydantic_evals.evaluators import EvaluatorContext
from pydantic_evals.otel.span_tree import AttributeValue, SpanNode, SpanTree

from engram.evals.harness.domain.evaluators import (
    AnswerAccuracy,
    RetrievalMetrics,
    _budget_reached,
    _count_disclosure_spans,
    _count_tool_spans,
)
from engram.evals.shared.domain import QaCaseMeta, QaInput, RetrievalAnswer

_EPSILON = 1e-9


def _span(span_id: int, name: str, attributes: dict[str, AttributeValue]) -> SpanNode:
    """Build a leaf :class:`SpanNode` with a fixed (zero-duration) timestamp for tests."""
    moment = datetime(2026, 1, 1, tzinfo=UTC)
    return SpanNode(
        name=name, trace_id=1, span_id=span_id, parent_span_id=None, start_timestamp=moment, end_timestamp=moment, attributes=attributes
    )


def _span_tree(*nodes: SpanNode) -> SpanTree:
    """Assemble a :class:`SpanTree` from the given nodes (mirrors how the harness records them)."""
    tree = SpanTree(nodes_by_id={})
    tree.add_spans(list(nodes))
    return tree


def _context(
    *, output: RetrievalAnswer, expected: RetrievalAnswer | None, span_tree: SpanTree
) -> EvaluatorContext[QaInput, RetrievalAnswer, QaCaseMeta]:
    """Construct the kw-only :class:`EvaluatorContext` pydantic-evals passes to ``evaluate``."""
    return EvaluatorContext(
        name="case",
        inputs=QaInput(question="q"),
        metadata=None,
        expected_output=expected,
        output=output,
        duration=0.0,
        _span_tree=span_tree,
        attributes={},
        metrics={},
    )


class TestCountToolSpans:
    def test_counts_execute_tool_spans_across_naming_conventions(self):
        # Both pydantic-ai instrumentation versions tag tool spans with
        # `gen_ai.operation.name == "execute_tool"`; the span *name* differs (v3 "execute_tool <name>"
        # vs legacy "running tool"), so we count on the version-stable attribute, not the name.
        tree = _span_tree(
            _span(1, "execute_tool search", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "search"}),
            _span(2, "running tool", {"gen_ai.operation.name": "execute_tool", "gen_ai.tool.name": "fetch"}),
            _span(3, "chat gpt-4o", {"gen_ai.operation.name": "chat"}),
            _span(4, "agent run", {}),
        )

        assert _count_tool_spans(tree) == 2

    def test_no_tool_spans_gives_zero(self):
        tree = _span_tree(_span(1, "chat", {"gen_ai.operation.name": "chat"}))

        assert _count_tool_spans(tree) == 0

    def test_empty_tree_gives_zero(self):
        assert _count_tool_spans(_span_tree()) == 0


def _disclosure_span(span_id: int, *, ref: str, budget_reached: bool) -> SpanNode:
    """A conformant disclosure exchange as the capability records it (envelope attributes stamped)."""
    return _span(
        span_id,
        "disclosure tool call",
        {
            "tool": "disclose",
            "disclosure.ref": ref,
            "disclosure.kind": "page",
            "disclosure.mode": "auto",
            "disclosure.budget_reached": budget_reached,
        },
    )


class TestCountDisclosureSpans:
    def test_counts_only_spans_carrying_the_envelope_attributes(self):
        # The capability wraps EVERY tool call in a "disclosure tool call" span but stamps the
        # `disclosure.*` attributes only when the result came back a conformant envelope, so a
        # non-conformant passthrough must not count as a disclosure exchange.
        tree = _span_tree(
            _disclosure_span(1, ref="collection:workdocs", budget_reached=False),
            _disclosure_span(2, ref="page:mu.three-planes-model", budget_reached=False),
            _span(3, "disclosure tool call", {"tool": "search"}),
            _span(4, "execute_tool search", {"gen_ai.operation.name": "execute_tool"}),
        )

        assert _count_disclosure_spans(tree) == 2

    def test_a_run_that_never_disclosed_gives_zero(self):
        tree = _span_tree(_span(1, "execute_tool search", {"gen_ai.operation.name": "execute_tool"}))

        assert _count_disclosure_spans(tree) == 0

    def test_empty_tree_gives_zero(self):
        assert _count_disclosure_spans(_span_tree()) == 0


class TestBudgetReached:
    def test_true_when_any_exchange_tripped_the_budget(self):
        tree = _span_tree(
            _disclosure_span(1, ref="collection:workdocs", budget_reached=False),
            _disclosure_span(2, ref="page:bs.memory-engineering-lifecycle", budget_reached=True),
        )

        assert _budget_reached(tree) is True

    def test_false_when_every_exchange_stayed_within_budget(self):
        tree = _span_tree(
            _disclosure_span(1, ref="collection:workdocs", budget_reached=False),
            _disclosure_span(2, ref="page:mu.three-planes-model", budget_reached=False),
        )

        assert _budget_reached(tree) is False

    def test_false_for_a_run_with_no_disclosure_at_all(self):
        assert _budget_reached(_span_tree(_span(1, "agent run", {}))) is False


class TestRetrievalMetricsEvaluate:
    def test_default_ks_produce_every_metric_key(self):
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=["a", "x", "b"]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a", "b", "c"]),
            span_tree=_span_tree(),
        )

        scores = evaluator.evaluate(ctx)

        assert set(scores) == {
            "recall@1",
            "recall@5",
            "recall@10",
            "precision@1",
            "precision@5",
            "precision@10",
            "mrr",
            "n_tool_calls",
            "n_disclosure_calls",
            "disclosure_budget_reached",
        }

    def test_hand_computed_scores_for_canonical_case(self):
        # want = {a, b, c}; got = [a, x, b]; one execute_tool span.
        # recall@1 = 1/3, recall@5 = recall@10 = 2/3 (all of got fits in top-5/10).
        # precision@1 = 1/1 = 1.0; precision@5 = precision@10 = 2/3 (denominator capped at |got| = 3).
        # mrr = 1.0 (a at rank 1); n_tool_calls = 1.0.
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=["a", "x", "b"]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a", "b", "c"]),
            span_tree=_span_tree(_span(1, "execute_tool search", {"gen_ai.operation.name": "execute_tool"})),
        )

        scores = evaluator.evaluate(ctx)

        assert scores["recall@1"] == pytest.approx(1 / 3, abs=_EPSILON)
        assert scores["recall@5"] == pytest.approx(2 / 3, abs=_EPSILON)
        assert scores["recall@10"] == pytest.approx(2 / 3, abs=_EPSILON)
        assert scores["precision@1"] == pytest.approx(1.0, abs=_EPSILON)
        assert scores["precision@5"] == pytest.approx(2 / 3, abs=_EPSILON)
        assert scores["precision@10"] == pytest.approx(2 / 3, abs=_EPSILON)
        assert scores["mrr"] == pytest.approx(1.0, abs=_EPSILON)
        assert scores["n_tool_calls"] == pytest.approx(1.0, abs=_EPSILON)

    def test_a_disclosure_run_reports_its_descent_and_a_clean_budget(self):
        # The evidence a `--disclosure` acceptance run needs about itself: how many envelopes it
        # actually opened, and whether any of them tripped the stub.
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=["a"]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a"]),
            span_tree=_span_tree(
                _span(1, "execute_tool disclose", {"gen_ai.operation.name": "execute_tool"}),
                _disclosure_span(2, ref="collection:workdocs", budget_reached=False),
                _disclosure_span(3, ref="page:mu.three-planes-model", budget_reached=False),
            ),
        )

        scores = evaluator.evaluate(ctx)

        assert scores["n_disclosure_calls"] == pytest.approx(2.0, abs=_EPSILON)
        assert scores["disclosure_budget_reached"] == pytest.approx(0.0, abs=_EPSILON)

    def test_a_run_that_never_disclosed_is_distinguishable_from_one_that_did(self):
        # Recall alone cannot tell these apart, which is the whole reason the count is emitted.
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=["a"]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a"]),
            span_tree=_span_tree(_span(1, "execute_tool search", {"gen_ai.operation.name": "execute_tool"})),
        )

        scores = evaluator.evaluate(ctx)

        assert scores["recall@1"] == pytest.approx(1.0, abs=_EPSILON)
        assert scores["n_disclosure_calls"] == pytest.approx(0.0, abs=_EPSILON)

    def test_a_tripped_budget_surfaces_as_one(self):
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=["a"]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a"]),
            span_tree=_span_tree(_disclosure_span(1, ref="page:bs.memory-engineering-lifecycle", budget_reached=True)),
        )

        scores = evaluator.evaluate(ctx)

        assert scores["disclosure_budget_reached"] == pytest.approx(1.0, abs=_EPSILON)

    def test_custom_ks_drive_the_emitted_keys(self):
        evaluator = RetrievalMetrics(ks=(2,))
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=["a", "x", "b"]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a", "b", "c"]),
            span_tree=_span_tree(),
        )

        scores = evaluator.evaluate(ctx)

        assert set(scores) == {"recall@2", "precision@2", "mrr", "n_tool_calls", "n_disclosure_calls", "disclosure_budget_reached"}
        # recall@2 = |{a,x} ∩ want| / 3 = 1/3; precision@2 = 1 / min(2, 3) = 1/2.
        assert scores["recall@2"] == pytest.approx(1 / 3, abs=_EPSILON)
        assert scores["precision@2"] == pytest.approx(0.5, abs=_EPSILON)

    def test_claim_anchors_in_output_are_normalized_before_scoring(self):
        # cited = ["a#^c1", "a", "b"] -> page ids [a, b]; the anchor and bare "a" collapse.
        # want = {a, b}: recall@1 = 1/2, recall@5 = 1.0; mrr = 1.0 (a at rank 1).
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=["a#^c1", "a", "b"]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a", "b"]),
            span_tree=_span_tree(),
        )

        scores = evaluator.evaluate(ctx)

        assert scores["recall@1"] == pytest.approx(0.5, abs=_EPSILON)
        assert scores["recall@5"] == pytest.approx(1.0, abs=_EPSILON)
        assert scores["mrr"] == pytest.approx(1.0, abs=_EPSILON)

    def test_claim_anchors_in_expected_are_normalized_before_scoring(self):
        # The expected side is normalized too: expected ["a#^c1", "b"] -> want {a, b}.
        # got = [a, b] -> recall@5 = 1.0, precision@5 = 1.0, mrr = 1.0.
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=["a", "b"]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a#^c1", "b"]),
            span_tree=_span_tree(),
        )

        scores = evaluator.evaluate(ctx)

        assert scores["recall@5"] == pytest.approx(1.0, abs=_EPSILON)
        assert scores["precision@5"] == pytest.approx(1.0, abs=_EPSILON)
        assert scores["mrr"] == pytest.approx(1.0, abs=_EPSILON)

    def test_no_expected_output_scores_no_retrieval_keys(self):
        evaluator = RetrievalMetrics()
        ctx = _context(output=RetrievalAnswer(answer="a", cited_source_ids=["a", "b"]), expected=None, span_tree=_span_tree())

        scores = evaluator.evaluate(ctx)

        assert set(scores) == {"n_tool_calls", "n_disclosure_calls", "disclosure_budget_reached"}

    def test_a_case_with_an_answer_and_no_gold_ids_scores_no_retrieval_keys(self):
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="yes", cited_source_ids=["a"]),
            expected=RetrievalAnswer(answer="yes", cited_source_ids=[]),
            span_tree=_span_tree(_span(1, "running tool", {"gen_ai.operation.name": "execute_tool"})),
        )

        scores = evaluator.evaluate(ctx)

        assert scores == {"n_tool_calls": 1.0, "n_disclosure_calls": 0.0, "disclosure_budget_reached": 0.0}

    def test_empty_citations_score_zero_without_error(self):
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=[]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a", "b"]),
            span_tree=_span_tree(),
        )

        scores = evaluator.evaluate(ctx)

        assert scores["recall@1"] == 0.0
        assert scores["precision@1"] == 0.0
        assert scores["mrr"] == 0.0

    def test_evaluate_is_synchronous_and_returns_plain_floats(self):
        evaluator = RetrievalMetrics()
        ctx = _context(
            output=RetrievalAnswer(answer="a", cited_source_ids=["a"]),
            expected=RetrievalAnswer(answer="a", cited_source_ids=["a"]),
            span_tree=_span_tree(),
        )

        scores = evaluator.evaluate(ctx)

        assert all(isinstance(value, float) for value in scores.values())


class TestAnswerAccuracyEvaluate:
    def test_an_answer_differing_only_in_case_scores_one_on_both(self):
        ctx = _context(
            output=RetrievalAnswer(answer="The North Tower", cited_source_ids=[]),
            expected=RetrievalAnswer(answer="the north tower", cited_source_ids=[]),
            span_tree=_span_tree(),
        )

        assert AnswerAccuracy().evaluate(ctx) == {"answer_strict": 1.0, "answer_official": 1.0}

    def test_a_trailing_full_stop_scores_strict_but_not_official(self):
        ctx = _context(
            output=RetrievalAnswer(answer="Yes.", cited_source_ids=[]),
            expected=RetrievalAnswer(answer="yes", cited_source_ids=[]),
            span_tree=_span_tree(),
        )

        assert AnswerAccuracy().evaluate(ctx) == {"answer_strict": 1.0, "answer_official": 0.0}

    def test_an_answer_sharing_one_word_scores_official_but_not_strict(self):
        ctx = _context(
            output=RetrievalAnswer(answer="the north tower", cited_source_ids=["a"]),
            expected=RetrievalAnswer(answer="North", cited_source_ids=["a"]),
            span_tree=_span_tree(),
        )

        assert AnswerAccuracy().evaluate(ctx) == {"answer_strict": 0.0, "answer_official": 1.0}

    def test_a_wrong_answer_scores_zero_on_both(self):
        ctx = _context(
            output=RetrievalAnswer(answer="south", cited_source_ids=[]),
            expected=RetrievalAnswer(answer="north", cited_source_ids=[]),
            span_tree=_span_tree(),
        )

        assert AnswerAccuracy().evaluate(ctx) == {"answer_strict": 0.0, "answer_official": 0.0}

    @pytest.mark.parametrize("expected", [None, RetrievalAnswer(answer="", cited_source_ids=["a"]), RetrievalAnswer(answer="  ", cited_source_ids=[])])
    def test_a_case_with_no_expected_answer_scores_nothing(self, expected: RetrievalAnswer | None):
        ctx = _context(output=RetrievalAnswer(answer="yes", cited_source_ids=["a"]), expected=expected, span_tree=_span_tree())

        assert AnswerAccuracy().evaluate(ctx) == {}
