"""Two evaluation runs of one dataset paired case by case, their strict accuracies compared, and the markdown report.

A case is correct when its answer matched strictly and it did not fail. The difference is the candidate's strict
accuracy minus the baseline's, with a normal 95 percent interval over the per-case differences. McNemar's exact
two-sided test reads the cases only one run answered correctly. Holm's method adjusts the p values across the pairs of
one call, over all cases and within each query type. Each side also carries its failed cases, its mean source-level
retrieval and its mean seen recall, with the paired difference of the seen recalls.
"""

from __future__ import annotations

import math

from collections import defaultdict
from typing import TYPE_CHECKING, Self

from core.thinking import Thinking
from pydantic import BaseModel, ConfigDict, Field, model_validator

from engram.evals.outcomes.contracts import graph_pair_verdict
from engram.evals.paired_comparison.exceptions import RunsNotComparableError
from engram.evals.tuning.contracts import RetrievalStrategy

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence

INTERVAL_Z = 1.96
"""The normal quantile the 95 percent interval of a paired difference spans on either side of the mean."""

_SHOWN_CASES = 5
"""How many case names a refusal lists before it counts the rest."""

_MISSING = "missing"
"""What a report cell shows for a seen recall the results file does not hold."""


class ResultsHeader(BaseModel):
    """How one evaluation run was set up, as the first line of its results file records it."""

    model_config = ConfigDict(frozen=True)

    strategy: RetrievalStrategy
    """The retrieval strategy the agent under test ran under."""
    sql: bool
    """Whether the agent under test had the SQL tools."""
    code_mode: bool
    """Whether the agent under test drove its tools through the sandboxed `run_code` tool."""
    model: str
    """The model under test, as the run named it."""
    thinking: Thinking | None
    """The thinking level the model under test was built at, or None for the model's own default."""
    instructions_sha256: str
    """The SHA-256 of the agent's instructions, in hex."""
    max_tool_calls: int | None
    """The most tool calls one case could make, or None for no limit."""
    pinned_search: bool
    """Whether `search` was pinned to the knowledge base's own collection."""
    search_date_range: bool
    """Whether `search` took a date range, `since` and `until`."""
    dataset: str
    """The dataset's name."""
    dataset_sha256: str
    """The SHA-256 of the dataset's cases, which identifies the dataset."""
    collection: str
    """The knowledge base the run read."""
    knowledge_base_fingerprint: str
    """The knowledge base's index fingerprint and its name."""
    index_build: str
    """The index build the run read, its indexer version, embedding model, vocabulary and switches."""
    started: str
    """When the run started, as an ISO 8601 time with its offset."""
    source_lookup_error: str | None
    """What the lookup of the cited pages' sources raised, or None when it succeeded or the run mapped no sources."""


class ResultsLine(BaseModel):
    """One case of an evaluation run as its results file records it, with only the keys a paired comparison reads."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    case: str
    """The case's name, which pairs it with the same case of the other run."""
    query_type: str | None
    """The case's query type, or None when the dataset gives it none."""
    strict: bool
    """Whether the answer matched the expected answer strictly."""
    official: bool
    """Whether the answer shared a word with the expected answer."""
    failed: bool
    """Whether the case raised instead of answering. A failed case counts as incorrect."""
    sources: tuple[str, ...] | None
    """The source ids the case's citations map to, or None when the run mapped none, so its scores are over page ids."""
    recall_at_4: float | None = Field(validation_alias="recall@4")
    """Recall at 4 against the case's gold ids, or None for a case with no gold ids. The file's key is `recall@4`."""
    recall_at_10: float | None = Field(validation_alias="recall@10")
    """Recall at 10 against the case's gold ids, or None for a case with no gold ids. The file's key is `recall@10`."""
    mrr: float | None
    """The reciprocal rank of the case's first gold id, or None for a case with no gold ids."""
    other_collection: bool
    """Whether a row the case's SQL statements returned from a view of knowledge base content named another knowledge base."""
    seen_recall: float | None
    """The share of the case's gold sources its tool results named, or None for a case with no gold ids or a run that mapped no
    sources."""


