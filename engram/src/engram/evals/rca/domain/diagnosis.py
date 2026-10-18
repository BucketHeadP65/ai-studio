"""Fold one evaluation run report into a structured `Diagnosis`: where the agent under test is weak.

`diagnose` is a pure function over a thin, `Protocol`-typed slice of the report. It reads `.cases` and, per case,
the name, inputs, metadata, expected output, output, scores, metrics and duration.
The diagnosis holds the per-metric means (`overall`), one `BucketMetrics` per `(archetype, difficulty)` slice,
the lowest-recall `worst_cases` with their gold and cited ids, the mean duration and the best-effort mean total tokens.
Each case also gets a retrieval-layer error type, counted per bucket (`error_counts`) and run-wide (`error_totals`).
Only a bare `recall@` or `precision@` score key is the headline, while `overall` and `BucketMetrics.metrics` average
every score name, layer-prefixed or not.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from engram.evals.shared.contracts import PRECISION_PREFIX, RECALL_PREFIX, Archetype, QaDifficulty, RatedDifficulty, primary_at_k_key

if TYPE_CHECKING:
    from collections.abc import Sequence


# -- the report slice this reader depends on ---------------------------------


class _ScoreLike(Protocol):
    """The slice of one `EvaluationResult` this reader needs: its scalar `value`."""

    @property
    def value(self) -> float | int:
        """The metric's scalar score."""
        ...


class _AnswerLike(Protocol):
    """The slice of a `RetrievalAnswer` this reader needs: the ordered cited page ids."""

    @property
    def cited_source_ids(self) -> Sequence[str]:
        """The agent's (or ground truth's) ordered cited page ids."""
        ...


class _MetaLike(Protocol):
    """The slice of a `QaCaseMeta` this reader needs: the bucketing labels."""

    @property
    def archetype(self) -> Archetype | None:
        """The case's structural archetype, or `None` for an unlabelled case."""
        ...

    @property
    def difficulty(self) -> QaDifficulty | None:
        """The case's difficulty band, or `None` for a human-judged case."""
        ...


class _InputsLike(Protocol):
    """The slice of a `QaInput` this reader needs: the question text."""

    @property
    def question(self) -> str:
        """The case's question."""
        ...


class CaseLike(Protocol):
    """The slice of one scored case of an evaluation report that `diagnose` reads."""

    @property
    def name(self) -> str:
        """The case's dataset-assigned name, the identity score pairing joins on."""
        ...

    @property
    def inputs(self) -> _InputsLike:
        """The case's input question."""
        ...

    @property
    def metadata(self) -> _MetaLike | None:
        """The case's metadata, or `None` when the dataset attached none."""
        ...

    @property
    def expected_output(self) -> _AnswerLike | None:
        """The ground-truth answer, or `None` when the case has no expected output."""
        ...

    @property
    def output(self) -> _AnswerLike:
        """The agent-under-test's answer."""
        ...

    @property
    def scores(self) -> Mapping[str, _ScoreLike]:
        """The case's metric scores (recall@k / precision@k / mrr / n_tool_calls)."""
        ...

    @property
    def metrics(self) -> Mapping[str, float | int]:
        """The case's span-derived metrics (token usage / cost / request counts)."""
        ...

    @property
    def total_duration(self) -> float:
        """The case's wall-clock duration in seconds (task plus evaluators)."""
        ...


class ReportLike(Protocol):
    """The slice of an `EvaluationReport` this reader folds over: its cases."""

    @property
    def cases(self) -> Sequence[CaseLike]:
        """The report's scored cases."""
        ...


# -- the error-type taxonomy (retrieval-layer only) --------------------------

ErrorType = Literal["gold_missed", "gold_buried", "over_fetch", "abandoned", "none"]
Surface = Literal["S1", "S2"]  # Deterministic routing emits only S1 and S2. S3 attribution is the recommendation's.

# The taxonomy's members in a stable order, so a bucket's `error_counts` always carries every key.
_ERROR_TYPES: tuple[ErrorType, ...] = ("gold_missed", "gold_buried", "over_fetch", "abandoned", "none")

_BURIED_RANK = 5  # first gold cited after this rank => gold_buried
_OVER_FETCH_CALLS = 8  # recall complete but tool calls above this => over_fetch
_ABANDONED_CALLS = 1  # at most this many tool calls with no citations => abandoned
_FULL_RECALL = 1.0  # recall counts as complete at or above this
_MIN_PRECISION = 1 / 3  # recall complete but precision below this => over_fetch


