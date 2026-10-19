"""Layered retrieval-evidence metrics: citation, surfaced-source and raw-ranking, each from its own input.

`ranked_metrics` is the one arithmetic core (recall@k, precision@k and reciprocal rank) every layer calls. It reads the
first k items as k ranks, and a None item holds its rank and matches nothing.
The citation layer scores the agent's own cited ids against the expected ids and is always `measured`.
The surfaced layer scores the references the trial's ok tool calls returned, in first-seen order, resolved
off their collection qualification to the bare ids the other layers compare.
It is `not_applicable` with no tool call at all, and `unknown` when every call failed or was refused.
The ranking layer scores each `search` call's own ordered hits, call boundaries never merged, and its
trial-level scalar is the mean over the calls that reached `measured`.
Granularity `"page"` collapses a reference to its page (everything before its first `#`), and `"unit"`
scores the exact reference, while `ranked_metrics` itself never changes.
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING, Literal

from core.ai.evaluation.contracts import EvaluationModel, ToolCallTrace

from engram.evals.shared.contracts import BadRefError, parse_ref

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable, Mapping, Sequence

type Granularity = Literal["page", "unit"]
"""How a reference is scored: `"page"` collapses it to its owning page, `"unit"` keeps it exact."""

type MetricStatus = Literal["measured", "unknown", "not_applicable"]
"""Whether a layer's arithmetic ran (`measured`), could not (`unknown`), or had nothing to run on
(`not_applicable`). Never a float sentinel: a caller must branch on this before reading `metrics`."""

_ANCHOR_DELIMITER = "#"
DEFAULT_KS: tuple[int, ...] = (1, 5, 10)
"""The recall and precision cutoffs every layer reports when its caller names none."""


def page_of(ref: str) -> str:
    """The page `ref` belongs to: everything before its first `#` anchor delimiter.

    Args:
        ref: A bare reference, anchored (`note.x#^c3`) or not (`note.x`).

    Returns:
        The bare page id, unchanged when `ref` carries no anchor.
    """
    return ref.split(_ANCHOR_DELIMITER, 1)[0]


def _dedup_ordered(refs: Iterable[str]) -> list[str]:
    """`refs` with duplicates dropped, first occurrence kept, order preserved."""
    seen: set[str] = set()
    ordered: list[str] = []
    for ref in refs:
        if ref not in seen:
            seen.add(ref)
            ordered.append(ref)
    return ordered


def page_ids(refs: Sequence[str]) -> list[str]:
    """`refs` collapsed to their owning page, deduplicated, first occurrence kept.

    A claim anchor and its bare page collapse to the same page, so the first of either wins.

    Args:
        refs: The bare references to collapse.

    Returns:
        The page ids, in the order each first appears in `refs`.
    """
    return _dedup_ordered(page_of(ref) for ref in refs)


def normalize_refs(refs: Sequence[str], granularity: Granularity) -> list[str]:
    """`refs` prepared for scoring at `granularity`: bare page ids, or the refs themselves, deduplicated.

    Args:
        refs: The bare references to prepare (already resolved off any collection qualification).
        granularity: `"page"` collapses each ref to its owning page. `"unit"` keeps it exact.

    Returns:
        The deduplicated references, first occurrence kept, in `refs`' own order.
    """
    return page_ids(refs) if granularity == "page" else _dedup_ordered(refs)


def bare_unit_id(ref: str, collection: str) -> str:
    """`ref`'s own unit id, resolved off any `{kind}:{collection}/` qualification.

    A reference already bare (no `kind:` prefix) resolves to itself. A ref this trial's own
    `collection` cannot parse is returned unchanged rather than raised.

    Args:
        ref: A bare or collection-qualified reference.
        collection: The knowledge base a ref with no collection segment of its own resolves within.

    Returns:
        The bare unit id `ref` names.
    """
    try:
        return parse_ref(ref, collection).unit_id
    except BadRefError:
        return ref


def _recall_at_k(got: Sequence[str | None], want: Collection[str], k: int) -> float:
    """`|set(got[:k]) ∩ want| / max(1, |want|)`, guarded against an empty `want`."""
    want_set = set(want)
    hits = len(set(got[:k]) & want_set)
    return hits / max(1, len(want_set))


def _precision_at_k(got: Sequence[str | None], want: Collection[str], k: int) -> float:
    """`|set(got[:k]) ∩ want| / max(1, min(k, |got|))`, guarded against an empty `got`."""
    want_set = set(want)
    hits = len(set(got[:k]) & want_set)
    return hits / max(1, min(k, len(got)))


def _mrr(got: Sequence[str | None], want: Collection[str]) -> float:
    """`1 / rank` of the first item of `got` that is in `want`, or `0.0` when none is."""
    want_set = set(want)
    for index, item in enumerate(got):
        if item in want_set:
            return 1.0 / (index + 1)
    return 0.0


def ndcg_at_k(ranked: Sequence[str | None], grades: Mapping[str, int], k: int) -> float:
    """nDCG@k of `ranked` against graded relevance judgments, from 0 to 1.

    A document's gain is its grade, as trec_eval's `ndcg_cut` counts it, and rank `i` (from 1) is
    discounted by `log2(i + 1)`. The result is the discounted gain of the first `k` ranked documents
    over that of the ideal order of `grades`, and `0.0` when no document has a grade above 0.

    Args:
        ranked: The documents in the order retrieval returned them, each at most once. A None rank has grade 0.
        grades: Each judged document's relevance judgment. A document missing here has grade 0.
        k: The cutoff.

    Returns:
        The normalized discounted cumulative gain.
    """
    ideal = sorted((grade for grade in grades.values() if grade > 0), reverse=True)[:k]
    ideal_gain = sum(grade / math.log2(rank + 2) for rank, grade in enumerate(ideal))
    if ideal_gain == 0:
        return 0.0
    gain = sum(max(grades.get(document, 0), 0) / math.log2(rank + 2) for rank, document in enumerate(ranked[:k]) if document is not None)
    return gain / ideal_gain


class RankedMetrics(EvaluationModel):
    """Recall, precision and reciprocal rank over one ordered `got` sequence, scored against `want`."""

    recall: dict[int, float]
    precision: dict[int, float]
    reciprocal_rank: float


def ranked_metrics(got: Sequence[str | None], want: Collection[str], *, ks: tuple[int, ...]) -> RankedMetrics:
    """The one arithmetic core every layer calls: recall@k, precision@k and MRR over `got` against `want`.

    Args:
        got: The ordered references under scoring, one per rank. A None item holds its rank and matches nothing.
        want: The expected references.
        ks: The recall and precision cutoffs to report.

    Returns:
        Recall and precision at each cutoff, and the reciprocal rank of the first expected reference.
    """
    return RankedMetrics(
        recall={k: _recall_at_k(got, want, k) for k in ks},
        precision={k: _precision_at_k(got, want, k) for k in ks},
        reciprocal_rank=_mrr(got, want),
    )


class CitationSourceMetrics(EvaluationModel):
    """The citation layer: recall/precision/MRR over the agent's own citations against the expected ids.

    Always `measured`: `cited_source_ids` and the expected ids are both always available to a scored
    case, empty or not, so this layer never depends on whether a tool call ran or what it returned.
    """

    status: Literal["measured"] = "measured"
    metrics: RankedMetrics


def citation_source_metrics(
    cited: Sequence[str], expected: Collection[str], *, ks: tuple[int, ...] = DEFAULT_KS, granularity: Granularity = "page"
) -> CitationSourceMetrics:
    """Score the agent's own citations against the expected ids, independent of any tool call.

    Args:
        cited: The agent's ordered citations (`RetrievalAnswer.cited_source_ids`).
        expected: The expected ids (`ExpectedQa.expected_source_ids`).
        ks: The recall/precision cutoffs to report.
        granularity: `"page"` (the default) collapses a claim anchor to its page. `"unit"` scores
            the exact cited reference.

    Returns:
        The citation layer's `CitationSourceMetrics`, always `measured`. The unprefixed `recall@{k}`,
        `precision@{k}` and `mrr` keys are this same layer through the same `ranked_metrics` core, so
        they always agree with the `citation_*` keys.
    """
    got = normalize_refs(cited, granularity)
    want = set(normalize_refs(tuple(expected), granularity))
    return CitationSourceMetrics(metrics=ranked_metrics(got, want, ks=ks))


class SurfacedSourceMetrics(EvaluationModel):
    """The surfaced layer: recall/precision/MRR over every reference an *ok* tool call actually returned.

    `not_applicable` when the trial made no tool call at all, there is nothing surfaced to score.
    `unknown` when every tool call failed or was refused, something was attempted but nothing the
    model could have cited was ever delivered.
    """

    status: MetricStatus
    metrics: RankedMetrics | None = None


def surfaced_source_metrics(
    tool_calls: Sequence[ToolCallTrace],
    expected: Collection[str],
    *,
    collection: str,
    ks: tuple[int, ...] = DEFAULT_KS,
    granularity: Granularity = "page",
) -> SurfacedSourceMetrics:
    """Score every reference an ok tool call actually returned, in first-seen order, against the expected ids.

    Args:
        tool_calls: The trial's own tool calls, in any order (a call's own `ordinal` is not read).
        expected: The expected ids (`ExpectedQa.expected_source_ids`).
        collection: The knowledge base `returned_unit_refs` resolve within, to recover their bare
            unit ids off the collection qualification they are stored under.
        ks: The recall/precision cutoffs to report.
        granularity: `"page"` (the default) collapses a surfaced reference to its page. `"unit"`
            scores the exact reference a call returned.

    Returns:
        `not_applicable` with no calls at all, `unknown` when every call failed or was refused,
        otherwise `measured` with the surfaced layer's `RankedMetrics`.
    """
    if not tool_calls:
        return SurfacedSourceMetrics(status="not_applicable")
    ok_calls = [call for call in tool_calls if call.status == "ok"]
    if not ok_calls:
        return SurfacedSourceMetrics(status="unknown")
    surfaced = _dedup_ordered(bare_unit_id(ref, collection) for call in ok_calls for ref in call.returned_unit_refs)
    got = normalize_refs(surfaced, granularity)
    want = set(normalize_refs(tuple(expected), granularity))
    return SurfacedSourceMetrics(status="measured", metrics=ranked_metrics(got, want, ks=ks))


class SearchCallRanking(EvaluationModel):
    """One `search` call's own raw ranking, kept apart from every other call in the trial.

    `unknown` when the call was not `ok`, stored no evidence, or the reader refused its payload.
    """

    ordinal: int
    status: MetricStatus
    metrics: RankedMetrics | None = None


class SearchRankingRollup(EvaluationModel):
    """The ranking layer's trial-level scalar: `ranking_recall@{k}` / `ranking_precision@{k}` / `ranking_mrr`.

    `not_applicable` when the trial made no `search` call at all. `unknown` when it made calls but
    none reached `measured`. `measured` otherwise, `metrics` the mean of every measured call's own
    `RankedMetrics`, per key, over exactly those calls, a call that could not be measured
    contributing to neither the sum nor the count rather than being read as a zero.
    """

    status: MetricStatus
    metrics: RankedMetrics | None = None


class SearchRankingMetrics(EvaluationModel):
    """The ranking layer: every `search` call's own raw ranking, call boundaries never merged.

    `calls` is empty exactly when the trial made no `search` call at all (`not_applicable` at the
    trial level, never merged with a per-call `unknown`). `rollup` is the single trial-level scalar
    the namespaced `ranking_recall@{k}` / `ranking_precision@{k}` / `ranking_mrr` keys name, the mean
    over the calls that reached `measured`.
    """

    calls: tuple[SearchCallRanking, ...] = ()

    @property
    def rollup(self) -> SearchRankingRollup:
        """The trial-level scalar the namespaced ranking keys name, folded from `calls`.

        It is `not_applicable` when `calls` is empty, and `unknown` when no call reached `measured`.
        Otherwise it is `measured`, with `metrics` the mean of every measured call's own `RankedMetrics`
        over the one `k` set every call shares.
        """
        if not self.calls:
            return SearchRankingRollup(status="not_applicable")
        measured = [call.metrics for call in self.calls if call.status == "measured" and call.metrics is not None]
        if not measured:
            return SearchRankingRollup(status="unknown")
        ks = tuple(measured[0].recall)
        return SearchRankingRollup(
            status="measured",
            metrics=RankedMetrics(
                recall={k: sum(metrics.recall[k] for metrics in measured) / len(measured) for k in ks},
                precision={k: sum(metrics.precision[k] for metrics in measured) / len(measured) for k in ks},
                reciprocal_rank=sum(metrics.reciprocal_rank for metrics in measured) / len(measured),
            ),
        )


class LayeredEvidenceMetrics(EvaluationModel):
    """One trial's three evidence layers, each computed from its own explicit input."""

    citation: CitationSourceMetrics
    surfaced: SurfacedSourceMetrics
    ranking: SearchRankingMetrics
