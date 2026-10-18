"""The recommendation's three structural detectors, each turning a diagnosis into a typed signal.

They do no IO. Each takes the query results the caller fetched instead of a port.

* `detect_section_splits` fires when a failing case's gold pages carry over-budget sections, grouped by page type.
  It is the signal behind a `restructure_sections` proposal.
* `detect_missing_edges` fires when a failing trace or survey case has gold pages that cannot reach each other in the
  graph. It carries the unlifted prose links that would connect them and an optional reachability counterfactual.
  It is the signal behind an `add_relation` proposal.
* `detect_over_expansion` fires when cases cite far more pages than their gold with low precision and many tool calls.
  It is a machinery finding rather than a typed op.

A case is failing when its recall is below `recall_threshold`, so a perfect-recall case never contributes. Cases are
identified by their `question`, the only identifier `FailingCase` carries, so every signal's `case_names` holds question
text, not dataset case names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from engram.evals.rca.contracts import Diagnosis
from engram.evals.recommendation.domain.recommendation import CounterfactualResult, MinedLink
from engram.evals.shared.contracts import PRECISION_PREFIX, Archetype, primary_at_k_key

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from engram.evals.rca.contracts import FailingCase

type EvidenceGrade = Literal["residual-of-tuned", "raw-baseline"]
"""The residual-first evidence grade a run carries: a supplied policy versus the raw baseline."""

# -- the typed structural signals --------------------------------------------


class SectionStat(BaseModel):
    """One over-budget section, as read off `kb.sections`.

    Attributes:
        page_id: The page the section belongs to.
        canon: The section's canonical slug.
        tokens: The section's token count, over the retrieval-unit budget.
    """

    model_config = ConfigDict(frozen=True)

    page_id: str
    canon: str
    tokens: int


class SectionSplitSignal(BaseModel):
    """A page type whose gold pages of failing cases carry over-budget sections.

    Attributes:
        page_type: The page type the offending sections belong to. JSON carries it under the key `kind`.
        case_names: The failing cases (by question) whose gold pages are over budget.
        offenders: The over-budget sections on those gold pages, ordered by page id then slug.
    """

    model_config = ConfigDict(frozen=True, serialize_by_alias=True)

    page_type: str = Field(validation_alias=AliasChoices("kind", "page_type"), serialization_alias="kind")
    case_names: tuple[str, ...]
    offenders: tuple[SectionStat, ...]


class PairReachability(BaseModel):
    """One gold pair and whether it is reachable in the graph, the injected `reachable_pairs` result.

    The handler zips the gateway's `reachable_pairs` booleans back onto the pairs it asked about and
    feeds the result to `detect_missing_edges`.

    Attributes:
        src_id: The source gold page id.
        dst_id: The destination gold page id.
        reachable: Whether the destination is reachable from the source within the hop horizon.
    """

    model_config = ConfigDict(frozen=True)

    src_id: str
    dst_id: str
    reachable: bool


class MissingEdgeSignal(BaseModel):
    """Failing trace or survey cases whose gold pairs cannot reach each other in the graph.

    Attributes:
        case_names: The failing cases (by question) that own an unreachable gold pair.
        unreachable_pairs: The gold pairs found unreachable within the hop horizon.
        mined_candidates: The unlifted prose links whose endpoints match an unreachable pair.
        counterfactual: The measured reachability delta from injecting the candidate edges, or None.
    """

    model_config = ConfigDict(frozen=True)

    case_names: tuple[str, ...]
    unreachable_pairs: tuple[tuple[str, str], ...]
    mined_candidates: tuple[MinedLink, ...]
    counterfactual: CounterfactualResult | None


class OverExpansionSignal(BaseModel):
    """Cases that cite far more than their gold with low precision and many tool calls.

    Attributes:
        case_names: The offending cases (by question).
        mean_cited: The mean number of cited pages over the offending cases.
        mean_gold: The mean number of gold pages over the offending cases.
        mean_tool_calls: The mean tool-call count over the offending cases.
    """

    model_config = ConfigDict(frozen=True)

    case_names: tuple[str, ...]
    mean_cited: float
    mean_gold: float
    mean_tool_calls: float


class StructuralDiagnosis(BaseModel):
    """The whole structural signal for one run: the base diagnosis plus the three detector outputs.

    Attributes:
        base: The tuning-loop `Diagnosis` the signals are read from, reused verbatim.
        evidence_grade: Whether the run was under the tuned policy or a raw baseline.
        section_splits: The section-split signals, one per page type.
        missing_edges: The missing-edge signals, at most one in v1.
        over_expansion: The over-expansion signal, or None when it does not fire.
    """

    model_config = ConfigDict(frozen=True)

    base: Diagnosis
    evidence_grade: Literal["residual-of-tuned", "raw-baseline"]
    section_splits: tuple[SectionSplitSignal, ...]
    missing_edges: tuple[MissingEdgeSignal, ...]
    over_expansion: OverExpansionSignal | None


# -- the report slice the over-expansion detector reads ----------------------


class _ScoreLike(Protocol):
    """The slice of one `EvaluationResult` the over-expansion detector needs: its scalar value."""

    @property
    def value(self) -> float | int:
        """The metric's scalar score."""
        ...


