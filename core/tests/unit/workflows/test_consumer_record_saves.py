"""Every save records the reusable steps a workflow holds copies of, and the library answers who holds a copy of a step.

Every library here runs over `FakeCollection` with `prune` on, so a stored record loses its nulls and empty
containers at every depth, as engram's ingest drops them.
"""

from __future__ import annotations

import json

from typing import TYPE_CHECKING

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    ConsumerCard,
    ConsumerEntry,
    ConsumerRecords,
    FromNode,
    FromParameter,
    LibraryCollection,
    LibraryError,
    Lineage,
    ParameterSpec,
    RunNode,
    Shape,
    Workflow,
    WorkflowLibrary,
    WorkflowUnknownError,
    adapt,
    step_content_address,
)
from core.ai.workflows.infrastructure.folder import FolderLibrary
from core.ai.workflows.infrastructure.library_collection import CONSUMERS_SQL, STEP_PAGES_SQL
from tests.support.fakes.library_collection import COLUMNS, FakeCollection

if TYPE_CHECKING:
    from pathlib import Path

TEXT = Shape(json_schema={"type": "string"})


def _digest(*lines: str) -> Briefing:
    return Briefing(name="digest", instructions="\n".join(lines), inputs={"source": TEXT}, output=TEXT)


DIGEST = {
    1: _digest("Read the whole source.", "Write one digest page.", "Quote nothing."),
    2: _digest("Read the whole source.", "Write one digest page.", "Quote every claim."),
    3: _digest("Read the whole source.", "Write one digest page with a section per claim.", "Quote every claim and name its section."),
}
"""Three saved versions of the reusable step `digest`, each its own content."""

EDITED = _digest("Read the whole source.", "Write one digest page.", "Quote every claim, briefly.")
"""A copy of `digest` that matches no saved version and is closest by line to v2 (tied with v1, the tie goes higher)."""

_UNSET = {"diverged_from": None, "diverged_basis": None, "declined": None, "declined_because": None}
"""The fields an entry for a copy that matches a version and declines nothing writes as null."""

RELATE = Briefing(name="relate", instructions="Link the digest page to every earlier page it builds on.", inputs={"source": TEXT}, output=TEXT)
WEIGH = Briefing(name="weigh", instructions="Weigh the digest page against the sources it quotes.", inputs={"source": TEXT}, output=TEXT)


def _workflow(*inline: Briefing, name: str = "digest-sources", version: int = 1) -> Workflow:
    return Workflow(
        name=name,
        version=version,
        description="Digest one source.",
        parameters={"source": ParameterSpec(description="the source", json_schema={"type": "string"})},
        inline=inline,
        nodes=tuple(RunNode(id=step.name, work=step.name, inputs={"source": FromParameter(parameter="source")}) for step in inline),
        output=FromNode(node=inline[0].name),
    )


async def _library(store: FakeCollection) -> LibraryCollection:
    """A library holding `digest` v1 to v3 and `relate` v1."""
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for version, step in DIGEST.items():
        await library.put_step(step, version=version, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    await library.put_step(RELATE, version=1, title="Relate", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    return library


async def _put(library: LibraryCollection, workflow: Workflow, *, lineage: Lineage | None = None) -> str:
    return await library.put(
        workflow, title="Digest sources", summary="s", applicability="Use per source.", provenance={"saved_by": "agent:test"}, lineage=lineage
    )


async def test_a_copy_equal_to_a_saved_version_is_recorded_under_reusable_steps_and_uses_is_never_written() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)

    workflow_id = await library.put(
        _workflow(DIGEST[3]),
        title="Digest sources",
        summary="s",
        applicability="Use per source.",
        provenance={"saved_by": "agent:test", "uses": "st.digest.v3"},
    )

    frontmatter = store.written(workflow_id)
    assert frontmatter["reusable_steps"] == [{"step": "digest", "version": 3, "step_hash": step_content_address(DIGEST[3]), **_UNSET}]
    assert "uses" not in frontmatter


async def test_one_read_answers_the_versions_of_every_inline_step_and_a_step_no_reusable_step_has_gets_no_entry() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    store.sent.clear()

    workflow_id = await _put(library, _workflow(WEIGH, RELATE, DIGEST[1]))

    reads = [args for sql, args in store.sent if sql == STEP_PAGES_SQL]
    assert reads == [("workflows", ["weigh", "relate", "digest"])], "one statement names every inline step"
    assert [entry["step"] for entry in store.written(workflow_id)["reusable_steps"]] == ["digest", "relate"]


async def test_a_workflow_holding_no_copy_writes_no_record() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)

    workflow_id = await _put(library, _workflow(WEIGH))

    assert "reusable_steps" not in store.written(workflow_id)


