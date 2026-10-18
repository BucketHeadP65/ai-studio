"""The automation a person asked for: what it holds, and what it is still missing.

`intent_issues` is a completeness count and nothing else. Every test here pins that reading: it
names blank fields, it never reads English for meaning, and it never refuses anything.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic import ValidationError

from core.ai.workflows.contracts import StepRegistry, WorkflowIntent, fingerprint, intent_issues, save, spec_identity
from core.ai.workflows.infrastructure.folder import FolderLibrary

if TYPE_CHECKING:
    from pathlib import Path

    from core.ai.workflows.contracts import Workflow


def test_request_only_intent_is_draftable_but_incomplete() -> None:
    intent = WorkflowIntent(request="Review new documents every Monday")
    assert intent_issues(intent) == ("expected_result", "success_example", "empty_input_behavior", "failure_behavior")


def test_a_fully_written_intent_owes_nothing() -> None:
    intent = WorkflowIntent(
        request="Review new documents every Monday",
        expected_result="One note per new document, filed under the week it arrived.",
        success_example="Three documents arrived last week and three notes came back.",
        empty_input_behavior="Nothing arrived, so the run says so and files nothing.",
        failure_behavior="A document that cannot be read is named in the answer and the rest still run.",
        trigger="scheduled",
        assumptions=("Monday means the start of the working week.",),
    )
    assert intent_issues(intent) == ()


def test_whitespace_is_not_an_answer() -> None:
    """A field holding only spaces reads as unwritten, the same as one holding nothing."""
    intent = WorkflowIntent(request="Review new documents", expected_result="   \n ", success_example="A note came back.")
    assert intent_issues(intent) == ("expected_result", "empty_input_behavior", "failure_behavior")


def test_a_long_vague_answer_is_still_an_answer() -> None:
    """Completeness, never quality: nothing here reads what a field says."""
    intent = WorkflowIntent(
        request="Review new documents",
        expected_result="stuff",
        success_example="stuff",
        empty_input_behavior="stuff",
        failure_behavior="stuff",
    )
    assert intent_issues(intent) == ()


def test_an_intent_without_a_request_is_not_an_intent() -> None:
    with pytest.raises(ValidationError):
        WorkflowIntent(request="   ")


def test_the_request_is_kept_stripped() -> None:
    assert WorkflowIntent(request="  Review new documents  ").request == "Review new documents"


def test_an_intent_is_frozen_and_takes_no_field_it_does_not_declare() -> None:
    intent = WorkflowIntent(request="Review new documents")
    with pytest.raises(ValidationError):
        intent.request = "something else"  # type: ignore[misc]
    with pytest.raises(ValidationError):
        WorkflowIntent(request="Review new documents", priority="high")  # type: ignore[call-arg]


async def test_an_intent_changes_neither_the_workflows_identity_nor_the_grants_it_runs_under(
    tmp_path: Path, quarterly_report: Workflow, registry: StepRegistry
) -> None:
    """An intent is a record beside the workflow. It selects no work and asks for no permission."""
    grants_before = registry.grants
    identity_before = spec_identity(quarterly_report)
    stamp_before = fingerprint(quarterly_report, registry)

    workflow_id = await save(
        quarterly_report,
        FolderLibrary(tmp_path / "with-intent"),
        registry,
        title="Quarterly report",
        summary="Close the quarter.",
        applicability="Use for a quarterly close.",
        saved_by="human:someone",
        intent=WorkflowIntent(request="Close the books each quarter", trigger="scheduled"),
    )

    assert registry.grants == grants_before
    assert spec_identity(quarterly_report) == identity_before
    assert fingerprint(quarterly_report, registry).grants == stamp_before.grants
    assert workflow_id == f"{quarterly_report.name}@{quarterly_report.version}"
