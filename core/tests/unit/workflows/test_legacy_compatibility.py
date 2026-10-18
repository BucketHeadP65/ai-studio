"""Library files and outcomes produced before today's contracts existed, read back by today's code.

Every fixture here is hand-typed rather than dumped from a current model, so a field that starts
being written by default fails loudly here instead of the fixture quietly growing it. What each one
asserts is the honest fallback: a file the models no longer recognise reads as carrying nothing,
never as a listing that will not load and never as words this library composed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest

from pydantic import ValidationError

from core.ai.workflows.contracts import (
    GateDecisionFact,
    GateReview,
    MapBatch,
    Outcome,
    RetryFeedback,
    ReviewedArtifactRef,
    WorkflowPresentation,
    WorkflowResultSpec,
    WorkItemFact,
    observed_outcome,
)
from core.ai.workflows.domain import outcome as _outcome
from core.ai.workflows.domain.vocabulary import RequiredGateCheck
from core.ai.workflows.infrastructure.folder import FolderLibrary

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

_SPEC_YAML = """workflow:
  name: quarterly-close
  version: 1
  description: Reconcile the quarter and draft the report.
  parameters:
    quarter:
      description: Fiscal quarter
      json_schema:
        type: string
      required: true
  inline: []
  nodes:
  - kind: run
    id: reconcile
    work: reconcile
    inputs:
      quarter:
        parameter: quarter
    tier: null
  output:
    node: reconcile
"""
"""The executable half every fixture below shares, so what differs between them is only the
author's own record beside it."""

_NO_PRESENTATION_FILE = f"""---
title: Quarterly close
summary: Reconcile the quarter and draft the report.
applicability: Use this at the end of a fiscal quarter.
status: draft
{_SPEC_YAML}---

## Applicability

Use this at the end of a fiscal quarter.
"""
"""One workflow file exactly as it was written before a presentation or an intent could be kept:
the card's three fields, the spec, and nothing else."""

_UNSUPPORTED_ACTION_FILE = f"""---
title: Quarterly close
summary: Reconcile the quarter and draft the report.
applicability: Use this at the end of a fiscal quarter.
status: draft
presentation:
  purpose: Close the quarter without reading every ledger line.
  produces: One reconciled report, ready to send.
  owner_participation: You approve the draft before it goes out.
  not_for: A mid-quarter spot check.
  actions:
  - kind: publish_to_web
    id: publish
    label: Publish it
    artifact_id: report
    endpoint: https://example.invalid/publish
    method: POST
{_SPEC_YAML}---

## Applicability

Use this at the end of a fiscal quarter.
"""
"""A file whose presentation offers an action this app has no intent for, with the endpoint and
method such an action would have needed. Nothing here is implementable and nothing here is
addressable, so the whole record reads as one nobody wrote."""


_SUPPORTED_ACTION_FILE = _UNSUPPORTED_ACTION_FILE.replace(
    """  - kind: publish_to_web
    id: publish
    label: Publish it
    artifact_id: report
    endpoint: https://example.invalid/publish
    method: POST
""",
    """  - kind: save_note
    id: keep
    label: Keep it
    artifact_id: report
    destination: user_selects
""",
)
"""The same file with an action this app does implement, so the tests below tell an unreadable
record apart from a fixture that was never readable."""


def _library(root: Path, *, file: str) -> FolderLibrary:
    """A folder library holding exactly one hand-typed file, named the way this store names them."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "quarterly-close-v1.md").write_text(file, encoding="utf-8")
    return FolderLibrary(root)


# --- a card written before presentations existed ------------------------------------------------


async def test_a_file_written_before_presentations_reads_as_carrying_none(tmp_path: Path) -> None:
    """Absent is absent. Nothing composes four sentences out of the spec's own prose."""
    library = _library(tmp_path / "library", file=_NO_PRESENTATION_FILE)

    card = await library.fetch_card("quarterly-close@1")

    assert card is not None
    assert card.presentation is None, "a card carrying no words carries no words"
    assert card.intent is None
    assert card.title == "Quarterly close"


async def test_a_file_written_before_presentations_still_lists_and_still_runs(tmp_path: Path) -> None:
    """The listing a chooser reads and the spec a launch needs both load from the same old file."""
    library = _library(tmp_path / "library", file=_NO_PRESENTATION_FILE)

    catalog = await library.catalog()
    spec = await library.fetch_workflow("quarterly-close@1")

    assert [row.id for row in catalog] == ["quarterly-close@1"]
    assert [row.presentation for row in catalog] == [None]
    assert spec.name == "quarterly-close"