class ResultsFile(BaseModel):
    """One evaluation run's results file: where it was read from, its header and its case lines in file order.

    Each case appears once. A results file that repeats a case is refused.
    """

    model_config = ConfigDict(frozen=True)

    path: str
    """The file's path, as the caller named it."""
    header: ResultsHeader
    lines: tuple[ResultsLine, ...]

    @model_validator(mode="after")
    def each_case_once(self) -> Self:
        """Refuse a results file that repeats a case.

        Returns:
            The results file itself, unchanged.

        Raises:
            ValueError: When two lines name the same case. The message names the case.
        """
        seen: set[str] = set()
        for line in self.lines:
            if line.case in seen:
                msg = f"the results file {self.path} repeats the case {line.case}"
                raise ValueError(msg)
            seen.add(line.case)
        return self


class SourceLevelRetrieval(BaseModel):
    """One run's mean source-level retrieval over its cases scored against gold source ids."""

    model_config = ConfigDict(frozen=True)

    cases: int
    """How many cases were scored: those whose citations the run mapped to sources and that have gold ids."""
    recall_at_4: float
    """The mean recall at 4, from 0 to 1."""
    recall_at_10: float
    """The mean recall at 10, from 0 to 1."""
    mrr: float
    """The mean reciprocal rank of the first gold source, from 0 to 1."""
    seen_recall: float | None = None
    """The mean seen recall over the scored cases whose lines carry one, from 0 to 1, or None when none does."""


class QueryTypeRetrieval(BaseModel):
    """One query type's mean source-level retrieval in one run."""

    model_config = ConfigDict(frozen=True)

    query_type: str | None
    """The query type, or None for the cases with none."""
    retrieval: SourceLevelRetrieval


class PairedScores(BaseModel):
    """Two runs' scores over the cases they share, either all of them or one query type's."""

    model_config = ConfigDict(frozen=True)

    cases: int
    """How many cases were paired."""
    baseline_strict: float
    """The baseline's strict accuracy, from 0 to 1."""
    candidate_strict: float
    """The candidate's strict accuracy, from 0 to 1."""
    difference: float
    """The candidate's strict accuracy minus the baseline's."""
    interval: tuple[float, float] | None
    """The low and high ends of the difference's 95 percent interval, or None for a single case."""
    baseline_only: int
    """How many cases only the baseline answered correctly."""
    candidate_only: int
    """How many cases only the candidate answered correctly."""
    p_value: float
    """McNemar's exact two-sided p value over the cases only one run answered correctly."""
    holm_p_value: float
    """The p value adjusted by Holm's method across the pairs of the call, within the same query type for a query type's scores."""
    baseline_official: float
    """The baseline's official accuracy, from 0 to 1."""
    candidate_official: float
    """The candidate's official accuracy, from 0 to 1."""
    baseline_other_collection: int
    """How many of the baseline's cases read rows naming another knowledge base."""
    candidate_other_collection: int
    """How many of the candidate's cases read rows naming another knowledge base."""
    baseline_failed: int
    """How many of the baseline's cases failed."""
    candidate_failed: int
    """How many of the candidate's cases failed."""
    baseline_retrieval: SourceLevelRetrieval | None
    """The baseline's mean source-level retrieval over these cases, or None when it scored none at the source level."""
    candidate_retrieval: SourceLevelRetrieval | None
    """The candidate's mean source-level retrieval over these cases, or None when it scored none at the source level."""
    seen_recall_difference: float | None
    """The mean over the cases both runs scored a seen recall for of the candidate's minus the baseline's, or None when no case has both."""


class QueryTypeScores(BaseModel):
    """Two runs' scores over the cases of one query type."""

    model_config = ConfigDict(frozen=True)

    query_type: str | None
    """The query type, or None for the cases with none."""
    scores: PairedScores


class PairedComparison(BaseModel):
    """Two evaluation runs of one dataset compared case by case, over all their cases and per query type."""

    model_config = ConfigDict(frozen=True)

    baseline: str
    """The baseline's results file, as the caller named it."""
    candidate: str
    """The candidate's results file, as the caller named it."""
    baseline_header: ResultsHeader
    candidate_header: ResultsHeader
    overall: PairedScores
    """The scores over every paired case."""
    query_types: tuple[QueryTypeScores, ...]
    """The scores per query type, ordered by name, with the cases that have none last."""


