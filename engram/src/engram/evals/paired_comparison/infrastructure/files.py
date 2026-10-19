"""Read the results file of an evaluation run, and write a paired comparison as markdown with a YAML twin beside it."""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml

from core.files import atomic_write
from pydantic import BaseModel, ValidationError

from engram.evals.paired_comparison.domain import ResultsFile, ResultsHeader, ResultsLine, paired_comparison_markdown
from engram.evals.paired_comparison.exceptions import ResultsFileUnreadableError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from engram.evals.paired_comparison.domain import PairedComparison


class _HeaderLine(BaseModel):
    """The first line of a results file."""

    header: ResultsHeader


def read_results(path: Path) -> ResultsFile:
    """Read the results file of an evaluation run: its header line, then one line per case.

    Lines end at a newline alone, so a line separator inside an answer stays in its line. Blank lines are skipped, and
    the keys a paired comparison does not read are ignored. A header or a case line missing a field the writer writes is
    refused as predating the current format.

    Args:
        path: The results file.

    Returns:
        The file's header and its case lines, in file order.

    Raises:
        ResultsFileUnreadableError: When the file cannot be read, is empty or predates the current format, or when a line does
            not parse or repeats a case. The message names the line.
    """
    try:
        rows = path.read_text(encoding="utf-8").split("\n")
    except OSError as error:
        msg = f"the results file {path} cannot be read: {error}"
        raise ResultsFileUnreadableError(msg) from error
    numbered = [(number, row) for number, row in enumerate(rows, start=1) if row.strip()]
    if not numbered:
        msg = f"the results file {path} is empty"
        raise ResultsFileUnreadableError(msg)
    (header_number, header_row), *case_rows = numbered
    try:
        header = _HeaderLine.model_validate_json(header_row).header
    except ValidationError as error:
        missing = _missing_keys(error, under=("header",))
        msg = (
            f"line {header_number} of {path} predates the current results format: its header has no {', '.join(missing)}. "
            "Run the evaluation again to write it in the current format."
            if missing
            else f"line {header_number} of {path} is not a results header: {error}"
        )
        raise ResultsFileUnreadableError(msg) from error
    lines: list[ResultsLine] = []
    seen: set[str] = set()
    for number, row in case_rows:
        try:
            line = ResultsLine.model_validate_json(row)
        except ValidationError as error:
            missing = _missing_keys(error, under=())
            msg = (
                f"line {number} of {path} predates the current results format: it has no {', '.join(missing)}. "
                "Run the evaluation again to write it in the current format."
                if missing
                else f"line {number} of {path} is not a case line: {error}"
            )
            raise ResultsFileUnreadableError(msg) from error
        if line.case in seen:
            msg = f"line {number} of {path} repeats the case {line.case}"
            raise ResultsFileUnreadableError(msg)
        seen.add(line.case)
        lines.append(line)
    return ResultsFile(path=str(path), header=header, lines=tuple(lines))


def _missing_keys(error: ValidationError, *, under: tuple[str, ...]) -> tuple[str, ...]:
    """The keys a line lacks directly under the path `under`, sorted, when every complaint is one of those, else nothing."""
    complaints = error.errors()
    depth = len(under) + 1
    if not complaints or any(complaint["type"] != "missing" or len(complaint["loc"]) != depth or tuple(complaint["loc"][:-1]) != under for complaint in complaints):
        return ()
    return tuple(sorted({str(complaint["loc"][-1]) for complaint in complaints}))


def write_paired_comparison(comparisons: Sequence[PairedComparison], *, out: Path) -> tuple[Path, Path]:
    """Write the markdown report at `out` and its YAML twin beside it, the same name ending in `.yaml`.

    The folder is created when missing, and files already there are replaced.

    Args:
        comparisons: The pairs compared in one call.
        out: The markdown file to write. Its name ends in `.md`.

    Returns:
        The markdown path and the YAML path.

    Raises:
        ValueError: When `out` does not end in `.md`.
    """
    if out.suffix != ".md":
        msg = f"the report {out} must end in .md, so its YAML twin can sit beside it"
        raise ValueError(msg)
    twin = out.with_suffix(".yaml")
    out.parent.mkdir(parents=True, exist_ok=True)
    document = {"pairs": [comparison.model_dump(mode="json") for comparison in comparisons]}
    atomic_write(twin, yaml.safe_dump(document, sort_keys=False, allow_unicode=True))
    atomic_write(out, paired_comparison_markdown(comparisons))
    return out, twin
