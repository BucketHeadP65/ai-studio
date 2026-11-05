"""One reusable step version holds one content, a new version supersedes the lower ones, and the step version reads.

The fake below is a whole small collection: the ingest writes into the same pages the rows read from, shaped the way
`api.cards` answers (the modelled keys lifted into columns, the rest left in `frontmatter`). With `prune` on, the
ingest drops `None` and empty containers at every depth before it stores, as engram's ingest does.
"""

from __future__ import annotations

import json
import re

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import pytest

from core.ai.workflows.contracts import (
    Briefing,
    LibraryCollection,
    LibraryError,
    Shape,
    StepVersionChangedError,
    StepVersionQueries,
    WorkflowLibrary,
    step_content_address,
)
from core.ai.workflows.infrastructure.folder import FolderLibrary
from core.ai.workflows.infrastructure.library_collection import FETCH_SQL, LIVE_STEP_VERSIONS_SQL, PAGE_SQL, STEP_PAGES_SQL

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from pydantic import JsonValue

_COLUMNS = ("id", "type", "title", "summary", "status")


def _pruned(value: object) -> object:
    """What engram stores for one frontmatter value: `None` and empty containers dropped at every depth."""
    if isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        items = [kept for kept in (_pruned(item) for item in value) if kept is not None]
        return items or None
    if isinstance(value, Mapping):
        nested = {str(key): kept for key, kept in ((key, _pruned(item)) for key, item in value.items()) if kept is not None}
        return nested or None
    return None


class FakeCollection:
    """The rows and the ingest of one collection over one dict of pages, so a save is read back by the next call."""

    def __init__(self, *, prune: bool = False) -> None:
        self.prune = prune
        self.pages: dict[str, dict[str, Any]] = {}
        self.payloads: list[Mapping[str, Any]] = []
        self.sent: list[tuple[str, tuple[object, ...]]] = []

    async def ingest(self, payload: Mapping[str, Any], *, collection: str) -> None:
        assert collection == "workflows"
        self.payloads.append(payload)
        authored = dict(payload["frontmatter"])
        long_tail = {key: value for key, value in authored.items() if key not in (*_COLUMNS, "superseded_by")}
        if self.prune:
            long_tail = {key: kept for key, kept in ((key, _pruned(value)) for key, value in long_tail.items()) if kept is not None}
        self.pages[str(authored["id"])] = {
            **{key: authored[key] for key in _COLUMNS},
            "superseded": authored.get("superseded_by") is not None,
            "frontmatter": long_tail,
            "applicability": payload["sections"][0]["body"],
        }

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        self.sent.append((sql, args))
        if sql == STEP_PAGES_SQL:
            names = args[1]
            assert isinstance(names, list)
            return [
                {"id": page["id"], "step": json.dumps(page["frontmatter"]["step"])}
                for page in self.pages.values()
                if page["type"] == "step" and page["frontmatter"]["step"]["name"] in names
            ]
        if sql == LIVE_STEP_VERSIONS_SQL:
            # SQL `like`: `%` is any run of characters and `_` is any one character.
            like = re.compile("".join(".*" if char == "%" else "." if char == "_" else re.escape(char) for char in str(args[1])))
            return [
                {"id": page["id"]}
                for page in self.pages.values()
                if page["type"] == "step"
                and like.fullmatch(str(page["id"]))
                and "archived_at" not in page["frontmatter"]
                and not page["superseded"]
            ]
        assert sql == PAGE_SQL, sql
        page = self.pages.get(str(args[0]))
        return [page] if page is not None else []

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        self.sent.append((sql, args))
        assert sql == FETCH_SQL, sql
        page = self.pages.get(str(args[0]))
        return page["frontmatter"] if page is not None else None


def _library(store: FakeCollection) -> LibraryCollection:
    return LibraryCollection(store, store.ingest)


def _digest(instructions: str = "Read the whole source and write one digest page that quotes its claims.") -> Briefing:
    return Briefing(
        name="digest",
        instructions=instructions,
        inputs={"source": Shape(json_schema={"type": "string"})},
        output=Shape(json_schema={"type": "string"}),
    )