type _CasePair = tuple[ResultsLine, ResultsLine]


def mcnemar_exact_p(baseline_only: int, candidate_only: int) -> float:
    """McNemar's exact two-sided p value: twice the binomial tail of the smaller discordant count, at most 1.

    Args:
        baseline_only: How many cases only the baseline answered correctly.
        candidate_only: How many cases only the candidate answered correctly.

    Returns:
        The p value, 1 when the two counts are equal.
    """
    discordant = baseline_only + candidate_only
    tail = sum(math.comb(discordant, successes) for successes in range(min(baseline_only, candidate_only) + 1))
    return min(1.0, 2 * tail / 2**discordant)


def difference_interval(differences: Sequence[int]) -> tuple[float, float] | None:
    """The 95 percent interval of the mean per-case difference: the mean plus or minus `INTERVAL_Z` standard errors.

    The standard error is the sample standard deviation of the differences over the square root of their count.

    Args:
        differences: Each case's candidate correctness minus its baseline correctness, so 1, 0 or -1.

    Returns:
        The low and high ends, or None for fewer than two cases.
    """
    count = len(differences)
    if count < 2:  # noqa: PLR2004 (a sample standard deviation needs two values)
        return None
    mean = sum(differences) / count
    variance = sum((difference - mean) ** 2 for difference in differences) / (count - 1)
    half_width = INTERVAL_Z * math.sqrt(variance / count)
    return mean - half_width, mean + half_width


def holm_adjusted(p_values: Sequence[float]) -> tuple[float, ...]:
    """Holm's step-down adjustment of one family of p values, returned in the order given.

    The p value ranked `i` from the smallest, counting from 0, is multiplied by the family's size minus `i`, raised to
    the largest adjusted value ranked before it, and capped at 1.

    Args:
        p_values: The p values of one family of tests.

    Returns:
        One adjusted p value per value of `p_values`, in its order.
    """
    size = len(p_values)
    adjusted = [0.0] * size
    largest = 0.0
    for rank, index in enumerate(sorted(range(size), key=lambda position: p_values[position])):
        largest = max(largest, min(1.0, (size - rank) * p_values[index]))
        adjusted[index] = largest
    return tuple(adjusted)


def source_level_retrieval(lines: Iterable[ResultsLine]) -> SourceLevelRetrieval | None:
    """The mean recall@4, recall@10, MRR and seen recall over the lines scored against gold source ids.

    A line counts when its run mapped its citations to sources and its case has gold ids. A failed case counts with
    the zero scores its line carries, and with the seen recall of what its tool results named before it failed. The
    seen recall is the mean over the counted lines that carry one, so a run whose lines carry none has none rather than
    zero.

    Args:
        lines: The case lines of one run, all of them or any subset.

    Returns:
        The means and how many lines they cover, or None when no line counts.
    """
    scored = [
        (line.recall_at_4, line.recall_at_10, line.mrr, line.seen_recall)
        for line in lines
        if line.sources is not None and line.recall_at_4 is not None and line.recall_at_10 is not None and line.mrr is not None
    ]
    if not scored:
        return None
    count = len(scored)
    seen = [seen_recall for _, _, _, seen_recall in scored if seen_recall is not None]
    return SourceLevelRetrieval(
        cases=count,
        recall_at_4=sum(recall_at_4 for recall_at_4, _, _, _ in scored) / count,
        recall_at_10=sum(recall_at_10 for _, recall_at_10, _, _ in scored) / count,
        mrr=sum(mrr for _, _, mrr, _ in scored) / count,
        seen_recall=sum(seen) / len(seen) if seen else None,
    )


def source_level_retrieval_per_query_type(lines: Iterable[ResultsLine]) -> tuple[QueryTypeRetrieval, ...]:
    """Each query type's mean source-level retrieval, as `source_level_retrieval` scores it over that type's lines.

    Args:
        lines: The case lines of one run.

    Returns:
        One row per query type with a scored line, ordered by name, with the cases that have none last.
    """
    groups: dict[str | None, list[ResultsLine]] = defaultdict(list)
    for line in lines:
        groups[line.query_type].append(line)
    rows = (
        (query_type, source_level_retrieval(group))
        for query_type, group in sorted(groups.items(), key=lambda item: (item[0] is None, item[0] or ""))
    )
    return tuple(QueryTypeRetrieval(query_type=query_type, retrieval=retrieval) for query_type, retrieval in rows if retrieval is not None)


