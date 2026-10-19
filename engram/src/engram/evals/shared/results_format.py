"""The refusals both results file readers give: a line that predates the current format, or one that is not a line at all.

A line whose only complaints are keys it lacks predates the current format. Any other complaint makes it unreadable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from pydantic import ValidationError

RUN_AGAIN = "Run the evaluation again to write it in the current format."
"""The way out every predating refusal ends with."""


def missing_keys(error: ValidationError, *, under: tuple[str, ...]) -> tuple[str, ...]:
    """The keys a line lacks directly under the path `under`, sorted, when every complaint is one of those.

    Args:
        error: What validating the line raised.
        under: The path the keys sit under, empty for a key at the top of the line.

    Returns:
        The missing keys, or an empty tuple when any complaint is something other than a key missing under `under`.
    """
    complaints = error.errors()
    depth = len(under) + 1
    if not complaints or any(
        complaint["type"] != "missing" or len(complaint["loc"]) != depth or tuple(complaint["loc"][:-1]) != under for complaint in complaints
    ):
        return ()
    return tuple(sorted({str(complaint["loc"][-1]) for complaint in complaints}))


def header_refusal(path: Path, number: int, error: ValidationError) -> str:
    """Why the header line at `number` of `path` cannot be read: it predates the current format, or it is no header.

    Args:
        path: The results file.
        number: The header's line number, counted from 1.
        error: What validating the header line raised. Its header sits under the `header` key.

    Returns:
        The refusal, naming the line and the file.
    """
    missing = missing_keys(error, under=("header",))
    if missing:
        return f"line {number} of {path} predates the current results format: its header has no {', '.join(missing)}. {RUN_AGAIN}"
    return f"line {number} of {path} is not a results header: {error}"


def case_line_refusal(path: Path, number: int, error: ValidationError) -> str:
    """Why the case line at `number` of `path` cannot be read: it predates the current format, or it is no case line.

    Args:
        path: The results file.
        number: The line's number, counted from 1.
        error: What validating the case line raised.

    Returns:
        The refusal, naming the line and the file.
    """
    missing = missing_keys(error, under=())
    if missing:
        return f"line {number} of {path} predates the current results format: it has no {', '.join(missing)}. {RUN_AGAIN}"
    return f"line {number} of {path} is not a case line: {error}"