def _gold_rank(gold: Sequence[str], cited: Sequence[str]) -> int | None:
    """The 1-based rank of the first cited id that is a gold id, or `None` when none is cited."""
    wanted = set(gold)
    for rank, cited_id in enumerate(cited, start=1):
        if cited_id in wanted:
            return rank
    return None


def classify_case(*, gold: Sequence[str], cited: Sequence[str], recall: float, precision: float, n_tool_calls: float) -> ErrorType:
    """Classify one case's retrieval outcome into a deterministic error type.

    The taxonomy is retrieval-layer only (the answer layer needs a judge and stays unmeasured). The
    checks run in trajectory order so the first match wins:

    * `abandoned` when the agent cited nothing after at most 1 tool call (it gave up before retrieving).
    * `gold_missed` when gold was expected but no gold id was cited at all.
    * `gold_buried` when a gold id was cited but the first one lands after rank 5.
    * `over_fetch` when recall is complete (1.0) yet the run paid for it, either above 8 tool calls
      or below one third precision.
    * `none` when no failure signal fires.

    Args:
        gold: The expected page ids.
        cited: The agent's cited page ids in retrieval order.
        recall: The case's primary `recall@k` score.
        precision: The case's primary `precision@k` score (`0.0` when the run reported none, the
            file's absent-metric convention).
        n_tool_calls: How many tool calls the run made.

    Returns:
        The matching `ErrorType`.
    """
    if not cited and n_tool_calls <= _ABANDONED_CALLS:
        return "abandoned"
    rank = _gold_rank(gold, cited)
    if gold and rank is None:
        return "gold_missed"
    if rank is not None and rank > _BURIED_RANK:
        return "gold_buried"
    if recall >= _FULL_RECALL and (n_tool_calls > _OVER_FETCH_CALLS or precision < _MIN_PRECISION):
        return "over_fetch"
    return "none"


def route_surface(error_type: ErrorType) -> Surface:
    """Route an error type to its primary corrective surface.

    Deterministic routing emits only S1 (retrieval instructions) and S2 (retrieval config). S3
    (schema and ingestion) attribution is the reflective recommendation's, never the router's.
    `abandoned` is a query-policy failure so it routes to S1, and every other error type (including
    the non-failure `none`) routes to S2.

    Args:
        error_type: The classified error type.

    Returns:
        The primary surface, `S1` or `S2`.
    """
    return "S1" if error_type == "abandoned" else "S2"


# -- the structured diagnosis the proposer reasons about ---------------------


class BucketMetrics(BaseModel):
    """The mean metrics for one `(archetype, difficulty)` slice of a run.

    `metrics` holds every score name the bucket's cases share, each averaged over the bucket.

    Attributes:
        archetype: The bucket's structural archetype, or `None` for unlabelled cases.
        difficulty: The bucket's difficulty band.
        n_cases: How many cases fell in the bucket.
        metrics: Each metric name mapped to its mean over the bucket's cases.
        error_counts: Each `ErrorType` mapped to how many of the bucket's cases classified as it,
            every taxonomy key present (zero when unseen). Empty when built outside `diagnose`.
    """

    model_config = ConfigDict(frozen=True)

    archetype: Archetype | None
    difficulty: RatedDifficulty
    n_cases: int
    metrics: Mapping[str, float]
    error_counts: Mapping[ErrorType, int] = Field(default_factory=dict)


class FailingCase(BaseModel):
    """One weak case for the proposer to inspect: its gold vs cited ids and headline scores.

    Attributes:
        question: The case's question.
        gold_ids: The expected page ids (the ground truth's `cited_source_ids`), empty when the
            case had no expected output.
        cited_ids: The page ids the agent actually cited.
        recall: The case's primary recall score.
        n_tool_calls: How many tool calls the run made (from the case's `n_tool_calls` score).
        archetype: The case's structural archetype, or `None` when unlabelled.
        difficulty: The case's difficulty band.
        error_type: The case's deterministic retrieval-layer class. `none` when built outside
            `diagnose`.
        surface: The primary corrective surface `error_type` routes to (`S1` or `S2`).
        gold_rank: The 1-based rank of the first gold id among `cited_ids`, or `None` when no gold
            id was cited.
    """

    model_config = ConfigDict(frozen=True)

    question: str
    gold_ids: tuple[str, ...]
    cited_ids: tuple[str, ...]
    recall: float
    n_tool_calls: float
    archetype: Archetype | None
    difficulty: RatedDifficulty
    error_type: ErrorType = "none"
    surface: Surface = "S2"
    gold_rank: int | None = None