def paired_comparisons(pairs: Sequence[tuple[ResultsFile, ResultsFile]]) -> tuple[PairedComparison, ...]:
    """Compare each pair of runs case by case, with Holm's method across all the pairs.

    Two runs are compared only when they agree on the dataset, the model, the thinking level, the instructions, the
    tool-call budget, the search pinning, whether the search took a date range and the knowledge base. Two runs may differ
    in the knowledge base fingerprint and in the index build. Two runs under one strategy may differ in either the SQL
    switch or code mode, never in both. Two runs under different strategies must
    share the SQL switch and code mode, and the graph verdict must call the candidate the baseline plus the graph leg.
    Every pair is checked before any is scored. A side whose source lookup failed carries no source-level retrieval.

    Args:
        pairs: Each pair's baseline run, then its candidate run.

    Returns:
        One comparison per pair, in the order given.

    Raises:
        RunsNotComparableError: When a pair's variants differ in a held field, which is named, when they differ in both
            the SQL switch and code mode, when its strategies make a pair the graph verdict calls unfair, with the
            verdict's reason, when a case is in one run only, which is named, or when a pair holds no cases.
    """
    paired = [_paired_lines(baseline, candidate) for baseline, candidate in pairs]
    by_type = [_by_query_type(lines) for lines in paired]
    overall_p = [_p_value(lines) for lines in paired]
    overall_holm = holm_adjusted(overall_p)
    type_p = [{query_type: _p_value(group) for query_type, group in groups.items()} for groups in by_type]
    type_holm = _holm_per_query_type(type_p)
    return tuple(
        PairedComparison(
            baseline=baseline.path,
            candidate=candidate.path,
            baseline_header=baseline.header,
            candidate_header=candidate.header,
            overall=_scores(lines, p_value=overall_p[index], holm_p_value=overall_holm[index], mapped=_mapped(baseline, candidate)),
            query_types=tuple(
                QueryTypeScores(
                    query_type=query_type,
                    scores=_scores(
                        group,
                        p_value=type_p[index][query_type],
                        holm_p_value=type_holm[index][query_type],
                        mapped=_mapped(baseline, candidate),
                    ),
                )
                for query_type, group in by_type[index].items()
            ),
        )
        for index, ((baseline, candidate), lines) in enumerate(zip(pairs, paired, strict=True))
    )


def paired_comparison_markdown(comparisons: Sequence[PairedComparison]) -> str:
    """The markdown report of a paired comparison: how to read it, then one section per pair.

    Args:
        comparisons: The pairs compared in one call.

    Returns:
        The report.
    """
    lines = [
        "# Paired comparison",
        "",
        (
            "A case is correct when its answer matches the expected answer strictly, and a failed case counts as incorrect. "
            "The difference is the candidate's strict accuracy minus the baseline's, in points, with a 95 percent interval: "
            f"the mean per-case difference plus or minus {INTERVAL_Z} standard errors. Baseline only and candidate only count "
            "the cases only that run answered correctly, which McNemar's exact two-sided test reads. "
            f"Holm's method adjusts the p values across the {len(comparisons)} pair(s) of this comparison, over all cases and "
            "within each query type. The other knowledge base columns count the cases whose SQL rows from a view of knowledge "
            "base content named another knowledge base, and the failed columns count the cases that raised instead of answering. "
            "The retrieval table gives each run's mean source-level recall@4, recall@10 and MRR over the cases it scored: those "
            "whose citations it mapped to sources and that have gold source ids, a failed case scoring 0. None means the run "
            "scored no case there. The seen recall table gives each run's mean share of the gold sources that any tool result "
            "the model received named, a failed case counting what it saw before it failed, and the mean per-case difference "
            "over the cases both runs scored. Missing means the run's results file holds no seen recall there, as for a run "
            "that mapped no sources, never that the run saw nothing. Each run's index build is shown beside its knowledge "
            "base fingerprint."
        ),
    ]
    for comparison in comparisons:
        lines.extend(["", *_pair_section(comparison)])
    return "\n".join(lines) + "\n"


