"""Trust per version and in total: every saved version of a name folds on its own and together, and the level stays the current one's."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    Approval,
    FromNode,
    FromParameter,
    Grade,
    LibraryCollection,
    LibraryError,
    Outcome,
    ParameterSpec,
    RunEvidence,
    RunNode,
    Workflow,
    WorkflowVersionQueries,
    spec_identity,
    trust_across_versions,
    version_hashes,
)
from core.ai.workflows.infrastructure.folder import FolderLibrary
from core.ai.workflows.infrastructure.library_collection import ALL_VERSIONS_SQL
from tests.support.fakes.library_collection import FakeCollection

if TYPE_CHECKING:
    from pathlib import Path

HASHES = {"h1": 1, "h2": 2, "h3": 3}


def _run(spec_hash: str, version: int, *, confirmed: bool = True, outcome: str = "completed") -> RunEvidence:
    return RunEvidence.model_validate(
        {
            "workflow_id": f"wf.report.v{version}",
            "spec_hash": spec_hash,
            "workflow_version": version,
            "outcome": outcome,
            "result": Outcome(status="succeeded" if confirmed else "unknown", summary="the report was published"),
            "gates_auto": (),
            "gates_orchestrated": (),
        }
    )


def _approval(spec_hash: str, version: int, by: str) -> Approval:
    return Approval(workflow_id=f"wf.report.v{version}", spec_hash=spec_hash, workflow_version=version, by=by)


def test_three_versions_fold_into_a_total_of_every_run_a_record_per_version_and_the_newest_as_current() -> None:
    events = [
        _run("h1", 1),
        _run("h1", 1),
        _run("h1", 1, outcome="failed"),
        _run("h2", 2),
        _run("h2", 2, confirmed=False),
        _run("h3", 3),
        Grade(workflow_id="wf.report.v3", spec_hash="h3", workflow_version=3, judge_kind="structural", judge="recall", measure="recall", score=0.5),
    ]

    trust = trust_across_versions(events, HASHES, "h3")

    assert trust.total.runs_with_evidence == 4, "every confirmed run of every version"
    assert trust.total.failed_runs == 1
    assert trust.total.unconfirmed_runs == 1
    assert trust.total.structural_grades == 1
    assert [version for version, _ in trust.versions] == [1, 2, 3], "one record per version, lowest first"
    assert [record.runs_with_evidence for _, record in trust.versions] == [2, 1, 1]
    assert trust.current.runs_with_evidence == 1, "the current record holds the newest version's runs only"
    assert trust.current == dict(trust.versions)[3]
    assert trust.level == "machine-confirmed"


def test_an_approval_of_an_earlier_version_shows_in_the_total_and_leaves_the_level_at_the_current_version_s() -> None:
    events = [_approval("h1", 1, "owner"), _run("h3", 3)]

    trust = trust_across_versions(events, HASHES, "h3")

    assert trust.total.approved_by == ("owner",)
    assert trust.total.level == "approved", "the total alone would read approved"
    assert trust.level == "machine-confirmed", "the level reads the current version only"
    assert dict(trust.versions)[1].level == "approved"


def test_a_run_counts_once_under_the_version_whose_content_address_it_carries() -> None:
    """The run recorded version 2 as provenance, and its content address is version 1's, so version 1 holds it."""
    misfiled = _run("h1", 2)

    trust = trust_across_versions([misfiled], HASHES, "h3")

    assert [record.runs_with_evidence for _, record in trust.versions] == [1, 0, 0]
    assert trust.total.runs_with_evidence == 1


def test_a_run_of_a_content_address_no_version_holds_counts_nowhere() -> None:
    trust = trust_across_versions([_run("another", 3)], HASHES, "h3")

    assert trust.total.runs_with_evidence == 0
    assert trust.level == "unverified"


def test_a_current_content_address_outside_the_versions_is_refused() -> None:
    with pytest.raises(ValueError, match="h9"):
        trust_across_versions([], HASHES, "h9")


def _report(version: int) -> Workflow:
    return Workflow(
        name="report",
        version=version,
        description=f"Draft and publish the report, revision {version}.",
        parameters={"topic": ParameterSpec(description="the topic", json_schema={"type": "string"})},
        nodes=(RunNode(id="draft", work="draft", inputs={"topic": FromParameter(parameter="topic")}),),
        output=FromNode(node="draft"),
    )


async def _three_versions(store: FakeCollection) -> LibraryCollection:
    """A library holding `report` v1 and v2, both superseded, and v3, archived."""
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for version in (1, 2, 3):
        await library.put(_report(version), title="Report", summary="s", applicability="Use per topic.", provenance={"saved_by": "owner:cli"})
    await library.set_archived("wf.report.v3", archived=True)
    return library


async def test_versions_sends_one_statement_and_keeps_superseded_and_archived_ids(tmp_path: Path) -> None:
    store = FakeCollection(prune=True)
    library = await _three_versions(store)
    store.sent.clear()

    found = await library.versions("report")

    assert found == ((1, "wf.report.v1"), (2, "wf.report.v2"), (3, "wf.report.v3"))
    assert store.sent == [(ALL_VERSIONS_SQL, ("workflows", "wf.report.v%"))]
    assert isinstance(library, WorkflowVersionQueries)
    assert not isinstance(FolderLibrary(tmp_path), WorkflowVersionQueries), "a library without the read is not narrowed to it"


async def test_versions_of_an_unsaved_name_is_empty() -> None:
    library = await _three_versions(FakeCollection(prune=True))

    assert await library.versions("unsaved") == ()


async def test_version_hashes_maps_every_saved_version_s_content_address_to_its_version() -> None:
    library = await _three_versions(FakeCollection(prune=True))

    hashes = await version_hashes(_report(3), versions=library, fetch_workflow=library.fetch_workflow)

    assert hashes == {spec_identity(_report(version)): version for version in (1, 2, 3)}


async def test_version_hashes_leaves_out_a_version_that_does_not_read() -> None:
    store = FakeCollection(prune=True)
    library = await _three_versions(store)
    store.pages["wf.report.v1"]["frontmatter"]["spec"] = {"name": "report"}

    hashes = await version_hashes(_report(3), versions=library, fetch_workflow=library.fetch_workflow)

    assert hashes == {spec_identity(_report(version)): version for version in (2, 3)}


async def test_version_hashes_without_the_versions_read_holds_the_workflow_alone() -> None:
    async def unread(workflow_id: str) -> Workflow:
        msg = f"no read of {workflow_id} was expected"
        raise LibraryError(msg)

    hashes = await version_hashes(_report(3), versions=None, fetch_workflow=unread)

    assert hashes == {spec_identity(_report(3)): 3}


async def test_version_hashes_reads_the_current_workflow_s_address_from_the_workflow_given() -> None:
    """The version the caller holds is never read again, so its address is the one the caller's trust is asked about."""
    library = await _three_versions(FakeCollection(prune=True))
    edited = _report(3).model_copy(update={"description": "held by the caller, not the library"})

    hashes = await version_hashes(edited, versions=library, fetch_workflow=library.fetch_workflow)

    assert hashes[spec_identity(edited)] == 3
    assert spec_identity(_report(3)) not in hashes