class _CitedLike(Protocol):
    """The slice of a `RetrievalAnswer` the detector needs: the ordered cited page ids."""

    @property
    def cited_source_ids(self) -> Sequence[str]:
        """The cited page ids."""
        ...


class _InputLike(Protocol):
    """The slice of a `QaInput` the detector needs: the question text used as the case identifier."""

    @property
    def question(self) -> str:
        """The case's question."""
        ...


class _ExpansionCaseLike(Protocol):
    """The slice of a report case the over-expansion detector folds over."""

    @property
    def inputs(self) -> _InputLike:
        """The case's input question."""
        ...

    @property
    def output(self) -> _CitedLike:
        """The agent-under-test's answer."""
        ...

    @property
    def expected_output(self) -> _CitedLike | None:
        """The ground-truth answer, or None when the case has no expected output."""
        ...

    @property
    def scores(self) -> Mapping[str, _ScoreLike]:
        """The case's metric scores (precision@k / n_tool_calls)."""
        ...


class _ExpansionReportLike(Protocol):
    """The slice of an evaluation report the over-expansion detector reads: its cases."""

    @property
    def cases(self) -> Sequence[_ExpansionCaseLike]:
        """The report's scored cases."""
        ...


# -- shared helpers ----------------------------------------------------------

_N_TOOL_CALLS = "n_tool_calls"


def _failing_cases(cases: Sequence[FailingCase], recall_threshold: float) -> list[FailingCase]:
    """The cases whose recall is below `recall_threshold`, the ones structural surgery targets."""
    return [case for case in cases if case.recall < recall_threshold]


def _score(case: _ExpansionCaseLike, name: str | None) -> float:
    """A single named score off the case as a float, `0.0` when absent (never raises)."""
    if name is None:
        return 0.0
    result = case.scores.get(name)
    return float(result.value) if result is not None else 0.0


# -- detectors ---------------------------------------------------------------


def detect_section_splits(
    base: Diagnosis, over_budget: Sequence[SectionStat], page_types: Mapping[str, str], *, recall_threshold: float = 1.0
) -> tuple[SectionSplitSignal, ...]:
    """Group over-budget sections on failing cases' gold pages by page type.

    An offending section counts only when it sits on a gold page of a failing case and its page's
    page type is known. Offenders are grouped by page type, and one signal is emitted per page type, ordered by
    page type, carrying the failing cases whose gold pages it covers.

    Args:
        base: The run's diagnosis, whose `worst_cases` supply the failing cases and their gold ids.
        over_budget: The over-budget sections the gateway found (already past the token budget).
        page_types: A page id to page type mapping for the over-budget pages.
        recall_threshold: A case counts as failing when its recall is below this (default 1.0).

    Returns:
        One `SectionSplitSignal` per offending page type, ordered by page type, empty when none fires.
    """
    failing = _failing_cases(base.worst_cases, recall_threshold)
    gold_ids = {gid for case in failing for gid in case.gold_ids}
    by_page_type: dict[str, list[SectionStat]] = {}
    for stat in over_budget:
        if stat.page_id not in gold_ids:
            continue
        page_type = page_types.get(stat.page_id)
        if page_type is None:
            continue
        by_page_type.setdefault(page_type, []).append(stat)
    signals: list[SectionSplitSignal] = []
    for page_type in sorted(by_page_type):
        offenders = tuple(sorted(by_page_type[page_type], key=lambda stat: (stat.page_id, stat.canon)))
        offender_pages = {stat.page_id for stat in offenders}
        case_names = tuple(case.question for case in failing if offender_pages & set(case.gold_ids))
        signals.append(SectionSplitSignal(page_type=page_type, case_names=case_names, offenders=offenders))
    return tuple(signals)