async def test_an_adaptation_whose_copy_was_edited_names_the_version_its_parent_recorded() -> None:
    """The edited copy is closest by line to v2, so a guess would name v2. The parent's record names v3."""
    store = FakeCollection(prune=True)
    library = await _library(store)
    parent = _workflow(DIGEST[3])
    await _put(library, parent)
    child, lineage = adapt(parent, ops=[{"op": "replace_inline_step", "name": "digest", "with": EDITED.model_dump(mode="json")}])

    child_id = await _put(library, child, lineage=lineage)

    (entry,) = store.written(child_id)["reusable_steps"]
    assert entry == {"step": "digest", "version": None, "step_hash": step_content_address(EDITED), **_UNSET, "diverged_from": 3, "diverged_basis": "parent"}
    assert await library.fetch_consumer_record(child_id) == (
        ConsumerEntry(step="digest", version=None, step_hash=step_content_address(EDITED), diverged_from=3, diverged_basis="parent"),
    ), "the entry reads back although the store dropped its null version"


async def test_a_saved_record_reads_back_as_entries_and_a_page_with_none_reads_empty() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    holding = await _put(library, _workflow(DIGEST[2], RELATE))
    bare = await _put(library, _workflow(WEIGH, name="weigh-sources"))

    assert await library.fetch_consumer_record(holding) == (
        ConsumerEntry(step="digest", version=2, step_hash=step_content_address(DIGEST[2])),
        ConsumerEntry(step="relate", version=1, step_hash=step_content_address(RELATE)),
    )
    assert await library.fetch_consumer_record("digest-sources") == await library.fetch_consumer_record(holding), "a bare name reads its newest live version"
    assert await library.fetch_consumer_record(bare) == ()
    with pytest.raises(WorkflowUnknownError):
        await library.fetch_consumer_record("wf.nothing.v1")


async def test_a_record_that_does_not_parse_is_refused_naming_the_page() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    workflow_id = await _put(library, _workflow(DIGEST[3]))
    store.pages[workflow_id]["frontmatter"]["reusable_steps"] = [{"step": "digest", "version": 3}]

    with pytest.raises(LibraryError, match=r"wf\.digest-sources\.v1"):
        await library.fetch_consumer_record(workflow_id)


async def test_consumers_of_sends_the_collection_and_the_step_name_and_answers_each_live_holder() -> None:
    store = FakeCollection(collection="library", prune=True)
    library = await _library(store)
    await _put(library, _workflow(DIGEST[1]))
    await _put(library, _workflow(DIGEST[3], RELATE, version=2))
    await _put(library, _workflow(EDITED, name="brief-sources"))
    await _put(library, _workflow(RELATE, name="relate-sources"))
    store.sent.clear()

    cards = await library.consumers_of("digest")

    assert store.sent == [(CONSUMERS_SQL, ("library", json.dumps([{"step": "digest"}])))]
    assert cards == (
        ConsumerCard(
            workflow_id="wf.brief-sources.v1",
            name="brief-sources",
            version=1,
            entry=ConsumerEntry(
                step="digest",
                version=None,
                step_hash=step_content_address(EDITED),
                diverged_from=2,
                diverged_basis="closest",
            ),
        ),
        ConsumerCard(
            workflow_id="wf.digest-sources.v2",
            name="digest-sources",
            version=2,
            entry=ConsumerEntry(step="digest", version=3, step_hash=step_content_address(DIGEST[3])),
        ),
    ), "the superseded v1 and the workflow holding no copy of digest are left out"
    assert [card.entry.step for card in await library.consumers_of("relate")] == ["relate", "relate"], "each card carries the entry asked about"


async def test_record_decline_declines_the_entry_and_keeps_every_other_key() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    workflow_id = await _put(library, _workflow(DIGEST[2], RELATE))
    await library.set_archived(workflow_id, archived=True)
    before = store.pages[workflow_id]

    await library.record_decline(workflow_id, step="digest", step_hash=step_content_address(DIGEST[3]), because="kept")

    after = store.pages[workflow_id]
    assert {key: value for key, value in after["frontmatter"].items() if key != "reusable_steps"} == {
        key: value for key, value in before["frontmatter"].items() if key != "reusable_steps"
    }
    assert {key: after[key] for key in (*COLUMNS, "applicability")} == {key: before[key] for key in (*COLUMNS, "applicability")}
    assert await library.fetch_consumer_record(workflow_id) == (
        ConsumerEntry(
            step="digest", version=2, step_hash=step_content_address(DIGEST[2]), declined=step_content_address(DIGEST[3]), declined_because="kept"
        ),
        ConsumerEntry(step="relate", version=1, step_hash=step_content_address(RELATE)),
    )


