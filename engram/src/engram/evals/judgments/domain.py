"""Judged queries as evals cases: the relevant documents become the gold ids, the answer and every grade ride along."""

from __future__ import annotations

import hashlib

from collections import Counter
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from engram.evals.generation.contracts import AuthoredCase
from engram.evals.judgments.exceptions import JudgmentsFileError
from engram.evals.shared.contracts import ExpectedQa, QaCaseMeta, QaInput

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence


class JudgedQuery(BaseModel):
    """One line of a relevance judgments file: a query and the grade of every document judged for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    query_id: str
    question: str
    judgments: dict[str, int]
    answer: str | None = None
    """The gold answer, None when the line gives none."""
    query_type: str | None = None
    """The query type, None when the line gives none."""


class JudgmentsImported(BaseModel):
    """What an import wrote: how many cases of each query type, how many queries it left out, and where."""

    model_config = ConfigDict(frozen=True)

    cases: int
    skipped: int
    """The queries left out for having no relevant document and no answer."""
    cases_per_type: dict[str, int]
    """How many written cases carry each query type. A case with no query type is counted only in `cases`."""
    out: str


def judged_queries(lines: Iterable[str]) -> list[JudgedQuery]:
    """Every judged query of a judgments file, in file order.

    Args:
        lines: The file's lines. Blank lines are skipped.

    Returns:
        The judged queries.

    Raises:
        JudgmentsFileError: When a non-empty line is not a judged query.
    """
    queries: list[JudgedQuery] = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            queries.append(JudgedQuery.model_validate_json(line))
        except ValidationError as error:
            msg = f"line {number} is not a judged query with query_id, question and judgments: {error}"
            raise JudgmentsFileError(msg) from error
    return queries


def authored_case(query: JudgedQuery) -> AuthoredCase | None:
    """The case for one judged query, or `None` when no document has a grade above 0 and it has no answer.

    The gold ids are the relevant documents, the highest grade first and ties in file order, and are empty for a
    query that has only an answer. The case is named after the query, has no difficulty and no anchor page, and keeps
    its answer, its query type and every grade in its judgments.

    Args:
        query: The judged query.

    Returns:
        The case, or `None`.
    """
    relevant = [document for document, grade in query.judgments.items() if grade > 0]
    if not relevant and not query.answer:
        return None
    gold = sorted(relevant, key=lambda document: -query.judgments[document])
    return AuthoredCase(
        question=QaInput(question=query.question),
        expected=ExpectedQa(answer=query.answer or "", expected_source_ids=tuple(gold)),
        meta=QaCaseMeta(judgments=dict(query.judgments), query_type=query.query_type),
        case_name=query.query_id,
    )


def per_type_sample(cases: Sequence[AuthoredCase], *, per_type: int, salt: str) -> list[AuthoredCase]:
    """The first `per_type` cases of each query type, ordered by the SHA-256 of the salt and the case name.

    The cases with no query type are sampled as one group. The kept cases stay in their given order, and the same
    salt keeps the same cases every time.

    Args:
        cases: The cases to sample, each named.
        per_type: How many cases to keep of each query type, at least 1. A query type with fewer keeps all of them.
        salt: The text put before each case name before it is hashed.

    Returns:
        The kept cases.

    Raises:
        ValueError: When `per_type` is below 1.
    """
    if per_type < 1:
        msg = f"the per-type sample size must be at least 1, got {per_type}"
        raise ValueError(msg)
    groups: dict[str | None, list[int]] = {}
    for index, case in enumerate(cases):
        groups.setdefault(case.meta.query_type, []).append(index)
    kept = {
        index
        for indexes in groups.values()
        for index in sorted(indexes, key=lambda position: _salted_hash(salt, cases[position].case_name or ""))[:per_type]
    }
    return [case for index, case in enumerate(cases) if index in kept]


def cases_per_type(cases: Iterable[AuthoredCase]) -> dict[str, int]:
    """How many cases carry each query type, in order of first appearance.

    Args:
        cases: The cases to count. A case with no query type is not counted.

    Returns:
        The count of each query type.
    """
    return dict(Counter(case.meta.query_type for case in cases if case.meta.query_type is not None))


def _salted_hash(salt: str, name: str) -> str:
    return hashlib.sha256((salt + name).encode()).hexdigest()