def detect_missing_edges(
    base: Diagnosis,
    reachability: Sequence[PairReachability],
    mined_links: Sequence[MinedLink],
    counterfactual: CounterfactualResult | None = None,
    *,
    recall_threshold: float = 1.0,
) -> MissingEdgeSignal | None:
    """Assemble the unreachable gold pairs of failing trace and survey cases into one signal.

    Only failing trace or survey cases are considered (the graph-shaped archetypes). A pair is kept
    when both its endpoints are gold pages of some such case and the injected reachability marks it
    unreachable. The mined links whose endpoints match an unreachable pair (in either direction, and
    only when they resolve to a page rather than dangling) become the relation candidates.

    Args:
        base: The run's diagnosis, whose `worst_cases` supply the failing cases.
        reachability: The gold pair reachability the gateway measured, one entry per pair.
        mined_links: The unlifted prose links the gateway mined for the collection.
        counterfactual: The measured reachability delta from injecting candidate edges, or None.
        recall_threshold: A case counts as failing when its recall is below this (default 1.0).

    Returns:
        A single `MissingEdgeSignal`, or None when no failing trace or survey case owns an
        unreachable gold pair.
    """
    failing = [case for case in _failing_cases(base.worst_cases, recall_threshold) if case.archetype in (Archetype.TRACE, Archetype.SURVEY)]
    if not failing:
        return None
    gold_ids = {gid for case in failing for gid in case.gold_ids}
    unreachable = tuple(
        pair
        for pair in dict.fromkeys((entry.src_id, entry.dst_id) for entry in reachability if not entry.reachable)
        if pair[0] in gold_ids and pair[1] in gold_ids
    )
    if not unreachable:
        return None
    unordered = {frozenset(pair) for pair in unreachable}
    case_names = tuple(case.question for case in failing if any({src, dst} <= set(case.gold_ids) for src, dst in unreachable))
    candidates = tuple(
        link for link in mined_links if link.dst_page_id is not None and frozenset((link.src_page_id, link.dst_page_id)) in unordered
    )
    return MissingEdgeSignal(
        case_names=case_names, unreachable_pairs=unreachable, mined_candidates=candidates, counterfactual=counterfactual
    )


def detect_over_expansion(
    report: _ExpansionReportLike, *, max_precision: float = 0.5, min_cited_ratio: float = 2.0, min_tool_calls: float = 3.0
) -> OverExpansionSignal | None:
    """Detect cases that cite far more pages than their gold with low precision and many tool calls.

    A case offends when its precision is at or below `max_precision`, it cites at least
    `min_cited_ratio` times its gold count (or cites anything at all when it has no gold), and it made
    at least `min_tool_calls` tool calls. The reported means are over the offending cases only.

    Args:
        report: The evaluation report to read (its cases carry the citations and per-case scores).
        max_precision: The precision at or below which a case counts as imprecise.
        min_cited_ratio: The cited-to-gold ratio at or above which citation counts as over-wide.
        min_tool_calls: The tool-call count at or above which a case counts as expensive.

    Returns:
        A single `OverExpansionSignal`, or None when no case offends.
    """
    cases = list(report.cases)
    precision_key = primary_at_k_key({name for case in cases for name in case.scores}, prefix=PRECISION_PREFIX)
    offenders: list[str] = []
    cited_counts: list[int] = []
    gold_counts: list[int] = []
    tool_counts: list[float] = []
    for case in cases:
        cited = len(case.output.cited_source_ids)
        gold = len(case.expected_output.cited_source_ids) if case.expected_output is not None else 0
        precision = _score(case, precision_key)
        tool_calls = _score(case, _N_TOOL_CALLS)
        over_cited = cited >= gold * min_cited_ratio if gold else cited > 0
        if precision <= max_precision and over_cited and tool_calls >= min_tool_calls:
            offenders.append(case.inputs.question)
            cited_counts.append(cited)
            gold_counts.append(gold)
            tool_counts.append(tool_calls)
    if not offenders:
        return None
    return OverExpansionSignal(
        case_names=tuple(offenders),
        mean_cited=sum(cited_counts) / len(cited_counts),
        mean_gold=sum(gold_counts) / len(gold_counts),
        mean_tool_calls=sum(tool_counts) / len(tool_counts),
    )
