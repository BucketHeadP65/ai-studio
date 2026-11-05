"""Every save path records the same consumer record: the Save button, `save_workflow`, `adapt_workflow` and `harness import`.

Each path saves the same workflow, holding a copy of the reusable step `digest` at v3, into its own library over one
small collection. The fake below is that collection: the ingest writes into the same pages the rows read from, shaped
the way `api.cards` answers (the modelled keys lifted into columns, the rest left in `frontmatter`), and it drops
`None` and empty containers at every depth before it stores, as engram's ingest does.
"""

from __future__ import annotations

import json
import re

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

import yaml

from core.ai.workflows.contracts import (
    Briefing,
    ConsumerEntry,
    FromNode,
    FromParameter,
    LibraryCollection,
    ParameterSpec,
    RunNode,
    Shape,
    StepRegistry,
    Workflow,
    WorkflowContract,
    WorkflowPresentation,
    step_content_address,
)
from core.ai.workflows.infrastructure.library_collection import (
    FETCH_SQL,
    LIVE_STEP_VERSIONS_SQL,
    LIVE_VERSIONS_SQL,
    PAGE_SQL,
    STEP_PAGES_SQL,
    SUPERSEDED_SQL,
    WORKFLOW_COLLECTION,
    collection_toolset,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import RunUsage

from harness.features.authoring.domain import Draft
from harness.features.authoring.service import save_draft
from harness.features.browsing.importing import import_path
from harness.settings import SelfAuthoringSettings

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

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

    def __init__(self, *, collection: str = "test-knowledge-base") -> None:
        self.collection = collection
        self.pages: dict[str, dict[str, Any]] = {}
        self.payloads: list[Mapping[str, Any]] = []

    async def ingest(self, payload: Mapping[str, Any], *, collection: str) -> None:
        assert collection == self.collection
        self.payloads.append(payload)
        authored = dict(payload["frontmatter"])
        long_tail = {key: value for key, value in authored.items() if key not in (*_COLUMNS, "superseded_by")}
        self.pages[str(authored["id"])] = {
            **{key: authored[key] for key in _COLUMNS},
            "superseded": authored.get("superseded_by") is not None,
            "frontmatter": {key: kept for key, kept in ((key, _pruned(value)) for key, value in long_tail.items()) if kept is not None},
            "applicability": payload["sections"][0]["body"],
        }

    async def fetch(self, sql: str, /, *args: object) -> Sequence[Mapping[str, Any]]:
        if sql == STEP_PAGES_SQL:
            names = args[1]
            assert isinstance(names, list)
            return [
                {"id": page["id"], "step": json.dumps(page["frontmatter"]["step"])}
                for page in self.pages.values()
                if page["type"] == "step" and page["frontmatter"]["step"]["name"] in names
            ]
        if sql in (LIVE_STEP_VERSIONS_SQL, LIVE_VERSIONS_SQL):
            page_type = "step" if sql == LIVE_STEP_VERSIONS_SQL else "workflow"
            # SQL `like`: `%` is any run of characters and `_` is any one character.
            like = re.compile("".join(".*" if char == "%" else "." if char == "_" else re.escape(char) for char in str(args[1])))
            return [
                {"id": page["id"]}
                for page in self.pages.values()
                if page["type"] == page_type and like.fullmatch(str(page["id"])) and "archived_at" not in page["frontmatter"] and not page["superseded"]
            ]
        page = self.pages.get(str(args[0]))
        if sql == SUPERSEDED_SQL:
            return [{"superseded": page["superseded"]}] if page is not None else []
        assert sql == PAGE_SQL, sql
        return [page] if page is not None else []

    async def fetchval(self, sql: str, /, *args: object) -> str | Mapping[str, Any] | None:
        assert sql == FETCH_SQL, sql
        page = self.pages.get(str(args[0]))
        return page["frontmatter"] if page is not None else None

    def written(self, workflow_id: str) -> Mapping[str, Any]:
        """The frontmatter the last write of `workflow_id` handed the ingest."""
        return next(payload["frontmatter"] for payload in reversed(self.payloads) if payload["frontmatter"]["id"] == workflow_id)


TEXT = Shape(json_schema={"type": "string"})


def _digest(revision: str) -> Briefing:
    return Briefing(name="digest", instructions=f"Read the whole source and write one digest page. {revision}", inputs={"source": TEXT}, output=TEXT)


DIGEST = {1: _digest("Quote nothing."), 2: _digest("Quote every claim."), 3: _digest("Quote every claim and name its section.")}
"""Three saved versions of the reusable step `digest`, each its own content."""

EXPECTED = [ConsumerEntry(step="digest", version=3, step_hash=step_content_address(DIGEST[3])).model_dump(mode="json")]
"""The consumer record every path writes: the copy matches `digest` v3."""

WORKFLOW_FILE = """---
title: Digest sources
description: Digest one source.
uses: [st.digest.v3]
workflow:
  name: digest-sources
  version: 1
  description: Digest one source.
  parameters:
    source: {description: the source, json_schema: {type: string}}
  nodes:
    - kind: run
      id: digest
      work: digest
      inputs: {source: {parameter: source}}
  output: {node: digest}
---

Use per source. Not for a batch.
"""


def _workflow(*inline: Briefing) -> Workflow:
    """The workflow `WORKFLOW_FILE` holds, with `inline` as its copies."""
    return Workflow(
        name="digest-sources",
        version=1,
        description="Digest one source.",
        parameters={"source": ParameterSpec(description="the source", json_schema={"type": "string"})},
        inline=inline,
        nodes=(RunNode(id="digest", work="digest", inputs={"source": FromParameter(parameter="source")}),),
        output=FromNode(node="digest"),
    )


async def _never_run(task: Briefing, output_type: type, /, **inputs: object) -> object:  # pragma: no cover (saving never runs a step)
    raise AssertionError


def _registry() -> StepRegistry:
    return StepRegistry(perform=_never_run)


async def _library(store: FakeCollection) -> LibraryCollection:
    """A library holding `digest` v1 to v3."""
    library = LibraryCollection(store, store.ingest, collection=store.collection)
    for version, step in DIGEST.items():
        await library.put_step(step, version=version, title="Digest", summary="s", applicability="a", provenance={"saved_by": "owner:cli"})
    return library


def _port(library: LibraryCollection) -> WorkflowContract[None]:
    return WorkflowContract[None](
        source=collection_toolset(library), vocabulary=WORKFLOW_COLLECTION, expose=(), registry=_registry(), saved_by="agent:test"
    )


def _ctx() -> RunContext[None]:
    return RunContext(deps=None, model=TestModel(), usage=RunUsage())


def _spec_yaml(workflow: Workflow) -> str:
    return yaml.safe_dump(workflow.model_dump(mode="json"), sort_keys=False)


async def _through_the_save_button(tmp_path: Path) -> list[object]:
    store = FakeCollection()
    library = await _library(store)
    draft = Draft(
        spec_yaml=_spec_yaml(_workflow(DIGEST[3])),
        title="Digest sources",
        summary="Digest one source.",
        applicability="Use per source. Not for a batch.",
        presentation=WorkflowPresentation(
            purpose="Digest one source.", produces="One digest page.", owner_participation="You read the page.", not_for="A batch of sources."
        ),
    )
    workflow_id = await save_draft(draft, library=library, registry=_registry(), self_authoring=SelfAuthoringSettings(custom_root=tmp_path / "custom"))
    return store.written(workflow_id)["reusable_steps"]


async def _through_save_workflow() -> list[object]:
    store = FakeCollection()
    said = await _port(await _library(store))._save(_ctx(), _spec_yaml(_workflow()), applicability="Use per source. Not for a batch.", uses=["st.digest.v3"])
    assert said.startswith("saved as wf.digest-sources.v1"), said
    return store.written("wf.digest-sources.v1")["reusable_steps"]


async def _through_adapt_workflow() -> list[object]:
    store = FakeCollection()
    port = _port(await _library(store))
    await port._save(_ctx(), _spec_yaml(_workflow()), applicability="Use per source. Not for a batch.", uses=["st.digest.v3"])
    said = await port._adapt(
        _ctx(), "wf.digest-sources.v1", ops=[{"op": "set_description", "to": "Digest one source, briefly."}], applicability="Use per source. Not for a batch."
    )
    assert said.startswith("adapted: wf.digest-sources.v1 -> wf.digest-sources.v2"), said
    return store.written("wf.digest-sources.v2")["reusable_steps"]


async def _through_the_import(tmp_path: Path) -> list[object]:
    store = FakeCollection()
    library = await _library(store)
    folder = tmp_path / "import"
    (folder / "workflows").mkdir(parents=True)
    (folder / "workflows" / "digest-sources.md").write_text(WORKFLOW_FILE, encoding="utf-8")
    written = await import_path(folder, library=library, registry=_registry(), by="test:import")
    assert written == ("wf.digest-sources.v1",)
    return store.written("wf.digest-sources.v1")["reusable_steps"]


async def test_every_save_path_records_the_same_reusable_steps(tmp_path: Path) -> None:
    recorded = {
        "the Save button": await _through_the_save_button(tmp_path),
        "save_workflow": await _through_save_workflow(),
        "adapt_workflow": await _through_adapt_workflow(),
        "harness import": await _through_the_import(tmp_path),
    }

    assert recorded == dict.fromkeys(recorded, EXPECTED)


async def test_no_save_path_writes_uses(tmp_path: Path) -> None:
    store = FakeCollection()
    port = _port(await _library(store))
    await port._save(_ctx(), _spec_yaml(_workflow()), applicability="Use per source. Not for a batch.", uses=["st.digest.v3"])
    folder = tmp_path / "import"
    (folder / "workflows").mkdir(parents=True)
    (folder / "workflows" / "digest-sources.md").write_text(WORKFLOW_FILE.replace("version: 1", "version: 2"), encoding="utf-8")
    await import_path(folder, library=LibraryCollection(store, store.ingest, collection=store.collection), registry=_registry(), by="test:import")

    assert [("uses" in payload["frontmatter"]) for payload in store.payloads if payload["frontmatter"]["type"] == "workflow"] == [False, False, False]
