"""What one reusable step version's own cases produced, kept by content address so each version runs them once."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class CaseResult(BaseModel):
    """How one case of a step's dataset came out."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    """The case's name, as its dataset gives it."""
    passed: bool
    """Whether every evaluator that judged the case passed it."""
    errored: bool
    """Whether the case errored: its run or an evaluator raised, as a broken model connection, a spend brake or a
    provider error does. An errored case did not pass and says nothing against the step. A case an evaluator judged
    and did not pass failed, and is not errored."""
    reason: str
    """What the evaluators said, or, for an errored case, `errored` and the error that stopped it."""


class CaseReport(BaseModel):
    """A step version's case report: every case's result, or why no case ran.

    `step_hash` and `cases_hash` are the content addresses of the step and of its dataset, which together name the
    report. A report whose `blocked_by` names a tool, or whose `no_cases` is true, ran nothing. A report holding an
    errored case is never kept, so the cases run again next time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step_id: str
    """The step's id in the library, such as `st.digest_source.v12`."""
    step_hash: str
    """The step's content address."""
    cases_hash: str
    """The content address of the step's dataset."""
    results: tuple[CaseResult, ...]
    """One result per case, in the dataset's order. Empty when no case ran."""
    blocked_by: str | None
    """The tool the step holds that a case run cannot serve, which stopped the case run. `None` when nothing did."""
    no_cases: bool
    """Whether no case ran because there was none to run.

    It is true for a step with no dataset or an empty one, for an agent step, and for a step whose output is another
    step's type, because neither runs its cases on its own."""
    stand_in_calls: int
    """How many write calls the stand-ins answered while the cases ran."""