async def test_rewriting_the_prose_of_an_undescribed_card_leaves_it_undescribed(tmp_path: Path) -> None:
    """A title edit is a title edit. It never becomes the moment somebody described the workflow."""
    library = _library(tmp_path / "library", file=_NO_PRESENTATION_FILE)

    await library.redescribe(
        "quarterly-close@1", title="Quarterly close, revised", summary="Reconcile the quarter.", applicability="End of a quarter."
    )
    card = await library.fetch_card("quarterly-close@1")

    assert card is not None
    assert card.title == "Quarterly close, revised"
    assert card.presentation is None


# --- an action nothing here implements -----------------------------------------------------------


async def test_a_file_offering_an_action_this_app_has_no_intent_for_reads_as_undescribed(tmp_path: Path) -> None:
    """One unaddressable action takes the whole record with it, because half a record is not a record."""
    library = _library(tmp_path / "library", file=_UNSUPPORTED_ACTION_FILE)

    card = await library.fetch_card("quarterly-close@1")

    assert card is not None
    assert card.presentation is None, "an action nothing can carry out is not shown as one that can"


async def test_the_same_file_with_an_action_this_app_does_implement_reads_its_words(tmp_path: Path) -> None:
    """The control: nothing about this fixture's shape is what makes the unsupported one unreadable."""
    library = _library(tmp_path / "library", file=_SUPPORTED_ACTION_FILE)

    card = await library.fetch_card("quarterly-close@1")

    assert card is not None
    assert card.presentation is not None
    assert [action.id for action in card.presentation.actions] == ["keep"]


async def test_a_file_offering_an_unsupported_action_still_lists(tmp_path: Path) -> None:
    """A hand-edited file that no longer parses must not take down the listing that would have shown it."""
    library = _library(tmp_path / "library", file=_UNSUPPORTED_ACTION_FILE)

    catalog = await library.catalog()

    assert [row.id for row in catalog] == ["quarterly-close@1"]
    assert [row.presentation for row in catalog] == [None]


def test_the_same_action_is_refused_on_the_way_in() -> None:
    """Reads forgive an old file. Writes do not, so nothing new can land carrying one."""
    offered = {
        "actions": [
            {"kind": "publish_to_web", "id": "publish", "label": "Publish it", "artifact_id": "report", "endpoint": "https://x"}
        ]
    }

    with pytest.raises(ValidationError):
        WorkflowPresentation.model_validate(offered)


def test_a_supported_action_carrying_an_endpoint_is_refused_too() -> None:
    """The closed set is one half of the rule. The other half is that there is nowhere to put a route."""
    offered = {
        "actions": [
            {
                "kind": "save_note",
                "id": "keep",
                "label": "Keep it",
                "artifact_id": "report",
                "destination": "user_selects",
                "endpoint": "https://example.invalid/notes",
            }
        ]
    }

    with pytest.raises(ValidationError):
        WorkflowPresentation.model_validate(offered)


# --- an outcome nobody recorded -------------------------------------------------------------------

_DIGEST = "a" * 64


def _outcome_of(
    result: WorkflowResultSpec | None,
    *,
    status: Literal["completed", "failed", "stopped"] = "completed",
    facts: Sequence[WorkItemFact] = (),
    primary: MapBatch | None = None,
    decisions: Sequence[GateDecisionFact] = (),
    reviews: Mapping[str, GateReview] | None = None,
    exhausted: RetryFeedback | None = None,
) -> Outcome:
    """One classification, with everything a run would have supplied defaulted to nothing."""
    return observed_outcome(
        result,
        status=status,
        facts=facts,
        primary=primary,
        decisions=decisions,
        reviews={} if reviews is None else reviews,
        exhausted=exhausted,
    )


def test_a_workflow_that_declared_no_result_can_only_be_unknown() -> None:
    """A run of a workflow from before results were declarable is unknown, and stays unknown."""
    outcome = _outcome_of(None)

    assert outcome.status == "unknown"
    assert outcome.primary_input is None
    assert outcome.generation is None
    assert (outcome.attempted, outcome.succeeded, outcome.skipped, outcome.failed) == (0, 0, 0, 0)


def test_an_unknown_outcome_reports_counts_as_numbers_rather_than_as_nothing() -> None:
    """A reader always gets numbers. What is not known is said by the status, not by a missing count."""
    outcome = _outcome_of(WorkflowResultSpec(summary="the reconciled quarter", primary_work="reconcile"))

    assert outcome.status == "unknown"
    assert outcome.attempted == outcome.succeeded + outcome.skipped + outcome.failed


