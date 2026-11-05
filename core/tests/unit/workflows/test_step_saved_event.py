"""A new reusable step version hands the ingest `ReusableStepSaved` with its own page, and no other save hands it one.

`FakeCollection` keeps every event the ingest was handed beside the id of the page it came with, so each test proves
which write carried it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from core.ai.workflows.contracts import Briefing, LibraryCollection, ReusableStepSaved, Shape, step_content_address
from tests.support.fakes.library_collection import FakeCollection

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pydantic import JsonValue


def _library(store: FakeCollection) -> LibraryCollection:
    return LibraryCollection(store, store.ingest)


def _digest(instructions: str = "Read the whole source and write one digest page that quotes its claims.") -> Briefing:
    return Briefing(
        name="digest",
        instructions=instructions,
        inputs={"source": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
    )


V1 = _digest()
V2 = _digest("Read the whole source, then write one digest page that quotes each claim it keeps.")
V3 = _digest("Read the whole source, then write one digest page that quotes and dates each claim it keeps.")


async def _save(library: LibraryCollection, step: Briefing, version: int, *, cases: Mapping[str, JsonValue] | None = None) -> str:
    return await library.put_step(
        step,
        version=version,
        title="Digest",
        summary="One source, one digest.",
        applicability="Use per source.",
        provenance={"saved_by": "owner:cli"},
        cases=cases,
    )


def _saved(step: Briefing, version: int, *, superseded: tuple[str, ...] = ()) -> ReusableStepSaved:
    return ReusableStepSaved(
        step=step.name,
        version=version,
        step_id=f"st.{step.name}.v{version}",
        step_hash=step_content_address(step),
        saved_by="owner:cli",
        superseded=superseded,
    )


async def test_a_new_version_above_the_newest_hands_one_event_with_its_own_page_and_none_with_the_supersession() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, V1, 1)
    store.events.clear()

    await _save(library, V2, 2)

    assert store.events == [("st.digest.v2", _saved(V2, 2, superseded=("st.digest.v1",)))]
    assert store.pages["st.digest.v1"]["superseded"] is True


async def test_saving_a_version_again_with_equal_content_hands_the_ingest_nothing_at_all() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, V1, 1)
    store.events.clear()
    written = len(store.payloads)

    await _save(library, V1, 1)

    assert store.events == []
    assert len(store.payloads) == written


async def test_the_first_save_of_a_new_name_hands_one_event_that_supersedes_nothing() -> None:
    store = FakeCollection()

    await _save(_library(store), V1, 1)

    assert store.events == [("st.digest.v1", _saved(V1, 1))]


async def test_a_version_below_the_newest_is_written_and_hands_no_event() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, V2, 2)
    store.events.clear()

    await _save(library, V1, 1)

    assert "st.digest.v1" in store.pages
    assert store.events == []


async def test_an_archived_newest_version_still_counts_as_the_newest() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, V2, 2)
    store.pages["st.digest.v2"]["frontmatter"]["archived_at"] = "2026-11-05T09:00:00+00:00"
    store.events.clear()

    await _save(library, V1, 1)

    assert store.events == []


async def test_the_event_names_only_the_lower_versions_the_save_retires() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, V1, 1)
    await _save(library, V2, 2)
    store.events.clear()

    await _save(library, V3, 3)

    assert store.events == [("st.digest.v3", _saved(V3, 3, superseded=("st.digest.v2",)))]


async def test_every_live_lower_version_is_named_lowest_first() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, V2, 2)
    await _save(library, V1, 1)
    store.events.clear()

    await _save(library, V3, 3)

    assert store.events == [("st.digest.v3", _saved(V3, 3, superseded=("st.digest.v1", "st.digest.v2")))]


async def test_new_cases_under_an_unchanged_version_update_it_and_hand_no_event() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, V1, 1, cases={"one": {"inputs": {"source": "a"}}})
    store.events.clear()
    written = len(store.payloads)

    await _save(library, V1, 1, cases={"one": {"inputs": {"source": "b"}}})

    assert len(store.payloads) == written + 1
    assert store.events == []
