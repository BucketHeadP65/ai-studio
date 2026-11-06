"""An adoption on live trial carries its marker, and a revert makes the previous version live again.

Every library here runs over `FakeCollection` with `prune` on, as engram's ingest stores a page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    LibraryCollection,
    LibraryError,
    LiveTrialLibrary,
    LiveTrialMarker,
    LiveTrialStep,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowContract,
    WorkflowLibrary,
    adapt,
    next_version,
    step_content_address,
)
from core.ai.workflows.infrastructure.folder import FolderLibrary
from core.ai.workflows.infrastructure.library_collection import WORKFLOW_COLLECTION, collection_toolset
from tests.support.fakes.library_collection import FakeCollection

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path
    from typing import Any

    from core.cqrs.contracts import Event

TEXT = Shape(json_schema={"type": "string"})
DIGEST = {
    1: Briefing(name="digest", instructions="Read the whole source and write one digest page.", inputs={"source": TEXT}, output=TEXT),
    2: Briefing(name="digest", instructions="Read the whole source and write one digest page that quotes each claim.", inputs={"source": TEXT}, output=TEXT),
}
CONSUMER = Workflow(
    name="digest-sources",
    version=1,
    description="Digest one source.",
    parameters={"source": ParameterSpec(description="the source", json_schema={"type": "string"})},
    inline=(DIGEST[1],),
    nodes=(RunNode(id="digest", work="digest", inputs={"source": FromParameter(parameter="source")}),),
    output=FromNode(node="digest"),
)
MARKER = LiveTrialMarker(previous="wf.digest-sources.v1", steps=(LiveTrialStep(step="digest", step_hash=step_content_address(DIGEST[2])),))
SUMMARIZE = {
    1: Briefing(name="summarize", instructions="Summarize the digest page in three sentences for the weekly note.", inputs={"text": TEXT}, output=TEXT),
    2: Briefing(name="summarize", instructions="Summarize the digest page in two sentences that name every claim.", inputs={"text": TEXT}, output=TEXT),
}


async def _adopted(store: FakeCollection) -> LibraryCollection:
    """A library holding `digest` v1 and v2, the consumer at v1, and v2 adopting `digest` v2 on live trial."""
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for version, step in DIGEST.items():
        await library.put_step(step, version=version, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    await library.put(CONSUMER, title="Digest sources", summary="s", applicability="Use per source.", provenance={"saved_by": "owner:cli"})
    child, lineage = adapt(CONSUMER, ops=[{"op": "replace_inline_step", "name": "digest", "with": DIGEST[2].model_dump(mode="json")}])
    await library.put(
        child,
        title="Digest sources",
        summary="s",
        applicability="Use per source.",
        provenance={"saved_by": "agent:step-maintenance"},
        lineage=lineage,
        live_trial=MARKER,
    )
    return library


async def test_an_adoption_carries_its_live_trial_marker_and_a_version_without_one_reads_none() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)

    assert store.written("wf.digest-sources.v2")["live_trial"] == MARKER.model_dump(mode="json")
    assert await library.fetch_live_trial("wf.digest-sources.v2") == MARKER
    assert await library.fetch_live_trial("digest-sources") == MARKER, "a bare name reads its newest live version"
    assert await library.fetch_live_trial("wf.digest-sources.v1") is None
    assert "live_trial" not in store.written("wf.digest-sources.v1")


async def test_a_marker_that_does_not_parse_is_refused_naming_the_page() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    store.pages["wf.digest-sources.v2"]["frontmatter"]["live_trial"] = {"previous": "wf.digest-sources.v1"}

    with pytest.raises(LibraryError, match=r"wf\.digest-sources\.v2"):
        await library.fetch_live_trial("wf.digest-sources.v2")


async def test_revert_archives_the_newest_live_version_and_makes_its_parent_live_again() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    assert store.pages["wf.digest-sources.v1"]["superseded"]
    parent_before = dict(store.pages["wf.digest-sources.v1"]["frontmatter"])

    restored = await library.revert("wf.digest-sources.v2")

    assert restored == "wf.digest-sources.v1"
    assert "archived_at" in store.pages["wf.digest-sources.v2"]["frontmatter"]
    assert not store.pages["wf.digest-sources.v1"]["superseded"]
    assert store.pages["wf.digest-sources.v1"]["frontmatter"] == parent_before, "the parent keeps every key, its record and trust history included"
    assert await library.current("wf.digest-sources.v2") == "wf.digest-sources.v1"
    assert (await library.fetch_workflow("digest-sources")).version == 1


async def test_revert_of_a_version_a_person_saved_past_raises_and_writes_nothing() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    person, lineage = adapt(await library.fetch_workflow("wf.digest-sources.v2"), ops=[{"op": "set_description", "to": "Digest one source, briefly."}])
    await library.put(person, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "owner:studio"}, lineage=lineage)
    written = len(store.payloads)

    with pytest.raises(LibraryError, match=r"wf\.digest-sources\.v3"):
        await library.revert("wf.digest-sources.v2")

    assert len(store.payloads) == written
    assert await library.current("wf.digest-sources.v1") == "wf.digest-sources.v3"


async def test_revert_of_a_version_with_no_live_trial_marker_raises_and_writes_nothing() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    person, lineage = adapt(await library.fetch_workflow("wf.digest-sources.v2"), ops=[{"op": "set_description", "to": "Digest one source, briefly."}])
    await library.put(person, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "owner:studio"}, lineage=lineage)
    written = len(store.payloads)

    with pytest.raises(LibraryError, match="live trial marker"):
        await library.revert("wf.digest-sources.v3")

    assert len(store.payloads) == written


async def test_revert_of_a_stacked_adoption_makes_the_marker_s_previous_version_live_and_leaves_the_version_between_superseded() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for version in (1, 2):
        await library.put_step(DIGEST[version], version=version, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
        await library.put_step(SUMMARIZE[version], version=version, title="Summarize", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    first = CONSUMER.model_copy(
        update={
            "inline": (DIGEST[1], SUMMARIZE[1]),
            "nodes": (*CONSUMER.nodes, RunNode(id="summarize", work="summarize", inputs={"text": FromNode(node="digest")})),
            "output": FromNode(node="summarize"),
        }
    )
    await library.put(first, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    between, lineage = adapt(first, ops=[{"op": "replace_inline_step", "name": "digest", "with": DIGEST[2].model_dump(mode="json")}])
    await library.put(
        between, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "agent:step-maintenance"}, lineage=lineage, live_trial=MARKER
    )
    child, lineage = adapt(between, ops=[{"op": "replace_inline_step", "name": "summarize", "with": SUMMARIZE[2].model_dump(mode="json")}])
    stacked = MARKER.with_steps((LiveTrialStep(step="summarize", step_hash=step_content_address(SUMMARIZE[2])),))
    await library.put(
        child, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "agent:step-maintenance"}, lineage=lineage, live_trial=stacked
    )

    restored = await library.revert("wf.digest-sources.v3")

    assert restored == "wf.digest-sources.v1"
    assert "archived_at" in store.pages["wf.digest-sources.v3"]["frontmatter"]
    assert not store.pages["wf.digest-sources.v1"]["superseded"]
    assert store.pages["wf.digest-sources.v2"]["superseded"], "the version between stays superseded"
    assert "archived_at" not in store.pages["wf.digest-sources.v2"]["frontmatter"]
    assert await library.current("wf.digest-sources.v3") == "wf.digest-sources.v1"


@pytest.mark.parametrize("named", ["digest-sources", "wf.digest-sources"])
async def test_revert_refuses_anything_but_a_version_id(named: str) -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    written = len(store.payloads)

    with pytest.raises(LibraryError, match="version id"):
        await library.revert(named)

    assert len(store.payloads) == written


async def test_revert_refuses_a_parent_that_is_archived_and_writes_nothing() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    store.pages["wf.digest-sources.v1"]["frontmatter"]["archived_at"] = "2026-11-01T00:00:00+00:00"
    written = len(store.payloads)

    with pytest.raises(LibraryError, match="archived"):
        await library.revert("wf.digest-sources.v2")

    assert len(store.payloads) == written


async def test_revert_refuses_a_parent_that_is_not_there_and_writes_nothing() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    del store.pages["wf.digest-sources.v1"]
    written = len(store.payloads)

    with pytest.raises(LibraryError, match=r"wf\.digest-sources\.v1"):
        await library.revert("wf.digest-sources.v2")

    assert len(store.payloads) == written


async def test_a_failed_archive_leaves_the_parent_live_and_the_version_newest_so_the_revert_can_be_asked_again() -> None:
    store = FakeCollection(prune=True)
    await _adopted(store)
    refusing = True

    async def ingest(payload: Mapping[str, Any], *, collection: str, events: Sequence[Event] = ()) -> None:
        if refusing and "archived_at" in payload["frontmatter"]:
            msg = "the knowledge base is read-only for a moment"
            raise RuntimeError(msg)
        await store.ingest(payload, collection=collection, events=events)

    library = LibraryCollection(store, ingest, collection=store.collection)

    with pytest.raises(LibraryError, match="read-only") as refused:
        await library.revert("wf.digest-sources.v2")

    assert "wf.digest-sources.v1 is live again" in str(refused.value)
    assert "Revert wf.digest-sources.v2 again" in str(refused.value)

    assert not store.pages["wf.digest-sources.v1"]["superseded"]
    assert await library.current("wf.digest-sources.v1") == "wf.digest-sources.v2", "the version that was not archived is still the newest"
    refusing = False
    assert await library.revert("wf.digest-sources.v2") == "wf.digest-sources.v1"
    assert await library.current("wf.digest-sources.v2") == "wf.digest-sources.v1"


def test_the_collection_answers_the_live_trial_port_and_the_folder_library_does_not(tmp_path: Path) -> None:
    store = FakeCollection()
    collection = LibraryCollection(store, store.ingest)

    assert isinstance(collection, LiveTrialLibrary)
    assert isinstance(collection, WorkflowLibrary)
    assert not isinstance(FolderLibrary(tmp_path), LiveTrialLibrary)


async def test_a_save_over_an_archived_version_is_refused_naming_the_next_free_version_and_writes_nothing() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    await library.revert("wf.digest-sources.v2")
    archived_before = dict(store.pages["wf.digest-sources.v2"]["frontmatter"])
    written = len(store.payloads)
    again, lineage = adapt(CONSUMER, ops=[{"op": "set_description", "to": "Digest one source, briefly."}])

    with pytest.raises(LibraryError, match=r"wf\.digest-sources\.v2") as refused:
        await library.put(again, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "owner:studio"}, lineage=lineage)

    assert "archived" in str(refused.value)
    assert "version 3" in str(refused.value), "the refusal names the next free version number"
    assert len(store.payloads) == written
    assert store.pages["wf.digest-sources.v2"]["frontmatter"] == archived_before, "the archived version keeps its spec, lineage, marker and archived_at"
    assert await library.current("wf.digest-sources.v1") == "wf.digest-sources.v1"


async def test_revert_raises_again_the_diverged_copy_flags_of_the_version_it_makes_live() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for version in (1, 2):
        await library.put_step(DIGEST[version], version=version, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
        await library.put_step(SUMMARIZE[version], version=version, title="Summarize", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    edited = SUMMARIZE[1].model_copy(update={"instructions": "Summarize the digest page in three sentences for the weekly note, plainly."})
    consumer = CONSUMER.model_copy(
        update={
            "inline": (DIGEST[1], edited),
            "nodes": (*CONSUMER.nodes, RunNode(id="summarize", work="summarize", inputs={"text": FromNode(node="digest")})),
            "output": FromNode(node="summarize"),
        }
    )
    await library.put(consumer, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    child, lineage = adapt(consumer, ops=[{"op": "replace_inline_step", "name": "digest", "with": DIGEST[2].model_dump(mode="json")}])
    await library.put(
        child, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "agent:step-maintenance"}, lineage=lineage, live_trial=MARKER
    )
    flag = await library.fetch_flag("mf.diverged.digest-sources.summarize")
    assert flag is not None
    assert (flag.status, flag.workflow) == ("open", "wf.digest-sources.v2")

    await library.revert("wf.digest-sources.v2")

    flag = await library.fetch_flag("mf.diverged.digest-sources.summarize")
    assert flag is not None
    assert (flag.status, flag.workflow) == ("open", "wf.digest-sources.v1"), "the restored version still holds the diverged copy"


async def _reverted_at_v4(store: FakeCollection) -> LibraryCollection:
    """A library holding `digest` v1 and v2 and the consumer at v3, whose adoption of `digest` v2 as v4 was reverted."""
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for version, step in DIGEST.items():
        await library.put_step(step, version=version, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    third = CONSUMER.model_copy(update={"version": 3})
    await library.put(third, title="Digest sources", summary="s", applicability="Use per source.", provenance={"saved_by": "owner:cli"})
    child, lineage = adapt(third, ops=[{"op": "replace_inline_step", "name": "digest", "with": DIGEST[2].model_dump(mode="json")}])
    marker = LiveTrialMarker(previous="wf.digest-sources.v3", steps=(LiveTrialStep(step="digest", step_hash=step_content_address(DIGEST[2])),))
    await library.put(
        child, title="Digest sources", summary="s", applicability="Use per source.", provenance={"saved_by": "agent:step-maintenance"}, lineage=lineage, live_trial=marker
    )
    assert await library.revert("wf.digest-sources.v4") == "wf.digest-sources.v3"
    return library


async def _never_run(task: Briefing, output_type: type, /, **inputs: object) -> object:  # pragma: no cover (saving never runs a step)
    raise AssertionError


async def test_the_next_version_is_one_above_every_saved_version_of_the_name_archived_ones_included() -> None:
    library = await _reverted_at_v4(FakeCollection(prune=True))
    restored = await library.fetch_workflow("wf.digest-sources.v3")

    assert await next_version(restored.name, above=restored.version, versions=library) == 5
    assert await next_version(restored.name, above=restored.version, versions=None) == 4, "without a read of the saved versions it is one above"
    assert await next_version("digest-notes", above=1, versions=library) == 2, "a name with no saved version"


async def test_adapt_workflow_after_a_revert_saves_the_next_free_version_and_leaves_the_archived_one_as_it_was() -> None:
    store = FakeCollection(prune=True)
    library = await _reverted_at_v4(store)
    archived_before = dict(store.pages["wf.digest-sources.v4"]["frontmatter"])
    port = WorkflowContract[None](
        source=collection_toolset(library),
        vocabulary=WORKFLOW_COLLECTION,
        expose=(),
        registry=StepRegistry(perform=_never_run),
        saved_by="agent:test",
        versions=library,
    )

    said = await port._adapt(
        RunContext(deps=None, model=TestModel(), usage=RunUsage()),
        "wf.digest-sources.v3",
        ops=[{"op": "set_description", "to": "Digest one source, briefly."}],
        applicability="Use per source. Not for a batch.",
    )

    assert said.startswith("adapted: wf.digest-sources.v3 -> wf.digest-sources.v5 (v5)"), said
    lineage = await library.fetch_lineage("wf.digest-sources.v5")
    assert lineage is not None
    assert (lineage.parent_name, lineage.parent_version) == ("digest-sources", 3)
    assert store.pages["wf.digest-sources.v4"]["frontmatter"] == archived_before
    assert await library.current("wf.digest-sources.v3") == "wf.digest-sources.v5"


async def test_a_save_of_other_content_over_a_version_on_live_trial_is_refused_naming_the_next_free_version_and_the_marker_stays() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    written = len(store.payloads)
    other = (await library.fetch_workflow("wf.digest-sources.v2")).model_copy(update={"description": "Digest one source, briefly."})

    with pytest.raises(LibraryError, match=r"wf\.digest-sources\.v2") as refused:
        await library.put(other, title="Digest sources", summary="s", applicability="a", provenance={"saved_by": "owner:studio"})

    assert "live trial" in str(refused.value)
    assert "version 3" in str(refused.value), "the refusal names the next free version number"
    assert len(store.payloads) == written
    assert await library.fetch_live_trial("wf.digest-sources.v2") == MARKER


async def test_a_save_of_equal_content_over_a_version_on_live_trial_still_saves() -> None:
    store = FakeCollection(prune=True)
    library = await _adopted(store)
    same = await library.fetch_workflow("wf.digest-sources.v2")

    saved = await library.put(
        same, title="Digest sources, renamed", summary="s", applicability="a", provenance={"saved_by": "agent:step-maintenance"}, live_trial=MARKER
    )

    assert saved == "wf.digest-sources.v2"
    assert store.written("wf.digest-sources.v2")["title"] == "Digest sources, renamed"