def test_work_that_settled_under_a_declaration_nobody_wrote_still_reads_unknown() -> None:
    """Facts on their own settle nothing: without a declaration there is no question they answer."""
    settled = (
        WorkItemFact.model_validate({"node_id": "reconcile", "generation": 0, "item_key": "", "disposition": "succeeded"}),
    )

    outcome = _outcome_of(None, facts=settled, primary=MapBatch(generation=0, ordered_digest=_DIGEST, items=("",)))

    assert outcome.status == "unknown", "a run whose declaration is missing is never promoted to succeeded"


# --- the words an outcome is allowed to use -------------------------------------------------------

_USEFULNESS_WORDS = (
    "useful",
    "usefulness",
    "valuable",
    "worthwhile",
    "helpful",
    "worth doing",
    "quality",
    "right answer",
    "as it should",
)
"""Words that would claim more than a checked outcome can know: that the work was worth doing or
any good. Whether it was is somebody else's record, kept as their own event. Whether a required
gate accepted the bytes is not on this list, because that is a fact this side checks and says."""


def _every_summary() -> list[str]:
    """One summary from every branch the classifier can reach, plus the status words themselves.

    The list is maintained by hand, so the test below checks it against the classifier's own fixed
    summaries: a branch added with a new constant and no case here fails rather than going unread.
    """
    declared = WorkflowResultSpec(summary="the reconciled quarter", primary_work="reconcile")
    gated = WorkflowResultSpec(
        summary="the reconciled quarter",
        primary_work="reconcile",
        required_gates=(RequiredGateCheck(gate_id="approve", accepted_sources=("person",)),),
    )
    batch = MapBatch(generation=0, ordered_digest=_DIGEST, items=("one", "two"))

    def fact(key: str, disposition: str) -> WorkItemFact:
        return WorkItemFact.model_validate({"node_id": "reconcile", "generation": 0, "item_key": key, "disposition": disposition})

    exhausted = RetryFeedback(
        gate_id="approve",
        target_node="reconcile",
        attempt=1,
        source="person",
        feedback="The totals do not add up.",
        requested_repair="Recount the ledger before drafting again.",
        reviewed=ReviewedArtifactRef(run_id="20260915T120000000000-abcdef12", node_id="reconcile", turn=None, value_digest=_DIGEST),
    )
    empty = MapBatch(generation=0, ordered_digest=_DIGEST, items=())
    cases = [
        _outcome_of(None),
        _outcome_of(declared),
        _outcome_of(declared, primary=batch),
        _outcome_of(declared, primary=batch, facts=(fact("one", "succeeded"), fact("two", "succeeded"))),
        _outcome_of(declared, primary=batch, facts=(fact("one", "succeeded"), fact("two", "failed"))),
        _outcome_of(declared, primary=batch, facts=(fact("one", "failed"), fact("two", "failed"))),
        _outcome_of(declared, primary=batch, facts=(fact("one", "succeeded"), fact("one", "failed"))),
        _outcome_of(declared, primary=empty),
        _outcome_of(declared, primary=batch, status="stopped"),
        _outcome_of(declared, primary=batch, facts=(fact("one", "succeeded"), fact("two", "succeeded")), status="stopped"),
        _outcome_of(gated, primary=batch, facts=(fact("one", "succeeded"), fact("two", "succeeded"))),
        _outcome_of(gated, primary=batch, facts=(fact("one", "succeeded"),), exhausted=exhausted),
    ]
    return [case.summary for case in cases] + [case.status for case in cases]


def test_every_fixed_summary_the_classifier_can_write_is_covered_above() -> None:
    """The guard on the hand-maintained list: a new branch with a new constant is not silently unread.

    Only the classifier's fixed sentences are checked. The composed ones (the counts, and a spent
    gate's own refusal) are built per run and have no constant to compare against.
    """
    fixed = {
        value
        for name, value in vars(_outcome).items()
        if name.startswith("_") and name.isupper() and isinstance(value, str)
    }

    assert fixed <= set(_every_summary()), f"unreached: {sorted(fixed - set(_every_summary()))}"


@pytest.mark.parametrize("said", _every_summary())
def test_no_outcome_says_the_work_was_any_good(said: str) -> None:
    """Producer success is the only claim on this side. Usefulness and acceptance are other records."""
    lowered = said.lower()
    offending = [word for word in _USEFULNESS_WORDS if word in lowered]

    assert offending == [], f"{said!r} claims {offending}, which a checked outcome cannot know"
