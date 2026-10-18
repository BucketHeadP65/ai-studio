"""The errors a caller of the judgments import handles."""

from __future__ import annotations


class JudgmentsFileError(Exception):
    """The relevance judgments file is missing, or a line of it is not a judged query, named by its line number."""