class Diagnosis(BaseModel):
    """The whole error signal for one run: overall average, per-bucket means, worst cases, cost.

    Attributes:
        overall: The per-metric mean across all cases (the run's headline aggregate).
        buckets: The per-`(archetype, difficulty)` means, in a stable difficulty-then-archetype order.
        worst_cases: The lowest-recall cases (ascending, stable on ties), capped at the requested count.
        mean_duration_s: The mean per-case wall-clock duration in seconds.
        mean_total_tokens: The mean per-case total token usage, or `None` when the report exposes
            no per-case token metrics (the reader never invents a token source).
        error_totals: Each `ErrorType` mapped to how many cases across the whole run classified as
            it, every taxonomy key present (zero when unseen, including an empty report). Empty when
            built outside `diagnose`.
    """

    model_config = ConfigDict(frozen=True)

    overall: Mapping[str, float]
    buckets: tuple[BucketMetrics, ...]
    worst_cases: tuple[FailingCase, ...]
    mean_duration_s: float
    mean_total_tokens: float | None
    error_totals: Mapping[ErrorType, int] = Field(default_factory=dict)


# -- case-score access -------------------------------------------------------

_N_TOOL_CALLS = "n_tool_calls"


def _case_metric(case: CaseLike, name: str) -> float:
    """A single named score off the case as a float, `0.0` when absent (never raises)."""
    score = case.scores.get(name)
    return float(score.value) if score is not None else 0.0


def _case_recall(case: CaseLike, recall_key: str | None) -> float:
    """The case's primary recall score, `0.0` when no recall metric is present."""
    return _case_metric(case, recall_key) if recall_key is not None else 0.0


def _case_precision(case: CaseLike, precision_key: str | None) -> float:
    """The case's primary precision score, `0.0` when no precision metric is present."""
    return _case_metric(case, precision_key) if precision_key is not None else 0.0


# -- aggregation helpers -----------------------------------------------------


def _mean_scores(cases: Sequence[CaseLike]) -> dict[str, float]:
    """The per-metric mean over `cases`, keys sorted for a stable mapping."""
    totals: dict[str, float] = {}
    counts: dict[str, int] = {}
    for case in cases:
        for name, result in case.scores.items():
            totals[name] = totals.get(name, 0.0) + float(result.value)
            counts[name] = counts.get(name, 0) + 1
    return {name: totals[name] / counts[name] for name in sorted(totals)}


def _bucket_key(case: CaseLike) -> tuple[RatedDifficulty, str, Archetype | None]:
    """A stable sort key for a case's bucket: difficulty band, then archetype (`None` first)."""
    metadata = case.metadata
    difficulty: RatedDifficulty = (metadata.difficulty or "unrated") if metadata is not None else "medium"
    archetype = metadata.archetype if metadata is not None else None
    return (difficulty, "" if archetype is None else archetype.value, archetype)


def _case_gold_cited(case: CaseLike) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """The case's `(gold_ids, cited_ids)`, gold empty when the case had no expected output."""
    gold = case.expected_output.cited_source_ids if case.expected_output is not None else ()
    return tuple(gold), tuple(case.output.cited_source_ids)


def _case_error_type(case: CaseLike, *, recall_key: str | None, precision_key: str | None) -> ErrorType:
    """Classify one case by folding its citations and headline scores through `classify_case`."""
    gold, cited = _case_gold_cited(case)
    return classify_case(
        gold=gold,
        cited=cited,
        recall=_case_recall(case, recall_key),
        precision=_case_precision(case, precision_key),
        n_tool_calls=_case_metric(case, _N_TOOL_CALLS),
    )


def _error_counts(cases: Sequence[CaseLike], *, recall_key: str | None, precision_key: str | None) -> dict[ErrorType, int]:
    """The per-`ErrorType` case count over `cases`, every taxonomy key present (zero when unseen)."""
    counts: dict[ErrorType, int] = dict.fromkeys(_ERROR_TYPES, 0)
    for case in cases:
        counts[_case_error_type(case, recall_key=recall_key, precision_key=precision_key)] += 1
    return counts