def _paired_lines(baseline: ResultsFile, candidate: ResultsFile) -> list[_CasePair]:
    """The pair's case lines matched by case name in the baseline's order, once the two variants are checked."""
    _check_variants(baseline, candidate)
    baseline_cases = {line.case for line in baseline.lines}
    candidate_lines = {line.case: line for line in candidate.lines}
    for results, missing in (
        (baseline, [line.case for line in baseline.lines if line.case not in candidate_lines]),
        (candidate, [line.case for line in candidate.lines if line.case not in baseline_cases]),
    ):
        if missing:
            msg = f"{_named(missing)} in {results.path} only, so {baseline.path} and {candidate.path} cannot be compared case by case"
            raise RunsNotComparableError(msg)
    if not baseline.lines:
        msg = f"{baseline.path} and {candidate.path} hold no cases to compare"
        raise RunsNotComparableError(msg)
    return [(line, candidate_lines[line.case]) for line in baseline.lines]


def _check_variants(baseline: ResultsFile, candidate: ResultsFile) -> None:
    """Refuse a pair whose variants differ in a held field, or in more than one thing the comparison could attribute."""
    first, second = baseline.header, candidate.header
    held: tuple[tuple[str, str | int | None, str | int | None], ...] = (
        ("dataset_sha256", first.dataset_sha256, second.dataset_sha256),
        ("model", first.model, second.model),
        ("thinking", first.thinking, second.thinking),
        ("instructions_sha256", first.instructions_sha256, second.instructions_sha256),
        ("max_tool_calls", first.max_tool_calls, second.max_tool_calls),
        ("pinned_search", first.pinned_search, second.pinned_search),
        ("search_date_range", first.search_date_range, second.search_date_range),
        ("collection", first.collection, second.collection),
    )
    for field, baseline_value, candidate_value in held:
        if baseline_value != candidate_value:
            msg = (
                f"{baseline.path} and {candidate.path} differ in {field} ({baseline_value} and {candidate_value}), "
                "so their cases cannot be compared"
            )
            raise RunsNotComparableError(msg)
    if first.strategy == second.strategy:
        if first.sql != second.sql and first.code_mode != second.code_mode:
            msg = (
                f"{baseline.path} and {candidate.path} differ in both sql ({first.sql} and {second.sql}) and code_mode "
                f"({first.code_mode} and {second.code_mode}), so the difference cannot be put down to either"
            )
            raise RunsNotComparableError(msg)
        return
    verdict = graph_pair_verdict(first.strategy, second.strategy)
    if not verdict.fair:
        msg = f"{baseline.path} and {candidate.path} differ in strategy and the pair is not fair: {verdict.reason}"
        raise RunsNotComparableError(msg)
    for field, baseline_value, candidate_value in (("sql", first.sql, second.sql), ("code_mode", first.code_mode, second.code_mode)):
        if baseline_value != candidate_value:
            msg = (
                f"{baseline.path} and {candidate.path} differ in {field} ({baseline_value} and {candidate_value}) as well as in strategy, "
                "so the difference cannot be put down to either"
            )
            raise RunsNotComparableError(msg)


def _named(cases: Sequence[str]) -> str:
    """The cases as a refusal names them: the first few, then how many more."""
    if len(cases) == 1:
        return f"the case {cases[0]} is"
    listed = ", ".join(cases[:_SHOWN_CASES])
    if len(cases) > _SHOWN_CASES:
        listed += f" and {len(cases) - _SHOWN_CASES} more"
    return f"the cases {listed} are"


def _by_query_type(lines: Sequence[_CasePair]) -> dict[str | None, list[_CasePair]]:
    """The paired cases grouped by the baseline's query type, ordered by name, with the cases that have none last."""
    groups: dict[str | None, list[_CasePair]] = defaultdict(list)
    for baseline_line, candidate_line in lines:
        groups[baseline_line.query_type].append((baseline_line, candidate_line))
    return dict(sorted(groups.items(), key=lambda item: (item[0] is None, item[0] or "")))


