"""Import a relevance judgments file into an evals dataset file."""

from __future__ import annotations

from typing import TYPE_CHECKING

from engram.evals.generation.contracts import build_dataset, save_dataset
from engram.evals.judgments.domain import JudgmentsImported, authored_case, cases_per_type, judged_queries, per_type_sample
from engram.evals.judgments.exceptions import JudgmentsFileError

if TYPE_CHECKING:
    from pathlib import Path


def import_judgments(source: Path, out: Path, *, per_type: int | None = None, salt: str = "") -> JudgmentsImported:
    """Read a relevance judgments file and write the evals dataset it makes.

    Each line is `{"query_id", "question", "judgments": {"<source id>": <grade>}}`, with an optional `answer` and
    `query_type`. A query with no document graded above 0 and no answer is left out and counted. Nothing is written
    when a line does not parse or `per_type` is below 1.

    Args:
        source: The judgments file.
        out: The dataset file to write, with the schema file beside it.
        per_type: How many cases to keep of each query type, at least 1, or None to keep every case. The cases
            whose SHA-256 of the salt and the case name sorts first are kept.
        salt: The text put before each case name before it is hashed. The same salt keeps the same cases.

    Returns:
        How many cases were written and of each query type, how many queries were left out, and where.

    Raises:
        JudgmentsFileError: When `source` cannot be read or a line is not a judged query.
        ValueError: When `per_type` is below 1.
    """
    try:
        lines = source.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        msg = f"the judgments file {source} cannot be read: {error}"
        raise JudgmentsFileError(msg) from error
    queries = judged_queries(lines)
    cases = [case for case in (authored_case(query) for query in queries) if case is not None]
    skipped = len(queries) - len(cases)
    if per_type is not None:
        cases = per_type_sample(cases, per_type=per_type, salt=salt)
    out.parent.mkdir(parents=True, exist_ok=True)
    save_dataset(build_dataset(cases), out)
    return JudgmentsImported(cases=len(cases), skipped=skipped, cases_per_type=cases_per_type(cases), out=str(out))