def _buckets(cases: Sequence[CaseLike], *, recall_key: str | None, precision_key: str | None) -> tuple[BucketMetrics, ...]:
    """Group cases by `(archetype, difficulty)` in difficulty-then-archetype order and mean each metric per bucket."""
    grouped: dict[tuple[RatedDifficulty, str, Archetype | None], list[CaseLike]] = {}
    for case in cases:
        grouped.setdefault(_bucket_key(case), []).append(case)
    return tuple(
        BucketMetrics(
            archetype=archetype,
            difficulty=difficulty,
            n_cases=len(bucket_cases),
            metrics=_mean_scores(bucket_cases),
            error_counts=_error_counts(bucket_cases, recall_key=recall_key, precision_key=precision_key),
        )
        for (difficulty, _value, archetype), bucket_cases in sorted(grouped.items(), key=lambda item: item[0][:2])
    )


def _failing_case(case: CaseLike, *, recall_key: str | None, precision_key: str | None) -> FailingCase:
    """Build a `FailingCase` from one case's inputs, citations, scores, error type, and gold rank."""
    metadata = case.metadata
    gold, cited = _case_gold_cited(case)
    error_type = _case_error_type(case, recall_key=recall_key, precision_key=precision_key)
    return FailingCase(
        question=case.inputs.question,
        gold_ids=gold,
        cited_ids=cited,
        recall=_case_recall(case, recall_key),
        n_tool_calls=_case_metric(case, _N_TOOL_CALLS),
        archetype=metadata.archetype if metadata is not None else None,
        difficulty=(metadata.difficulty or "unrated") if metadata is not None else "medium",
        error_type=error_type,
        surface=route_surface(error_type),
        gold_rank=_gold_rank(gold, cited),
    )


def _worst_cases(cases: Sequence[CaseLike], *, worst_n: int, recall_key: str | None, precision_key: str | None) -> tuple[FailingCase, ...]:
    """The `worst_n` lowest-recall cases as `FailingCase`s, ascending, with ties kept in report order."""
    ranked = sorted(cases, key=lambda case: _case_recall(case, recall_key))
    return tuple(_failing_case(case, recall_key=recall_key, precision_key=precision_key) for case in ranked[:worst_n])


def _mean_duration(cases: Sequence[CaseLike]) -> float:
    """The mean per-case wall-clock duration, `0.0` for an empty report (no division by zero)."""
    if not cases:
        return 0.0
    return sum(case.total_duration for case in cases) / len(cases)


def _case_total_tokens(case: CaseLike) -> float | None:
    """One case's total token usage, summing its `*_tokens` metrics, or `None` when it has none."""
    token_values = [float(value) for name, value in case.metrics.items() if name.endswith("_tokens")]
    return sum(token_values) if token_values else None


def _mean_total_tokens(cases: Sequence[CaseLike]) -> float | None:
    """The mean total token usage over the cases that report tokens, or `None` when none does."""
    per_case = [tokens for case in cases if (tokens := _case_total_tokens(case)) is not None]
    if not per_case:
        return None
    return sum(per_case) / len(per_case)


def diagnose(report: ReportLike, *, worst_n: int = 5) -> Diagnosis:
    """Fold an eval report into the structured tuning signal the proposer reasons about.

    Pure: reads only the report's cases (and a thin slice of each), computes the per-run `overall`
    average, the per-`(archetype, difficulty)` `buckets`, the lowest-recall `worst_cases`, a
    best-effort cost summary, and the per-`ErrorType` `error_totals`, and returns them as a frozen
    `Diagnosis`. An empty report yields a well-formed empty diagnosis (empty `overall` / `buckets` /
    `worst_cases`, `0.0` duration, `None` tokens, an all-zero `error_totals`) rather than raising.

    Args:
        report: The evaluation report to read (its cases carry the per-case scores, metadata, and
            citations the signal is built from).
        worst_n: How many lowest-recall cases to surface in `worst_cases`.

    Returns:
        The structured `Diagnosis` for the run.
    """
    cases = list(report.cases)
    score_names = {name for case in cases for name in case.scores}
    recall_key = primary_at_k_key(score_names, prefix=RECALL_PREFIX)
    precision_key = primary_at_k_key(score_names, prefix=PRECISION_PREFIX)
    return Diagnosis(
        overall=_mean_scores(cases),
        buckets=_buckets(cases, recall_key=recall_key, precision_key=precision_key),
        worst_cases=_worst_cases(cases, worst_n=worst_n, recall_key=recall_key, precision_key=precision_key),
        mean_duration_s=_mean_duration(cases),
        mean_total_tokens=_mean_total_tokens(cases),
        error_totals=_error_counts(cases, recall_key=recall_key, precision_key=precision_key),
    )
