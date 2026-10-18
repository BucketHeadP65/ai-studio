"""The results file of an evaluation run: a header line, then one JSON line per case.

A case that failed is written as incorrect with its error and whether it failed transiently, so two runs of one
dataset always pair case by case. Each line scores recall@4, recall@10 and MRR against the case's gold ids, over the
source ids its citations map to when the run mapped them, and over its cited page ids otherwise. When the run mapped
sources, each line also carries its seen recall: the share of its gold ids among the sources its tool results named. A
file written by an older run reads back with code mode off, the thinking level unset, `search_date_range` as the
opposite of `pinned_search`, the index build unknown, no seen recall, and whether each failure was transient read from
its error text.
"""

from __future__ import annotations

import hashlib
import json

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Annotated

from core.files import atomic_write
from core.thinking import Thinking  # noqa: TC002 (pydantic reads the header's annotations at runtime when it reads a file back)
from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from engram.evals.harness.domain.answers import is_official_match, is_strict_match
from engram.evals.harness.transient_failures import is_transient_error_text
from engram.evals.outcomes.contracts import citation_source_metrics, ranked_metrics

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping, Sequence
    from pathlib import Path

    from pydantic import JsonValue
    from pydantic_evals.reporting import EvaluationReport, ReportCase, ReportCaseFailure

    from engram.evals.generation.contracts import EvaluationDataset
    from engram.evals.shared.contracts import QaCaseMeta, QaInput, RetrievalAnswer

_RECALL_KS = (4, 10)


@dataclass(frozen=True, kw_only=True)
class ResultsHeader:
    """How one evaluation run was set up, written as the first line of its results file."""

    strategy: str
    """The retrieval strategy the agent under test ran under."""
    sql: bool
    """Whether the agent under test had the SQL tools."""
    code_mode: bool = False
    """Whether the agent under test drove its tools through the sandboxed `run_code` tool. Off in a header that did not record it."""
    model: str
    """The model under test, as the run named it."""
    thinking: Thinking | None = None
    """The thinking level the model under test was built at, or None for the model's own default or a header that did not record it."""
    instructions_sha256: str
    """The SHA-256 of the agent's instructions, in hex."""
    max_tool_calls: int | None
    """The most tool calls one case could make, or None for no limit."""
    pinned_search: bool
    """Whether `search` was pinned to the knowledge base's own collection."""
    search_date_range: bool = True
    """Whether `search` took a date range, `since` and `until`. Every run writes true.

    A header that did not record it reads it as the opposite of `pinned_search`.
    """
    dataset: str
    """The dataset's name."""
    dataset_sha256: str
    """The SHA-256 of the dataset's cases, as `dataset_sha256` computes it."""
    collection: str
    """The knowledge base the run read."""
    knowledge_base_fingerprint: str
    """The knowledge base's index fingerprint and its name."""
    index_build: str | None = None
    """The index build the run read: the stored indexer version and the embedding model. None, read as unknown, in a header that did not record it."""
    started: str
    """When the run started, as an ISO 8601 time with its offset."""
    source_lookup_error: str | None
    """What the lookup of the cited pages' sources raised, or None when it succeeded or the run mapped no sources."""


@dataclass(frozen=True)
class ResultsLine:
    """One case of an evaluation run, as its results file records it."""

    case: str
    """The case's name."""
    query_type: str | None
    """The case's query type, or None when the case has none."""
    strict: bool
    """Whether the answer matched the expected answer strictly. False for a failed case or one with no expected answer."""
    official: bool
    """Whether the answer shared a word with the expected answer. False for a failed case or one with no expected answer."""
    failed: bool
    """Whether the case raised instead of answering."""
    error: str | None
    """What the failed case raised, or None for an answered one."""
    answer: str | None
    """The agent's answer, or None for a failed case."""
    cited: tuple[str, ...]
    """The ids the agent cited, in its order."""
    sources: tuple[str, ...] | None
    """The source ids the citations map to, each once at its first rank, or None when the run mapped none."""
    recall_at_4: Annotated[float | None, Field(validation_alias="recall@4")]
    """Recall at 4 against the gold ids, or None for a case with no gold ids. The file's key is `recall@4`."""
    recall_at_10: Annotated[float | None, Field(validation_alias="recall@10")]
    """Recall at 10 against the gold ids, or None for a case with no gold ids. The file's key is `recall@10`."""
    mrr: float | None
    """The reciprocal rank of the first gold id, or None for a case with no gold ids."""
    other_collection: bool
    """Whether a row the case's SQL statements returned from a view of knowledge base content named another knowledge base."""
    transient: bool = False
    """Whether the case failed transiently: a model connection error or timeout, an HTTP 429 or 5xx, or a database connection failure.

    False for an answered case. A line written before it was recorded reads it from its error text.
    """
    seen_recall: float | None = None
    """The share of the gold ids among the sources the case's tool results named, or None for a case with no gold ids, a run
    that mapped no sources or a line written before it was recorded."""
    seen_gold: tuple[str, ...] | None = None
    """The gold ids among the sources the case's tool results named, in gold order, or None whenever `seen_recall` is."""