async def test_record_decline_refuses_a_superseded_page_and_hands_the_ingest_nothing() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    old = await _put(library, _workflow(DIGEST[2]))
    await _put(library, _workflow(DIGEST[2], version=2))
    written = len(store.payloads)

    with pytest.raises(LibraryError, match=r"wf\.digest-sources\.v1.*superseded"):
        await library.record_decline(old, step="digest", step_hash=step_content_address(DIGEST[3]), because="reverted")

    assert len(store.payloads) == written


async def test_record_decline_refuses_a_step_the_page_holds_no_copy_of_and_a_page_that_is_not_there() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    workflow_id = await _put(library, _workflow(RELATE))
    written = len(store.payloads)

    with pytest.raises(LibraryError, match="digest"):
        await library.record_decline(workflow_id, step="digest", step_hash=step_content_address(DIGEST[3]), because="kept")
    with pytest.raises(LibraryError, match=r"wf\.nothing\.v1"):
        await library.record_decline("wf.nothing.v1", step="digest", step_hash=step_content_address(DIGEST[3]), because="kept")

    assert len(store.payloads) == written


async def test_a_decline_carries_over_to_an_adaptation_that_keeps_the_copy() -> None:
    store = FakeCollection(prune=True)
    library = await _library(store)
    parent = _workflow(DIGEST[2])
    parent_id = await _put(library, parent)
    await library.record_decline(parent_id, step="digest", step_hash=step_content_address(DIGEST[3]), because="reverted")
    child, lineage = adapt(parent, ops=[{"op": "set_description", "to": "Digest one source, briefly."}])

    child_id = await _put(library, child, lineage=lineage)

    (entry,) = await library.fetch_consumer_record(child_id)
    assert (entry.declined, entry.declined_because) == (step_content_address(DIGEST[3]), "reverted")


async def _declined_adaptation(library: LibraryCollection) -> tuple[Workflow, Lineage]:
    """Save `digest-sources` v1 holding digest v3 and its adaptation v2 holding the edited copy, then decline NEWER on v2."""
    parent = _workflow(DIGEST[3])
    await _put(library, parent)
    child, lineage = adapt(parent, ops=[{"op": "replace_inline_step", "name": "digest", "with": EDITED.model_dump(mode="json")}])
    child_id = await _put(library, child, lineage=lineage)
    await library.record_decline(child_id, step="digest", step_hash=step_content_address(NEWER), because="kept")
    return child, lineage


NEWER = _digest("Read the whole source twice.", "Write one digest page.", "Quote every claim.")
"""A later version of `digest` no library here holds, whose content address a consumer declines."""

KEPT = ConsumerEntry(
    step="digest",
    version=None,
    step_hash=step_content_address(EDITED),
    diverged_from=3,
    diverged_basis="parent",
    declined=step_content_address(NEWER),
    declined_because="kept",
)
"""The entry v2 of `_declined_adaptation` holds: diverged from the v3 its parent recorded, declining NEWER."""


async def test_saving_an_unchanged_version_again_with_no_lineage_keeps_its_decline_and_its_diverged_entry() -> None:
    """The import saves every file again with no lineage: the page's own record is the prior one, not a fresh guess."""
    store = FakeCollection(prune=True)
    library = await _library(store)
    child, _ = await _declined_adaptation(library)
    assert await library.fetch_consumer_record("wf.digest-sources.v2") == (KEPT,)

    await _put(library, child)

    assert await library.fetch_consumer_record("wf.digest-sources.v2") == (KEPT,)


async def test_saving_a_version_again_reads_its_own_record_before_its_lineage_parent_s() -> None:
    """A draft keeps its lineage, so a second Save of the same version names the parent: the page's own record still wins."""
    store = FakeCollection(prune=True)
    library = await _library(store)
    child, lineage = await _declined_adaptation(library)

    await _put(library, child, lineage=lineage)

    assert await library.fetch_consumer_record("wf.digest-sources.v2") == (KEPT,)


def test_the_collection_answers_the_consumer_records_and_the_folder_library_is_still_a_library(tmp_path: Path) -> None:
    store = FakeCollection()
    assert isinstance(LibraryCollection(store, store.ingest), ConsumerRecords)
    assert isinstance(FolderLibrary(tmp_path), WorkflowLibrary)
    assert not isinstance(FolderLibrary(tmp_path), ConsumerRecords)