def _holm_per_query_type(type_p: Sequence[Mapping[str | None, float]]) -> list[dict[str | None, float]]:
    """Each pair's p value per query type, adjusted across the pairs that hold that query type."""
    adjusted: list[dict[str | None, float]] = [{} for _ in type_p]
    for query_type in {query_type for p_values in type_p for query_type in p_values}:
        holding = [index for index, p_values in enumerate(type_p) if query_type in p_values]
        for index, p_value in zip(holding, holm_adjusted([type_p[index][query_type] for index in holding]), strict=True):
            adjusted[index][query_type] = p_value
    return adjusted


def _is_correct(line: ResultsLine) -> bool:
    """Whether the case's answer matched strictly. A failed case never is."""
    return line.strict and not line.failed


def _differences(lines: Sequence[_CasePair]) -> list[int]:
    """Each case's candidate correctness minus its baseline correctness."""
    return [int(_is_correct(candidate)) - int(_is_correct(baseline)) for baseline, candidate in lines]


def _p_value(lines: Sequence[_CasePair]) -> float:
    """McNemar's exact p value over the paired cases."""
    differences = _differences(lines)
    return mcnemar_exact_p(differences.count(-1), differences.count(1))


def _mapped(baseline: ResultsFile, candidate: ResultsFile) -> tuple[bool, bool]:
    """Whether each side's source lookup succeeded, so its lines may carry source-level scores."""
    return baseline.header.source_lookup_error is None, candidate.header.source_lookup_error is None


def _scores(lines: Sequence[_CasePair], *, p_value: float, holm_p_value: float, mapped: tuple[bool, bool]) -> PairedScores:
    """The two runs' scores over the paired cases, of which there is at least one."""
    count = len(lines)
    differences = _differences(lines)
    baseline_mapped, candidate_mapped = mapped
    seen_differences = [
        candidate.seen_recall - baseline.seen_recall
        for baseline, candidate in lines
        if baseline_mapped and candidate_mapped and baseline.seen_recall is not None and candidate.seen_recall is not None
    ]
    return PairedScores(
        cases=count,
        baseline_strict=sum(_is_correct(baseline) for baseline, _ in lines) / count,
        candidate_strict=sum(_is_correct(candidate) for _, candidate in lines) / count,
        difference=sum(differences) / count,
        interval=difference_interval(differences),
        baseline_only=differences.count(-1),
        candidate_only=differences.count(1),
        p_value=p_value,
        holm_p_value=holm_p_value,
        baseline_official=sum(baseline.official and not baseline.failed for baseline, _ in lines) / count,
        candidate_official=sum(candidate.official and not candidate.failed for _, candidate in lines) / count,
        baseline_other_collection=sum(baseline.other_collection for baseline, _ in lines),
        candidate_other_collection=sum(candidate.other_collection for _, candidate in lines),
        baseline_failed=sum(baseline.failed for baseline, _ in lines),
        candidate_failed=sum(candidate.failed for _, candidate in lines),
        baseline_retrieval=source_level_retrieval(baseline for baseline, _ in lines) if baseline_mapped else None,
        candidate_retrieval=source_level_retrieval(candidate for _, candidate in lines) if candidate_mapped else None,
        seen_recall_difference=sum(seen_differences) / len(seen_differences) if seen_differences else None,
    )