@dataclass(frozen=True)
class _HeaderLine:
    """The first line of a results file."""

    header: ResultsHeader


class _DateRangeKey(BaseModel):
    """The one header key that a file written before the date range was recorded lacks."""

    search_date_range: bool | None = None
    """Whether `search` took a date range, or None when the header did not record it."""


class _HeaderKeys(BaseModel):
    """The header line, read only for the keys an older file may lack."""

    header: _DateRangeKey


class _TransientKey(BaseModel):
    """The one key of a case line that a file written before transient failures were recorded lacks."""

    transient: bool | None = None
    """Whether the case failed transiently, or None when the line did not record it."""


_HEADER_LINE = TypeAdapter(_HeaderLine)
_CASE_LINE = TypeAdapter(ResultsLine)


def results_lines(  # noqa: PLR0913 (the report, its sources, its flags, the order and the kept lines are distinct inputs)
    report: EvaluationReport[QaInput, RetrievalAnswer, QaCaseMeta],
    *,
    sources: Mapping[str, Sequence[str]] | None,
    seen: Mapping[str, Collection[str]] | None = None,
    other_collection: Collection[str],
    case_order: Sequence[str],
    source_lookup_failed: bool = False,
    transient: Collection[str] = frozenset(),
    kept: Sequence[ResultsLine] = (),
) -> list[ResultsLine]:
    """One line per case of `report` and per kept line, its answered cases and its failures together, in the dataset's order.

    Args:
        report: The run's report.
        sources: The ranked source ids each answered case's citations map to, keyed by case name, or None when the
            run mapped none. An answered case missing here maps to no source.
        seen: The source ids each case's tool results named, answered and failed cases alike, keyed by case name. It is
            read only when `sources` is not None, and a case missing here saw no source.
        other_collection: The names of the cases whose SQL rows named another knowledge base.
        case_order: The dataset's case names in order. A case missing here comes last.
        source_lookup_failed: Whether this run's lookup of the cited pages' sources failed. It leaves the sources, recall,
            MRR and seen recall of every line of `report` null, rather than scoring the cited page ids in their place. The
            kept lines keep theirs.
        transient: The names of the failed cases that failed transiently.
        kept: The lines an earlier run of the same variant wrote for the cases `report` did not run, kept as they are.

    Returns:
        The lines.
    """
    position = {name: index for index, name in enumerate(case_order)}
    seen_sources = None if sources is None else (seen if seen is not None else {})
    run_now = [
        *(_answered_line(case, sources=sources, seen=seen_sources, other_collection=other_collection) for case in report.cases),
        *(
            _failed_line(failure, seen=seen_sources, other_collection=other_collection, transient=transient)
            for failure in report.failures
        ),
    ]
    if source_lookup_failed:
        run_now = [
            replace(line, sources=None, recall_at_4=None, recall_at_10=None, mrr=None, seen_recall=None, seen_gold=None) for line in run_now
        ]
    return sorted([*kept, *run_now], key=lambda line: position.get(line.case, len(position)))


