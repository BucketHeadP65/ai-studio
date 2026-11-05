"""An adoption on live trial carries its marker, and a revert makes the previous version live again.

Every library here runs over `FakeCollection` with `prune` on, as engram's ingest stores a page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    FromNode,
    FromParameter,
    LibraryCollection,
    LibraryError,
    LiveTrialLibrary,
    LiveTrialMarker,
    ParameterSpec,
    RunNode,
    Shape,
    Workflow,
    WorkflowLibrary,
    adapt,
    step_content_address,
)
from core.ai.workflows.infrastructure.folder import FolderLibrary
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
MARKER = LiveTrialMarker(previous="wf.digest-sources.v1", step="digest", step_hash=step_content_address(DIGEST[2]))


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


async def test_revert_of_a_version_with_no_lineage_parent_raises_and_writes_nothing() -> None:
    store = FakeCollection(prune=True)
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    await library.put(CONSUMER, title="Digest sources", summary="s", applicability="Use per source.", provenance={"saved_by": "owner:cli"})
    written = len(store.payloads)

    with pytest.raises(LibraryError, match="lineage"):
        await library.revert("wf.digest-sources.v1")

    assert len(store.payloads) == written


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

    with pytest.raises(LibraryError, match="read-only"):
        await library.revert("wf.digest-sources.v2")

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