def _relate() -> Briefing:
    return Briefing(
        name="relate",
        instructions="Link the digest page to every earlier page it builds on, and say why.",
        output=Shape(json_schema={"type": "string"}),
    )


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


async def test_saving_one_version_twice_with_equal_content_writes_once_and_raises_nothing() -> None:
    """The store drops the empty `tools` and the `None` tier, and the second save still reads as equal content."""
    store = FakeCollection(prune=True)
    library = _library(store)
    step = _digest().model_copy(update={"tools": (), "tier": None})

    first = await _save(library, step, 1)
    again = await _save(library, step, 1)

    assert first == again == "st.digest.v1"
    assert [payload["frontmatter"]["id"] for payload in store.payloads] == ["st.digest.v1"], "the second save hands the ingest nothing"


async def test_saving_different_content_under_an_existing_version_is_refused_before_any_write() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, _digest(), 1)

    with pytest.raises(StepVersionChangedError) as refused:
        await _save(library, _digest("Read the whole source twice and write one digest page that quotes its claims."), 1)

    assert str(refused.value) == "st.digest.v1 already holds different content. Raise version in the step file and import again."
    assert refused.value.step_id == "st.digest.v1"
    assert isinstance(refused.value, LibraryError), "a caller catching the library's one error catches this too"
    assert len(store.payloads) == 1, "the refused save hands the ingest nothing"


async def test_saving_a_new_version_links_the_lower_live_version_forward_to_it() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, _digest(), 1, cases={"cases": [{"name": "one", "inputs": {"source": "a"}}]})

    await _save(library, _digest("Read the whole source and write one digest page that quotes every claim."), 2)

    written, retired = store.payloads[1:]
    assert written["frontmatter"]["id"] == "st.digest.v2"
    assert retired["frontmatter"]["id"] == "st.digest.v1"
    assert retired["frontmatter"]["superseded_by"] == "[[st.digest.v2]]"
    assert retired["frontmatter"]["type"] == "step", "the rewritten page keeps its page type"
    assert retired["frontmatter"]["step"] == _digest().model_dump(mode="json"), "the retired version keeps its content"
    assert "cases" in retired["frontmatter"], "the retired version keeps its cases"


async def test_a_later_save_never_clears_an_older_version_s_supersession() -> None:
    """Re-saving a superseded version's equal content writes nothing, and a third version leaves the retired one alone."""
    store = FakeCollection()
    library = _library(store)
    await _save(library, _digest(), 1)
    await _save(library, _digest("Read the whole source and write one digest page that quotes every claim."), 2)
    written_so_far = len(store.payloads)

    await _save(library, _digest(), 1)
    assert len(store.payloads) == written_so_far, "equal content under v1 writes nothing, so v1 stays superseded"

    await _save(library, _digest("Read the whole source and write one digest page that quotes every claim it makes."), 3)
    assert [payload["frontmatter"]["id"] for payload in store.payloads[written_so_far:]] == ["st.digest.v3", "st.digest.v2"], (
        "only the live lower version is linked forward, the retired one is not written again"
    )
    assert store.pages["st.digest.v1"]["superseded"]


async def test_step_versions_answers_every_saved_version_with_its_content_address_lowest_first() -> None:
    store = FakeCollection(prune=True)
    library = _library(store)
    v2 = _digest("Read the whole source and write one digest page that quotes every claim.")
    v10 = _digest("Read the whole source and write one digest page that quotes every claim it makes.")
    await _save(library, _digest(), 1)
    await _save(library, v10, 10)
    await _save(library, v2, 2)

    versions = await library.step_versions("digest")

    assert [(found.version, found.step_id) for found in versions] == [(1, "st.digest.v1"), (2, "st.digest.v2"), (10, "st.digest.v10")], (
        "ordered by version number, so v10 comes after v2, and a superseded version is still answered"
    )
    assert [found.step_hash for found in versions] == [step_content_address(step) for step in (_digest(), v2, v10)]
    assert versions[0].step == _digest()
    assert {found.name for found in versions} == {"digest"}