def write_results(path: Path, header: ResultsHeader, lines: Sequence[ResultsLine]) -> None:
    """Write the results file at `path`: `{"header": ...}` on the first line, then one line per case.

    The folder is created when missing, and a file already there is replaced whole, never truncated in place.

    Args:
        path: The file to write.
        header: How the run was set up.
        lines: The case lines, in the order to write them.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, JsonValue]] = [{"header": _header_json(header)}, *(_line_json(line) for line in lines)]
    atomic_write(path, "".join(f"{json.dumps(row, ensure_ascii=False)}\n" for row in rows))


def read_results(path: Path) -> tuple[ResultsHeader, tuple[ResultsLine, ...]]:
    """Read back a results file `write_results` wrote: its header, then its case lines in file order.

    Lines end at a newline alone, so a line separator inside an answer stays in its line. Blank lines are skipped. A
    header that did not record code mode or the thinking level reads them as off and unset, one that did not record
    `search_date_range` reads it as the opposite of `pinned_search`, and one that did not record the index build reads
    it as None. A case line that did not record whether it failed transiently reads it from its error text, and one that
    did not record seen recall reads it as None.

    Args:
        path: The results file.

    Returns:
        The header and the case lines.

    Raises:
        OSError: When the file cannot be read.
        ValueError: When the file is empty, a line is not a header or a case line, or a line repeats a case. The message
            names the line.
    """
    numbered = [(number, row) for number, row in enumerate(path.read_text(encoding="utf-8").split("\n"), start=1) if row.strip()]
    if not numbered:
        msg = f"the results file {path} is empty"
        raise ValueError(msg)
    (header_number, header_row), *case_rows = numbered
    try:
        header = _HEADER_LINE.validate_json(header_row).header
        if _HeaderKeys.model_validate_json(header_row).header.search_date_range is None:
            header = replace(header, search_date_range=not header.pinned_search)
    except ValidationError as error:
        msg = f"line {header_number} of {path} is not a results header: {error}"
        raise ValueError(msg) from error
    lines: list[ResultsLine] = []
    seen: set[str] = set()
    for number, row in case_rows:
        try:
            line = _case_line(row)
        except ValidationError as error:
            msg = f"line {number} of {path} is not a case line: {error}"
            raise ValueError(msg) from error
        if line.case in seen:
            msg = f"line {number} of {path} repeats the case {line.case}"
            raise ValueError(msg)
        seen.add(line.case)
        lines.append(line)
    return header, tuple(lines)


def _case_line(row: str) -> ResultsLine:
    """One case line, whether it failed transiently read from its error text when the line did not record it."""
    line = _CASE_LINE.validate_json(row)
    if _TransientKey.model_validate_json(row).transient is None:
        return replace(line, transient=line.failed and is_transient_error_text(line.error))
    return line


def dataset_sha256(dataset: EvaluationDataset) -> str:
    """The SHA-256 of a dataset's cases: each case's name, input, expected output and metadata, in order.

    The dataset's own name and its evaluators are left out, so the same cases loaded from any file give the same value.

    Args:
        dataset: The dataset to identify.

    Returns:
        The hex digest.
    """
    cases = [
        {"name": case.name, "inputs": case.inputs, "expected_output": case.expected_output, "metadata": case.metadata}
        for case in dataset.cases
    ]
    canonical = json.dumps(to_jsonable_python(cases), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _answered_line(
    case: ReportCase[QaInput, RetrievalAnswer, QaCaseMeta],
    *,
    sources: Mapping[str, Sequence[str]] | None,
    seen: Mapping[str, Collection[str]] | None,
    other_collection: Collection[str],
) -> ResultsLine:
    """The line of a case that answered."""
    expected = case.expected_output.answer if case.expected_output is not None else ""
    gold = _gold_of(case.expected_output)
    cited = tuple(case.output.cited_source_ids)
    mapped = None if sources is None else tuple(sources.get(case.name, ()))
    recall_at_4, recall_at_10, mrr = _retrieval_scores(cited, mapped, gold)
    seen_gold, seen_recall = _seen_scores(case.name, seen, gold)
    answered = bool(expected.strip())
    return ResultsLine(
        case=case.name,
        query_type=case.metadata.query_type if case.metadata is not None else None,
        strict=answered and is_strict_match(case.output.answer, expected),
        official=answered and is_official_match(case.output.answer, expected),
        failed=False,
        error=None,
        answer=case.output.answer,
        cited=cited,
        sources=mapped,
        recall_at_4=recall_at_4,
        recall_at_10=recall_at_10,
        mrr=mrr,
        other_collection=case.name in other_collection,
        transient=False,
        seen_recall=seen_recall,
        seen_gold=seen_gold,
    )


def _failed_line(
    failure: ReportCaseFailure[QaInput, RetrievalAnswer, QaCaseMeta],
    *,
    seen: Mapping[str, Collection[str]] | None,
    other_collection: Collection[str],
    transient: Collection[str],
) -> ResultsLine:
    """The line of a case that raised, written as incorrect with nothing cited and with what its tool results named."""
    mapped = seen is not None
    gold = _gold_of(failure.expected_output)
    recall_at_4, recall_at_10, mrr = _retrieval_scores((), () if mapped else None, gold)
    seen_gold, seen_recall = _seen_scores(failure.name, seen, gold)
    return ResultsLine(
        case=failure.name,
        query_type=failure.metadata.query_type if failure.metadata is not None else None,
        strict=False,
        official=False,
        failed=True,
        error=failure.error_message,
        answer=None,
        cited=(),
        sources=() if mapped else None,
        recall_at_4=recall_at_4,
        recall_at_10=recall_at_10,
        mrr=mrr,
        other_collection=failure.name in other_collection,
        transient=failure.name in transient,
        seen_recall=seen_recall,
        seen_gold=seen_gold,
    )


def _gold_of(expected: RetrievalAnswer | None) -> tuple[str, ...]:
    """The case's gold ids, empty when it has no expected output."""
    return tuple(expected.cited_source_ids) if expected is not None else ()