def _pair_section(comparison: PairedComparison) -> list[str]:
    """One pair's heading, its two variants, a note for each failed source lookup and its four tables."""
    baseline, candidate = comparison.baseline_header, comparison.candidate_header
    rows = [("all cases", comparison.overall), *((_query_type_label(row.query_type), row.scores) for row in comparison.query_types)]
    lookup_notes = [
        f"The {side}'s source lookup failed ({header.source_lookup_error}), so its results file holds no source-level recall."
        for side, header in (("baseline", baseline), ("candidate", candidate))
        if header.source_lookup_error is not None
    ]
    return [
        f"## {comparison.candidate} against {comparison.baseline}",
        "",
        (
            f"Dataset {baseline.dataset} ({baseline.dataset_sha256[:12]}), model {baseline.model}, {comparison.overall.cases} cases. "
            f"Baseline: {_variant(baseline)}. Candidate: {_variant(candidate)}."
        ),
        *(line for note in lookup_notes for line in ("", note)),
        "",
        "| Query type | Cases | Baseline strict | Candidate strict | Difference (95% interval) | Baseline only | Candidate only | McNemar p | Holm p |",
        "|---|---|---|---|---|---|---|---|---|",
        *(
            f"| {label} | {scores.cases} | {scores.baseline_strict:.1%} | {scores.candidate_strict:.1%} | {_difference(scores)} "
            f"| {scores.baseline_only} | {scores.candidate_only} | {scores.p_value:.4f} | {scores.holm_p_value:.4f} |"
            for label, scores in rows
        ),
        "",
        (
            "| Query type | Baseline official | Candidate official | Baseline failed | Candidate failed "
            "| Baseline other knowledge base | Candidate other knowledge base |"
        ),
        "|---|---|---|---|---|---|---|",
        *(
            f"| {label} | {scores.baseline_official:.1%} | {scores.candidate_official:.1%} | {scores.baseline_failed} | {scores.candidate_failed} "
            f"| {scores.baseline_other_collection} | {scores.candidate_other_collection} |"
            for label, scores in rows
        ),
        "",
        (
            "| Query type | Baseline scored | Candidate scored | Baseline recall@4 | Candidate recall@4 "
            "| Baseline recall@10 | Candidate recall@10 | Baseline MRR | Candidate MRR |"
        ),
        "|---|---|---|---|---|---|---|---|---|",
        *(f"| {label} | {_retrieval_cells(scores.baseline_retrieval, scores.candidate_retrieval)} |" for label, scores in rows),
        "",
        "| Query type | Baseline seen recall | Candidate seen recall | Seen recall difference |",
        "|---|---|---|---|",
        *(f"| {label} | {_seen_cells(scores)} |" for label, scores in rows),
    ]


def _seen_cells(scores: PairedScores) -> str:
    """Both sides' mean seen recall and their paired difference, each missing when no case carries one."""
    means = (retrieval.seen_recall if retrieval is not None else None for retrieval in (scores.baseline_retrieval, scores.candidate_retrieval))
    cells = [f"{mean:.3f}" if mean is not None else _MISSING for mean in means]
    difference = scores.seen_recall_difference
    cells.append(f"{difference:+.3f}" if difference is not None else _MISSING)
    return " | ".join(cells)


def _retrieval_cells(baseline: SourceLevelRetrieval | None, candidate: SourceLevelRetrieval | None) -> str:
    """Both sides' scored cases, then each mean beside the other side's, with none for a side that scored no case."""
    cells = [str(baseline.cases if baseline is not None else 0), str(candidate.cases if candidate is not None else 0)]
    for baseline_mean, candidate_mean in zip(_mean_cells(baseline), _mean_cells(candidate), strict=True):
        cells.extend((baseline_mean, candidate_mean))
    return " | ".join(cells)


def _mean_cells(retrieval: SourceLevelRetrieval | None) -> tuple[str, str, str]:
    """A side's recall@4, recall@10 and MRR as table cells, each none when it scored no case."""
    if retrieval is None:
        return "none", "none", "none"
    return f"{retrieval.recall_at_4:.3f}", f"{retrieval.recall_at_10:.3f}", f"{retrieval.mrr:.3f}"


def _variant(header: ResultsHeader) -> str:
    """A run's strategy, SQL switch, code mode, thinking level, knowledge base fingerprint and index build, as the report prints them."""
    thinking = f"thinking {header.thinking}" if header.thinking is not None else "no thinking level"
    return (
        f"strategy {header.strategy.value}, SQL {'on' if header.sql else 'off'}, code mode {'on' if header.code_mode else 'off'}, "
        f"{thinking}, knowledge base {header.knowledge_base_fingerprint}, index build {header.index_build}"
    )


def _difference(scores: PairedScores) -> str:
    """The difference in points with its interval, or a note that a single case has none."""
    if scores.interval is None:
        return f"{scores.difference * 100:+.1f} (no interval)"
    low, high = scores.interval
    return f"{scores.difference * 100:+.1f} ({low * 100:+.1f} to {high * 100:+.1f})"


def _query_type_label(query_type: str | None) -> str:
    """The query type as a table row names it."""
    return query_type if query_type is not None else "no query type"