async def test_step_versions_of_many_names_sends_one_statement_and_answers_each_name() -> None:
    store = FakeCollection()
    library = _library(store)
    await _save(library, _digest(), 1)
    await _save(library, _relate(), 1)
    store.sent.clear()

    found = await library.step_versions_of(["digest", "relate", "unsaved"])

    assert [sql for sql, _ in store.sent] == [STEP_PAGES_SQL], "one statement for every name"
    assert store.sent[0][1] == ("workflows", ["digest", "relate", "unsaved"])
    assert [version.step_id for version in found["digest"]] == ["st.digest.v1"]
    assert [version.step_id for version in found["relate"]] == ["st.relate.v1"]
    assert found["unsaved"] == (), "a name with no saved version answers an empty tuple"


async def test_a_name_whose_like_pattern_matches_another_step_s_id_is_not_mixed_in() -> None:
    """`_` is a `like` wildcard, so `st.dig_st.v%` matches `st.digest.v1` in SQL. Only the exact name is linked forward."""
    store = FakeCollection()
    library = _library(store)
    await _save(library, _digest(), 1)
    lookalike = Briefing(
        name="dig_st",
        instructions="Read the whole source and write one digest page that quotes its claims.",
        output=Shape(json_schema={"type": "string"}),
    )
    await _save(library, lookalike, 1)

    await _save(
        library,
        lookalike.model_copy(update={"instructions": "Read the whole source and write one digest page that quotes every claim."}),
        2,
    )

    assert [payload["frontmatter"]["id"] for payload in store.payloads[2:]] == ["st.dig_st.v2", "st.dig_st.v1"]
    assert not store.pages["st.digest.v1"]["superseded"]


async def test_cases_land_under_the_cases_key_and_come_back_exactly_through_a_store_that_drops_empty_values() -> None:
    """A case input that is an empty list or null is part of the case, so the store must not lose it."""
    store = FakeCollection(prune=True)
    library = _library(store)
    cases: Mapping[str, JsonValue] = {
        "cases": [
            {"name": "nothing to check", "inputs": {"drafts": [], "collection": "notes"}, "expected_output": {"outcome": "approve"}},
            {"name": "no expectation", "inputs": {"drafts": [{"title": "One"}], "note": None}, "metadata": {}},
        ]
    }

    with_cases = await _save(library, _digest(), 1, cases=cases)
    without = await _save(library, _relate(), 1)

    assert "cases" in store.payloads[0]["frontmatter"]
    assert "cases" not in store.payloads[1]["frontmatter"], "a step saved without cases carries no key"
    assert await library.fetch_step_cases(with_cases) == cases
    assert await library.fetch_step_cases(without) is None


async def test_fetch_step_cases_refuses_an_id_no_step_has() -> None:
    with pytest.raises(LibraryError, match=r"st\.nope\.v1"):
        await _library(FakeCollection()).fetch_step_cases("st.nope.v1")


async def test_the_collection_answers_the_step_version_reads_and_the_folder_library_is_still_a_library(tmp_path: Path) -> None:
    store = FakeCollection()
    assert isinstance(_library(store), StepVersionQueries)
    assert isinstance(FolderLibrary(tmp_path), WorkflowLibrary)
    assert not isinstance(FolderLibrary(tmp_path), StepVersionQueries), "a library that keeps no reusable steps does not answer them"


async def test_fetch_step_cases_refuses_cases_kept_in_any_shape_but_json_text() -> None:
    store = FakeCollection()
    library = _library(store)
    with_cases = await _save(library, _digest(), 1, cases={"cases": []})
    store.pages[with_cases]["frontmatter"]["cases"] = {"cases": []}

    with pytest.raises(LibraryError, match="JSON text of an object"):
        await library.fetch_step_cases(with_cases)