def _seen_scores(case: str, seen: Mapping[str, Collection[str]] | None, gold: Sequence[str]) -> tuple[tuple[str, ...] | None, float | None]:
    """The gold ids among the sources the case's tool results named and their share of its gold, or None for both when unscored."""
    if seen is None or not gold:
        return None, None
    named = seen.get(case, frozenset())
    distinct = tuple(dict.fromkeys(gold))
    seen_gold = tuple(identifier for identifier in distinct if identifier in named)
    return seen_gold, len(seen_gold) / len(distinct)


def _retrieval_scores(cited: Sequence[str], mapped: Sequence[str] | None, gold: Sequence[str]) -> tuple[float | None, float | None, float | None]:
    """Recall at 4, recall at 10 and MRR over the mapped sources, or the cited pages when nothing was mapped."""
    if not gold:
        return None, None, None
    metrics = ranked_metrics(mapped, gold, ks=_RECALL_KS) if mapped is not None else citation_source_metrics(cited, gold, ks=_RECALL_KS).metrics
    return metrics.recall[4], metrics.recall[10], metrics.reciprocal_rank


def _header_json(header: ResultsHeader) -> dict[str, JsonValue]:
    """The header as the file writes it."""
    return {
        "strategy": header.strategy,
        "sql": header.sql,
        "code_mode": header.code_mode,
        "model": header.model,
        "thinking": header.thinking,
        "instructions_sha256": header.instructions_sha256,
        "max_tool_calls": header.max_tool_calls,
        "pinned_search": header.pinned_search,
        "search_date_range": header.search_date_range,
        "dataset": header.dataset,
        "dataset_sha256": header.dataset_sha256,
        "collection": header.collection,
        "knowledge_base_fingerprint": header.knowledge_base_fingerprint,
        "index_build": header.index_build,
        "started": header.started,
        "source_lookup_error": header.source_lookup_error,
    }


def _line_json(line: ResultsLine) -> dict[str, JsonValue]:
    """One case line as the file writes it."""
    return {
        "case": line.case,
        "query_type": line.query_type,
        "strict": line.strict,
        "official": line.official,
        "failed": line.failed,
        "error": line.error,
        "answer": line.answer,
        "cited": list(line.cited),
        "sources": list(line.sources) if line.sources is not None else None,
        "recall@4": line.recall_at_4,
        "recall@10": line.recall_at_10,
        "mrr": line.mrr,
        "other_collection": line.other_collection,
        "transient": line.transient,
        "seen_recall": line.seen_recall,
        "seen_gold": list(line.seen_gold) if line.seen_gold is not None else None,
    }
